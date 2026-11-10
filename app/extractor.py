"""Extraction orchestrator.

PDF with text layer ........ pdftotext -> rules            (exact, no OCR noise)
Image / scanned PDF ........ free VLM (if key set) -> validate
                             if VLM missing/unsure -> Tesseract on several preprocessed variants
                             -> rules -> checksum-guided merge -> validate
Every path ends in validate(): the receipt's own arithmetic decides whether we trust the result."""
import itertools
import logging
import re
import subprocess

import cv2
import numpy as np
import pytesseract

from . import config, parser, preprocess, vlm
from .models import Receipt
from .validate import TOL, validate

log = logging.getLogger(__name__)


class ExtractionError(Exception):
    pass


# ---------- PDF ----------
def _pdf_text(data: bytes) -> str:
    try:
        out = subprocess.run(["pdftotext", "-layout", "-l", "3", "-", "-"], input=data, capture_output=True, timeout=30)
        return out.stdout.decode("utf-8", "ignore")
    except (OSError, subprocess.SubprocessError):
        return ""


def _pdf_first_page(data: bytes) -> np.ndarray:
    try:
        out = subprocess.run(
            ["pdftoppm", "-r", "200", "-f", "1", "-l", "1", "-png", "-", ], input=data, capture_output=True, timeout=60
        )
        return preprocess.load_image(out.stdout)
    except Exception as e:
        raise ExtractionError(f"Could not render PDF: {e}") from e


# ---------- OCR + merge ----------
def _ocr_candidates(img: np.ndarray) -> list[Receipt]:
    cands = []
    for name, variant in preprocess.variants(img):
        for psm in (6, 4):
            try:
                text = pytesseract.image_to_string(variant, config=f"--psm {psm}")
            except pytesseract.TesseractNotFoundError as e:
                raise ExtractionError("Tesseract is not installed (apt install tesseract-ocr / brew install tesseract)") from e
            cands.append(validate(parser.parse_text(text, engine="tesseract+rules")))
    return cands


def _vote(values: list[float | None]) -> list[float | None]:
    """Candidate values ordered by frequency (None allowed - 'field absent' is a valid answer)."""
    freq: dict = {}
    for v in values:
        freq[v] = freq.get(v, 0) + 1
    return sorted(freq, key=lambda k: -freq[k])


def _item_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())[:10]


def _recover_items(cands: list[Receipt], target: float):
    """OCR variants each drop or garble different lines. Pool every item seen, then find the subset
    (at most one reading per item) whose prices sum to the target. Returns items or None."""
    pool: dict[tuple, tuple] = {}
    for c in cands:
        for it in c.line_items:
            if re.search(r"\bta[xk]\b|total|\btip\b", it.name, re.I):  # a tax/total line that leaked into items
                continue
            k = (_item_key(it.name), it.price)
            votes = pool[k][1] + 1 if k in pool else 1
            pool[k] = (it, votes)
    entries = sorted(pool.items(), key=lambda kv: -kv[1][1])[:14]
    best = None
    for mask in range(1, 1 << len(entries)):
        chosen = [entries[i] for i in range(len(entries)) if mask >> i & 1]
        keys = [k[0] for k, _ in chosen]
        if len(set(keys)) != len(keys):
            continue
        if abs(sum(k[1] for k, _ in chosen) - target) <= TOL:
            score = (len(chosen), sum(v for _, (_, v) in chosen))
            if best is None or score > best[0]:
                best = (score, [it for _, (it, _) in chosen])
    return best[1] if best else None


def merge(cands: list[Receipt]) -> Receipt:
    """Checksum-guided merge: take the richest item list, then pick the (subtotal, tax, tip, total)
    combination - across all OCR variants - that actually adds up. Falls back to the best single parse."""
    cands = sorted(cands, key=lambda c: (c.confidence, len(c.line_items)), reverse=True)
    best = cands[0].model_copy(deep=True)
    richest = max(cands, key=lambda c: len(c.line_items))
    if len(richest.line_items) > len(best.line_items):
        best.line_items = [i.model_copy() for i in richest.line_items]
    for f in ("merchant_name", "date", "time", "payment_method", "currency"):
        if getattr(best, f) is None:
            best_val = next((getattr(c, f) for c in cands if getattr(c, f)), None)
            setattr(best, f, best_val)

    items_sum = round(sum(i.price for i in best.line_items), 2)
    totals = [v for v in _vote([c.total for c in cands]) if v is not None]
    taxes, tips = _vote([c.tax for c in cands]), _vote([c.tip for c in cands])
    subs = _vote([c.subtotal for c in cands])
    for total, tax, tip, sub in itertools.product(totals, taxes, tips, subs):
        base = sub if sub is not None else items_sum
        if best.line_items and sub is not None and abs(items_sum - sub) > TOL:
            continue
        if abs(base + (tax or 0) + (tip or 0) - total) <= TOL:
            best.total, best.tax, best.tip, best.subtotal = total, tax, tip, sub
            break
    else:
        for total, tax, tip in itertools.product(totals, taxes, tips):
            items = _recover_items(cands, round(total - (tax or 0) - (tip or 0), 2))
            if items:
                best.line_items, best.total, best.tax, best.tip, best.subtotal = items, total, tax, tip, None
                break
        else:
            if best.total is None and totals:
                best.total = totals[0]
    best.engine = "tesseract+rules(merged)"
    return validate(best)


# ---------- public API ----------
def extract(data: bytes, ext: str) -> Receipt:
    if ext == ".pdf":
        text = _pdf_text(data)
        if len(text.strip()) >= 40:
            r = validate(parser.parse_text(text, engine="pdf-text+rules"))
            if r.total is not None:
                return r
        img = _pdf_first_page(data)
    else:
        try:
            img = preprocess.load_image(data)
        except Exception as e:
            raise ExtractionError("Could not read image file") from e

    fallback: Receipt | None = None
    if config.VLM_ENABLED:
        try:
            r = validate(vlm.extract(img))
            if r.confidence >= config.VLM_ACCEPT_CONFIDENCE:
                return r
            fallback = r  # unsure: cross-check with local OCR below
        except Exception as e:
            log.warning("VLM path failed, using local OCR: %s", e)

    ocr = merge(_ocr_candidates(img))
    if fallback and fallback.confidence >= ocr.confidence:
        return fallback
    if ocr.total is None and not ocr.line_items:
        raise ExtractionError("No receipt data could be read from this file")
    return ocr
