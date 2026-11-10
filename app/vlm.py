"""Free vision-LLM extraction via OpenRouter (free-tier models only; no paid keys)."""
import base64
import io
import json
import logging
import re

import cv2
import httpx
import numpy as np
from PIL import Image

from . import config
from .models import LineItem, Receipt
from .parser import parse_date, parse_time, smart_title

log = logging.getLogger(__name__)
URL = "https://openrouter.ai/api/v1/chat/completions"

PROMPT = """You are a receipt parser. Read the receipt image and return ONLY a JSON object (no markdown, no prose) with exactly:
{"merchant_name": string|null, "date": "YYYY-MM-DD"|null, "time": "HH:MM"|null, "currency": "USD"|"INR"|"EUR"|"GBP"|null,
 "line_items": [{"name": string, "quantity": number, "unit_price": number|null, "price": number}],
 "subtotal": number|null, "tax": number|null, "tip": number|null, "total": number|null, "payment_method": string|null}
Rules: "price" is the line total as printed (quantity x unit price). Use null when a field is not printed - never guess.
Numbers are plain numbers without currency symbols. Tip/gratuity goes in "tip"; sales tax/VAT/GST in "tax"."""


def _to_data_url(img: np.ndarray, max_edge: int = 1600) -> str:
    h, w = img.shape[:2]
    if max(h, w) > max_edge:
        s = max_edge / max(h, w)
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    buf = io.BytesIO()
    Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)).save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return round(float(str(v).replace(",", "").replace("$", "").strip()), 2)
    except ValueError:
        return None


def _json_from(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def normalize(d: dict, engine: str) -> Receipt:
    """Coerce whatever the model returned into our schema. Never trust model output types."""
    items = []
    for it in d.get("line_items") or []:
        if not isinstance(it, dict):
            continue
        price, name = _num(it.get("price")), str(it.get("name") or "").strip()
        if price is None or not name:
            continue
        items.append(LineItem(name=name, quantity=_num(it.get("quantity")) or 1, unit_price=_num(it.get("unit_price")), price=price))
    date = str(d.get("date") or "")
    time = str(d.get("time") or "")
    cur = str(d.get("currency") or "").upper() or None
    return Receipt(
        merchant_name=smart_title(str(d["merchant_name"]).strip()) or None if d.get("merchant_name") else None,
        date=date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else parse_date(date),
        time=time if re.fullmatch(r"\d{2}:\d{2}", time) else parse_time(time),
        currency=cur if cur in {"USD", "INR", "EUR", "GBP"} else None,
        line_items=items,
        subtotal=_num(d.get("subtotal")),
        tax=_num(d.get("tax")),
        tip=_num(d.get("tip")),
        total=_num(d.get("total")),
        payment_method=(str(d["payment_method"]).strip() or None) if d.get("payment_method") else None,
        engine=engine,
    )


def extract(img: np.ndarray) -> Receipt:
    """Try each configured free model in order; first valid JSON wins."""
    data_url = _to_data_url(img)
    errors = []
    for model in config.OPENROUTER_MODELS:
        try:
            resp = httpx.post(
                URL,
                headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "X-Title": "receipt-extractor"},
                json={
                    "model": model,
                    "temperature": 0,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": PROMPT},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                },
                timeout=60,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return normalize(_json_from(content), engine=f"vlm:{model}")
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")[:300]
            log.warning("VLM %s failed: %s | %s", model, e, body)
            errors.append(f"{model}: {type(e).__name__}")