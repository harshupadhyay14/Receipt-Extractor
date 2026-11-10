"""End-to-end on the three assessment receipts (real Tesseract). Skipped if Tesseract isn't installed."""
import shutil
from pathlib import Path

import pytest

from app import extractor

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")

EXPECTED = {
    "receipt_1.jpg": ("The Corner Bistro", "2026-02-20", 99.48, 15.0),
    "receipt_2.jpg": ("Target Stores", "2026-02-18", 50.31, None),
    "receipt_3.jpg": ("Quick-Mart Fuel & More", "2026-02-25", 44.07, None),
}


@pytest.mark.parametrize("name", EXPECTED)
def test_sample_receipt(name):
    merchant, date, total, tip = EXPECTED[name]
    r = extractor.extract((SAMPLES / name).read_bytes(), ".jpg")
    assert r.merchant_name == merchant and r.date == date and r.total == total and r.tip == tip
    assert not r.needs_review, r.warnings
