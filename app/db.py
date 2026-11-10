"""SQLite persistence. One short-lived connection per call keeps it thread-safe under FastAPI's threadpool."""
import json
import sqlite3
from contextlib import contextmanager

from . import config
from .models import Receipt, ReceiptRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    filename       TEXT NOT NULL,
    merchant_name  TEXT,
    date           TEXT,
    total_amount   REAL,
    extracted_json TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_receipts_created ON receipts (created_at DESC);
"""


@contextmanager
def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _row_to_record(row: sqlite3.Row) -> ReceiptRecord:
    return ReceiptRecord(
        id=row["id"],
        filename=row["filename"],
        merchant_name=row["merchant_name"],
        date=row["date"],
        total_amount=row["total_amount"],
        created_at=row["created_at"],
        extracted=Receipt.model_validate(json.loads(row["extracted_json"])),
    )


def insert(filename: str, receipt: Receipt) -> ReceiptRecord:
    with connect() as c:
        cur = c.execute(
            "INSERT INTO receipts (filename, merchant_name, date, total_amount, extracted_json) VALUES (?,?,?,?,?)",
            (filename, receipt.merchant_name, receipt.date, receipt.total, receipt.model_dump_json()),
        )
        row = c.execute("SELECT * FROM receipts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_record(row)


def list_all(limit: int = 200) -> list[ReceiptRecord]:
    with connect() as c:
        rows = c.execute("SELECT * FROM receipts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_row_to_record(r) for r in rows]


def get(receipt_id: int) -> ReceiptRecord | None:
    with connect() as c:
        row = c.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    return _row_to_record(row) if row else None
