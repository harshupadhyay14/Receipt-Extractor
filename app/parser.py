"""Rule-based receipt parser: raw OCR / PDF text -> Receipt fields.

Deliberately forgiving of OCR noise (stray symbols, '$' read as 'S', comma decimals) and deliberately
dumb about layout: it only trusts what it can verify, and the validator (validate.py) flags the rest.
"""
import re
from datetime import date as _date
from decimal import Decimal

from . import config
from .models import LineItem, Receipt

# ---------- money ----------
MONEY_END = re.compile(
    r"(?P<neg>-\s*)?(?P<sym>[$₹€£]|Rs\.?)?\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+)\s*[.,]\s?(?P<dec>\d{2})(?:\s+[A-Za-z|=_\-~%&\s]{1,6})?$"
)
# keyword lines (subtotal/tax/total) tolerate a stray trailing digit/symbol from OCR, e.g. "$ 46.48 7"
MONEY_RELAXED = re.compile(
    r"(?P<neg>-\s*)?(?P<sym>[$₹€£]|Rs\.?)?\s*(?P<num>\d{1,3}(?:,\d{3})+|\d+)\s*[.,]\s?(?P<dec>\d{2})(?:\s+[^\sA-Za-z]{0,2}\d?)?[\s|=_\-~%&]*$"
)
CURRENCY = {"$": "USD", "₹": "INR", "Rs": "INR", "Rs.": "INR", "€": "EUR", "£": "GBP"}


def _amount(m: re.Match) -> float:
    val = Decimal(f"{m.group('num').replace(',', '')}.{m.group('dec')}")
    return float(-val if m.group("neg") else val)


def _clean_line(line: str) -> str:
    # drop trailing OCR debris (|, =, —, &...) but keep things that end a number/word/percent
    return re.sub(r"[^A-Za-z0-9%)]+$", "", line.strip())


# ---------- keywords ----------
RE_SUBTOTAL = re.compile(r"sub\s*-?\s*total", re.I)
RE_TIP = re.compile(r"\b(tip|gratuity|service\s*charge)\b", re.I)
RE_TAX = re.compile(r"\b(tax|vat|gst|cgst|sgst|igst)\b", re.I)
RE_TOTAL = re.compile(r"\b(grand\s*total|total|amount\s*(?:due|paid|payable)|balance\s*due)\b", re.I)
RE_STRONG_TOTAL = re.compile(r"\b(grand\s*total|total\s*(?:due|paid|amount|payable)|amount\s*(?:due|paid|payable)|balance\s*due)\b", re.I)
RE_SKIP_ITEM = re.compile(
    r"\b(change|cash|visa|mastercard|amex|card|auth|tendered|payment|tel|phone|server|order|table|station)\b", re.I
)
RE_HEADER = re.compile(
    r"\b(date|time|order|table|server|tel|phone|station|invoice|receipt|items?\s+purchased|thank|welcome|cashier|auth)\b", re.I
)

# ---------- dates ----------
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
RE_DATE_ISO = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")
RE_DATE_NUM = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})\b")
RE_DATE_MON = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
RE_DATE_MON2 = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+(\d{4})\b", re.I)
RE_TIME = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*(AM|PM|A\.M\.|P\.M\.)?", re.I)


def _mk_date(y: int, m: int, d: int) -> str | None:
    try:
        dt = _date(y, m, d)
    except ValueError:
        return None
    return dt.isoformat() if 2000 <= dt.year <= _date.today().year + 1 else None


def parse_date(text: str) -> str | None:
    if m := RE_DATE_MON.search(text):
        return _mk_date(int(m[3]), MONTHS[m[1][:3].lower()], int(m[2]))
    if m := RE_DATE_MON2.search(text):
        return _mk_date(int(m[3]), MONTHS[m[2][:3].lower()], int(m[1]))
    if m := RE_DATE_ISO.search(text):
        return _mk_date(int(m[1]), int(m[2]), int(m[3]))
    if m := RE_DATE_NUM.search(text):
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        if a > 12:
            return _mk_date(y, b, a)  # clearly DD/MM
        if b > 12:
            return _mk_date(y, a, b)  # clearly MM/DD
        return _mk_date(y, b, a) if config.DATE_DAYFIRST else _mk_date(y, a, b)
    return None


def parse_time(text: str) -> str | None:
    m = RE_TIME.search(text)
    if not m:
        return None
    h, mi, ap = int(m[1]), int(m[2]), (m[3] or "").upper().replace(".", "")
    if ap == "PM" and h < 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


# ---------- text helpers ----------
def smart_title(s: str) -> str:
    return re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)*", lambda m: m.group(0).capitalize(), s) if s.isupper() else s


def strip_ocr_junk(s: str) -> str:
    """Remove debris OCR leaves around real text: leading 'be '/'j ', trailing '=t', '?', 'ae'."""
    s = re.sub(r"^(?:[a-z]{1,3}\s+)+(?=[A-Z0-9])", "", s.strip())
    for _ in range(3):
        s2 = re.sub(r"\s+(?=\S{1,8}$)(?![^\s]*[A-Za-z]{3})(?:[a-z]{1,2}|\S*[^\w\s.&'()%/,:@$+-]\S*)$", "", s) if len(s.split()) > 1 else s
        if s2 == s:
            break
        s = s2
    return re.sub(r"^[^A-Za-z0-9(]+|[^A-Za-z0-9.)]+$", "", s).strip()


