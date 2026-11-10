import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DB_PATH", ROOT / "data" / "receipts.db"))
MAX_UPLOAD_BYTES = int(float(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024)
DATE_DAYFIRST = os.getenv("DATE_DAYFIRST", "false").lower() in {"1", "true", "yes"}

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODELS = [
    m.strip()
    for m in os.getenv(
        "OPENROUTER_MODELS",
        "openrouter/free",
    ).split(",")
    if m.strip()
]
# A VLM is only attempted when a (free) key is configured; otherwise the local OCR path is used.
VLM_ENABLED = bool(OPENROUTER_API_KEY)
# Below this confidence a VLM answer is cross-checked against the local OCR pipeline.
VLM_ACCEPT_CONFIDENCE = 0.8
