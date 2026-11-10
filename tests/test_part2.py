import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "part2"))
import dispatcher as d  # noqa: E402
import openpyxl  # noqa: E402

from app import config, db  # noqa: E402
from app.models import LineItem, Receipt  # noqa: E402


def mk(merchant, date, total, review=False):
    return Receipt(merchant_name=merchant, date=date, total=total, currency="USD", line_items=[LineItem(name="x", price=total)],
                   confidence=0.5 if review else 1.0, needs_review=review, warnings=["Parts add up to 1.00 but total is 2.00"] if review else [])


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(d, "OUTBOX", tmp_path / "outbox")
    db.init()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["name", "email", "role"])
    for row in [("A", "a@example.com", "finance"), ("B", "b@example.com", "manager"), ("Dup", "a@example.com", "finance"), ("Bad", "nope", "x")]:
        ws.append(row)
    x = tmp_path / "r.xlsx"
    wb.save(x)
    return x, tmp_path


def test_dry_run_writes_one_email_per_valid_unique_recipient_and_is_idempotent(env):
    x, tmp = env
    db.insert("a.png", mk("Target Stores", "2026-02-18", 50.31))
    db.insert("b.png", mk("Corner Bistro", "2026-02-20", 99.48))
    st = d.run("2026-02-01", "2026-02-28", x)
    emls = sorted((tmp / "outbox").glob("*.eml"))
    assert len(emls) == 2  # duplicate + invalid rows rejected
    text = emls[0].read_text()
    assert "149.79" in text and "Corner Bistro" in text
    d.run("2026-02-01", "2026-02-28", x)  # re-run: nothing new is sent
    assert len(list((tmp / "outbox").glob("*.eml"))) == 2


def test_flagged_receipts_block_dispatch_until_allowed(env):
    x, tmp = env
    db.insert("a.png", mk("Target Stores", "2026-02-18", 50.31, review=True))
    d.run("2026-02-01", "2026-02-28", x)
    assert not (tmp / "outbox").exists() or not list((tmp / "outbox").glob("*.eml"))
    d.run("2026-02-01", "2026-02-28", x, allow_review=True)
    assert "Needs manual review" in next((tmp / "outbox").glob("*.eml")).read_text()

def _key():
    s = d.summarizer(d.collector("2026-02-01", "2026-02-28"), "2026-02-01", "2026-02-28")
    return s.key


def test_smtp_failure_does_not_crash_run_and_can_be_retried(env, monkeypatch):
    x, tmp = env
    db.insert("a.png", mk("Target Stores", "2026-02-18", 50.31))
    monkeypatch.setenv("SMTP_USER", "u@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "x")

    def boom(*a, **k):
        raise d.smtplib.SMTPServerDisconnected("closed")

    monkeypatch.setattr(d.smtplib, "SMTP_SSL", boom)
    st = d.run("2026-02-01", "2026-02-28", x, send=True)  # must not raise
    assert sum("FAILED" in line for line in st.log) == 2    # both valid recipients were tried
    assert not d._already_sent("a@example.com", _key())     # a failure is not recorded as sent


def test_dry_run_does_not_block_a_later_real_send(env, monkeypatch):
    x, tmp = env
    db.insert("a.png", mk("Target Stores", "2026-02-18", 50.31))
    d.run("2026-02-01", "2026-02-28", x)                    # dry run first
    assert not d._already_sent("a@example.com", _key())

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, user, pw): pass
        def send_message(self, msg): sent.append(msg["To"])

    monkeypatch.setenv("SMTP_USER", "u@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "x")
    monkeypatch.setattr(d.smtplib, "SMTP_SSL", FakeSMTP)

    d.run("2026-02-01", "2026-02-28", x, send=True)
    assert sorted(sent) == ["a@example.com", "b@example.com"]
    assert d._already_sent("a@example.com", _key())

    d.run("2026-02-01", "2026-02-28", x, send=True)         # second real send is skipped
    assert len(sent) == 2
    
def test_guard_blocks_unexpected_amounts(env):
    x, _ = env
    db.insert("a.png", mk("Shop", "2026-02-18", 10.0))
    s = d.summarizer(d.collector("2026-02-01", "2026-02-28"), "2026-02-01", "2026-02-28")
    body = d.composer(s, d.Recipient("A", "a@example.com"))
    assert d.guard(s, body) == []
    assert d.guard(s, body + "\nBonus payout: 999.99")