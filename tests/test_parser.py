"""Parser tests use realistic (noisy) OCR output captured from the three sample receipts."""
from app.parser import parse_date, parse_text, parse_time
from app.validate import validate

BISTRO = """{THE CORNER BISTRO. =t~=‘«‘
a Order #849 - Table 12 |
| Date: Feb 20, 2026 Time: 19:45 E
‘| 1X Sparkling Water. . $4.00 &
2x Caesar Salad © <= $ 22,00
2x Grilled Salmon =; . $48.00 &
Ix Espresso | ae $ 3.50
SUBTOTAL — $77.50 §
TAX ee $6.98 |
GRATUITY / TIP $15.00 |
TOTAL DUE $99.48 |
serverz Sarah M,"""

TARGET = """TARGET STORES |
1234 Main Street, CA
_ Tel: (555) 019-2831 _
| Date: 02/18/2026 -
Time: 14:22 ba
ITEMS PURCHASED: 7 Eo
1. Wireless Mouse $24.99 %
2. USB-C Cable 6ft $ 12.50 =
3. Notebook 3-Pack $ 8.99 {[-
SUBTOTAL | $ 46.48 7
TAX (8.25%) | —§ 3.83 Ok
TOTAL $50.31 :
Payment Method: Visa ending in 4012 |
Thank you for shopping at Target! 7"""

FUEL = """*** QUICK-MART FUEL & MORE ***
STATION #0491 - GURUGRAM
02-25-2026 08:12 AM
PUMP 04 REGULAR UNLEADED
12.4 GALLONS @ $3.25/GAL $ 40.30
MONSTER ENERGY DRINK 160Z $ 3.49
TAX INCLUDED IN FUEL
NON-FUEL TAX $ 0.28
TOTAL PAID $ 44.07
AUTH CODE: 994182 CASH"""


def test_restaurant_receipt_with_tip():
    r = validate(parse_text(BISTRO))
    assert r.merchant_name == "The Corner Bistro"
    assert (r.date, r.time) == ("2026-02-20", "19:45")
    assert [(i.name, i.quantity, i.price) for i in r.line_items] == [
        ("Sparkling Water", 1, 4.0), ("Caesar Salad", 2, 22.0), ("Grilled Salmon", 2, 48.0), ("Espresso", 1, 3.5)]
    assert (r.subtotal, r.tax, r.tip, r.total) == (77.5, 6.98, 15.0, 99.48)
    assert not r.needs_review


def test_retail_receipt_percent_is_not_an_amount():
    r = validate(parse_text(TARGET))
    assert r.merchant_name == "Target Stores"
    assert r.date == "2026-02-18"  # MM/DD/YYYY
    assert [i.price for i in r.line_items] == [24.99, 12.5, 8.99]
    assert (r.subtotal, r.tax, r.total) == (46.48, 3.83, 50.31)  # "TAX (8.25%)" must not become 8.25
    assert r.payment_method == "Visa ****4012"
    assert not r.needs_review


def test_fuel_receipt_unit_price_and_tax_included_line():
    r = validate(parse_text(FUEL))
    assert r.merchant_name == "Quick-Mart Fuel & More"
    assert r.date == "2026-02-25" and r.time == "08:12"
    fuel = r.line_items[0]
    assert (fuel.quantity, fuel.unit_price, fuel.price) == (12.4, 3.25, 40.3)
    assert r.tax == 0.28 and r.total == 44.07  # "TAX INCLUDED IN FUEL" has no amount and is ignored
    assert not r.needs_review


def test_validator_flags_missing_item():
    """If OCR drops a line, the arithmetic no longer closes and the receipt is flagged, not trusted."""
    r = validate(parse_text(FUEL.replace("MONSTER ENERGY DRINK 160Z $ 3.49\n", "")))
    assert r.needs_review
    assert any("add up" in w for w in r.warnings)


def test_dates_and_times():
    assert parse_date("Date: 25/02/2026") == "2026-02-25"   # day > 12 is unambiguous
    assert parse_date("02-25-2026") == "2026-02-25"
    assert parse_date("5 March 2026") == "2026-03-05"
    assert parse_date("2026-03-05") == "2026-03-05"
    assert parse_date("13/13/2026") is None
    assert parse_time("02:05 PM") == "14:05" and parse_time("12:10 AM") == "00:10"


def test_thousands_and_comma_decimals():
    r = parse_text("SHOP\nLaptop Bag $1,299.00\nTOTAL $1,299.00")
    assert r.line_items[0].price == 1299.0 and r.total == 1299.0
    assert parse_text("SHOP\nTOTAL 12,50").total == 12.5
