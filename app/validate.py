"""Arithmetic sanity checks. A receipt is its own checksum: items + tax + tip should equal the total.
This is what lets the pipeline say "I'm not sure" instead of silently storing a wrong number."""
from .models import Receipt

TOL = 0.02


def validate(r: Receipt) -> Receipt:
    warnings: list[str] = []
    conf = 1.0

    if not r.merchant_name:
        warnings.append("Merchant name not found")
        conf -= 0.15
    if not r.date:
        warnings.append("Date not found")
        conf -= 0.10
    if not r.line_items:
        warnings.append("No line items found")
        conf -= 0.20

    items_sum = round(sum(i.price for i in r.line_items), 2)
    if r.total is None and r.subtotal is not None:
        r.total = round(r.subtotal + (r.tax or 0) + (r.tip or 0), 2)
        warnings.append("Total not found; derived from subtotal + tax + tip")
        conf -= 0.25
    elif r.total is None:
        warnings.append("Total not found")
        conf -= 0.35

    if r.subtotal is not None and r.line_items and abs(items_sum - r.subtotal) > TOL:
        warnings.append(f"Line items add up to {items_sum:.2f} but subtotal is {r.subtotal:.2f}")
        conf -= 0.20

    if r.total is not None:
        base = r.subtotal if r.subtotal is not None else items_sum
        expected = round(base + (r.tax or 0) + (r.tip or 0), 2)
        if (r.subtotal is not None or r.line_items) and abs(expected - r.total) > TOL:
            warnings.append(f"Parts add up to {expected:.2f} but total is {r.total:.2f} (diff {r.total - expected:+.2f})")
            conf -= 0.30

    r.confidence = round(max(conf, 0.0), 2)
    r.needs_review = r.confidence < 0.8
    r.warnings = warnings
    return r