def clean_merchant(line: str) -> str | None:
    s = strip_ocr_junk(re.sub(r"^[^A-Za-z0-9]+", "", line.strip()))
    # junk tokens glued in front of an ALL-CAPS name, e.g. "we ¥&* QUICK-MART" (real words like "THE" are protected)
    s = re.sub(r"^(?:(?![A-Z0-9]{2})(?=\S*(?:[a-z]|[^\w\s]))\S{1,4}\s+)+(?=[A-Z]{2,})", "", s)
    if s.endswith(".") and not re.search(r"\b(Inc|Ltd|Co|Corp|LLC|Pvt|Bros|Jr|Sr)\.$", s, re.I):
        s = s[:-1]
    s = re.sub(r"\s{2,}", " ", s).strip()
    return smart_title(s) if s else None


def find_merchant(lines: list[str]) -> tuple[str | None, int]:
    for i, raw in enumerate(lines[:10]):
        # must contain a real word (>=4 letters), not a header/date/amount line
        if not re.search(r"[A-Za-z]{4,}", raw) or RE_HEADER.search(raw) or MONEY_END.search(_clean_line(raw)):
            continue
        name = clean_merchant(raw)
        if name and len(name) >= 3:
            return name, i
    return None, -1


def _clean_item_name(s: str) -> tuple[str, float]:
    qty = 1.0
    s = re.sub(r"(?:(?<=\s)|^)[^\w(@]{1,3}(?=\s|$)", " ", s)  # lone OCR symbols (©, <=, |)
    s = re.sub(r"\.{2,}|_{2,}", " ", s)
    if m := re.match(r"^\s*(\d+|[Il])\s*[xX]\s+", s):
        qty = 1.0 if m[1] in "Il" else float(m[1])
        s = s[m.end():]
    s = re.sub(r"^\s*\d+[.)]\s+", "", s)  # "1. Wireless Mouse"
    s = re.sub(r"\s+[S§$]$", "", s)  # '$' misread as S/§ and left on the name
    s = strip_ocr_junk(re.sub(r"\s{2,}", " ", s).strip(" -:.|"))
    return s, qty


RE_UNIT_PRICE = re.compile(r"(?P<q>\d+(?:\.\d+)?)\s*[A-Za-z]*\s*@\s*[$₹€£]?\s*(?P<p>\d+(?:\.\d+)?)")


def parse_text(text: str, engine: str = "rules") -> Receipt:
    lines = [ln for ln in (l.rstrip() for l in text.splitlines()) if ln.strip()]
    merchant, m_idx = find_merchant(lines)

    r = Receipt(merchant_name=merchant, engine=engine)
    r.date = parse_date(text)
    for ln in lines:
        if (RE_HEADER.search(ln) or parse_date(ln)) and (t := parse_time(ln)):
            r.time = t
            break

    if sym := re.search(r"[$₹€£]|\bRs\.?", text):
        r.currency = CURRENCY.get(sym.group(0))
    if pm := re.search(r"\b(visa|mastercard|amex|discover|rupay)\b(?:[^\n]*?(\d{4}))?", text, re.I):
        r.payment_method = f"{pm[1].title()} ****{pm[2]}" if pm[2] else pm[1].title()
    elif re.search(r"\bcash\b", text, re.I):
        r.payment_method = "Cash"
    elif re.search(r"\bupi\b", text, re.I):
        r.payment_method = "UPI"

    totals: list[tuple[bool, float]] = []
    taxes: list[float] = []
    in_items, pending = True, None
    for raw in lines[m_idx + 1 :]:
        line = _clean_line(raw.replace("_", " "))
        m = MONEY_END.search(line)
        label = line[: m.start()] if m else line
        is_kw = bool(RE_SUBTOTAL.search(label) or RE_TIP.search(label) or RE_TAX.search(label) or RE_TOTAL.search(label))
        if not m and (relaxed := MONEY_RELAXED.search(raw.replace("_", " "))):
            rl = raw.replace("_", " ")[: relaxed.start()]
            if RE_SUBTOTAL.search(rl) or RE_TIP.search(rl) or RE_TAX.search(rl) or RE_TOTAL.search(rl):
                m, label, is_kw = relaxed, rl, True

        if m and is_kw:
            amt = _amount(m)
            if RE_SUBTOTAL.search(label):
                r.subtotal = amt
            elif RE_TIP.search(label):
                r.tip = (r.tip or 0) + amt
            elif RE_TAX.search(label):
                taxes.append(amt)
            else:
                totals.append((bool(RE_STRONG_TOTAL.search(label)), amt))
            if r.line_items:
                in_items = False
            continue
        if is_kw and r.line_items:  # e.g. "TAX INCLUDED IN FUEL" - marks end of the item block
            in_items = False
            continue

        if not in_items:
            continue
        if not m:
            if len(re.findall(r"[A-Za-z]", line)) >= 4 and not RE_HEADER.search(line):
                pending = line
            continue
        if RE_SKIP_ITEM.search(label) or parse_date(label):
            continue

        price = _amount(m)
        qty, unit = 1.0, None
        name_src = label
        if (u := RE_UNIT_PRICE.search(label)) :
            qty, unit = float(u["q"]), float(u["p"])
            unit_word = re.search(r"[A-Za-z]{3,}", label[: u.start("p")])
            desc = f"{qty:g} {unit_word.group(0).lower() + ' ' if unit_word else ''}@ {unit:g}"
            base = smart_title(_clean_item_name(pending)[0]) if pending else ""
            name_src = f"{base} ({desc})" if base else desc
        name, n = _clean_item_name(name_src)
        if not u:
            qty = n
            if qty > 1 and abs(qty * (price / qty) - price) < 1e-9:
                unit = round(price / qty, 2)
        pending = None
        if len(re.findall(r"[A-Za-z]", name)) >= 2:
            r.line_items.append(LineItem(name=smart_title(name), quantity=qty, unit_price=unit, price=price))

    if taxes:
        r.tax = round(sum(taxes), 2)
    if totals:
        strong = [a for s, a in totals if s]
        r.total = strong[0] if strong else totals[0][1]
    return r
