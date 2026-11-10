import pytest
from fastapi.testclient import TestClient

from app import config, db, extractor, main
from app.models import LineItem, Receipt

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    with TestClient(main.app) as c:  # runs lifespan -> creates schema
        yield c


def fake_receipt(*_a, **_k):
    return Receipt(merchant_name="Shop", date="2026-01-02", total=10.0, line_items=[LineItem(name="x", price=10.0)],
                   confidence=1.0, needs_review=False, engine="fake")


def test_upload_then_list(client, monkeypatch):
    monkeypatch.setattr(extractor, "extract", fake_receipt)
    r = client.post("/upload", files={"file": ("a.png", PNG, "image/png")})
    assert r.status_code == 201
    body = r.json()
    assert body["merchant_name"] == "Shop" and body["total_amount"] == 10.0 and body["extracted"]["engine"] == "fake"
    rows = client.get("/receipts").json()
    assert [x["id"] for x in rows] == [body["id"]]
    assert client.get(f"/receipts/{body['id']}").json()["filename"] == "a.png"


def test_rejects_bad_extension_and_spoofed_content(client):
    assert client.post("/upload", files={"file": ("a.txt", b"hi", "text/plain")}).status_code == 400
    assert client.post("/upload", files={"file": ("a.png", b"not a png", "image/png")}).status_code == 400


def test_rejects_oversized(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 16)
    assert client.post("/upload", files={"file": ("a.png", PNG, "image/png")}).status_code == 413


def test_extraction_failure_is_422_and_nothing_saved(client, monkeypatch):
    def boom(*_a, **_k):
        raise extractor.ExtractionError("unreadable")
    monkeypatch.setattr(extractor, "extract", boom)
    assert client.post("/upload", files={"file": ("a.png", PNG, "image/png")}).status_code == 422
    assert client.get("/receipts").json() == []


def test_missing_receipt_404(client):
    assert client.get("/receipts/12345").status_code == 404


def test_schema_has_required_columns(client):
    with db.connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(receipts)")}
    assert {"id", "filename", "merchant_name", "date", "total_amount", "extracted_json", "created_at"} <= cols
