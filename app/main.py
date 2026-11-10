import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, extractor
from .models import ReceiptRecord

logging.basicConfig(level=logging.INFO)
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".pdf"}
MAGIC = {".png": (b"\x89PNG",), ".jpg": (b"\xff\xd8",), ".jpeg": (b"\xff\xd8",), ".pdf": (b"%PDF",)}
STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Receipt Extractor", version="1.0.0", lifespan=lifespan)


@app.post("/upload", response_model=ReceiptRecord, status_code=201)
def upload(file: UploadFile = File(...)):
    """Accept a receipt (.png/.jpg/.jpeg/.pdf), extract fields, persist, and return the stored record.
    Declared as a sync def so FastAPI runs the CPU/IO-heavy extraction in its threadpool, not the event loop."""
    filename = Path(file.filename or "upload").name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Use .png, .jpg, .jpeg or .pdf")

    data = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large (limit {config.MAX_UPLOAD_BYTES // 1024 // 1024} MB)")
    if not data.startswith(MAGIC[ext]):  # extension alone is not trusted
        raise HTTPException(400, "File content does not match its extension")

    try:
        receipt = extractor.extract(data, ext)
    except extractor.ExtractionError as e:
        raise HTTPException(422, str(e)) from e
    return db.insert(filename, receipt)


@app.get("/receipts", response_model=list[ReceiptRecord])
def list_receipts(limit: int = 200):
    return db.list_all(max(1, min(limit, 1000)))


@app.get("/receipts/{receipt_id}", response_model=ReceiptRecord)
def get_receipt(receipt_id: int):
    rec = db.get(receipt_id)
    if not rec:
        raise HTTPException(404, "Receipt not found")
    return rec


@app.get("/health")
def health():
    return {"status": "ok", "vlm_enabled": config.VLM_ENABLED}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
