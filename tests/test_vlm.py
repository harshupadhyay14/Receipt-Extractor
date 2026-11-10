"""The live OpenRouter call needs a key, but everything around it is deterministic and tested here."""
import pytest

from app import vlm


def test_json_extraction_handles_markdown_fences_and_chatter():
    raw = 'Sure! Here you go:\n```json\n{"merchant_name": "Shop", "total": "12.50"}\n```\nHope that helps.'
    assert vlm._json_from(raw)["total"] == "12.50"
    with pytest.raises(ValueError):
        vlm._json_from("sorry, I cannot read this image")


def test_normalize_coerces_untrusted_model_output():
    r = vlm.normalize({
        "merchant_name": " Cafe ", "date": "02/20/2026", "time": "7:45 PM", "currency": "usd",
        "line_items": [{"name": "Latte", "quantity": "2", "price": "$7.00"}, {"name": "", "price": 1}, "junk", {"name": "Bad", "price": "n/a"}],
        "tax": "0.63", "tip": None, "total": "7,63",
    }, engine="vlm:test")
    assert r.merchant_name == "Cafe" and r.date == "2026-02-20" and r.time == "19:45" and r.currency == "USD"
    assert [(i.name, i.quantity, i.price) for i in r.line_items] == [("Latte", 2.0, 7.0)]
    assert r.tax == 0.63 and r.tip is None and r.total == 763.0  # "7,63" is ambiguous; validator will flag it
