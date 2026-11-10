# Receipt Extractor

Upload a receipt (photo or PDF) -> extract merchant, date, line items, tax/tip and total with **free-only** tooling -> store in SQLite -> browse in a web table. Includes a Part 2 multi-agent "Expense Dispatcher" (working code **and** an architecture write-up).

```
[ Web UI (static/index.html) ] -> [ FastAPI ] -> [ Extraction engine ] -> [ SQLite ]
   upload, status, table          POST /upload     PDF text / free VLM /     data/receipts.db
                                  GET /receipts    Tesseract + rules
```
<img width="959" height="412" alt="image" src="https://github.com/user-attachments/assets/876b0a02-0903-4726-967f-9b10f3f7ce44" />
<img width="620" height="80" alt="dispatcher-dry-run" src="https://github.com/user-attachments/assets/e68818a8-ab16-4643-93c4-440efbd9a829" />


## Proof of concept: results on the assessment receipts

Run with a free OpenRouter vision model enabled (`OPENROUTER_MODELS=openrouter/free`), local Tesseract as fallback. All three sample receipts pass the arithmetic check (`items + tax + tip = total`) and are shown as **OK** in the UI.

| Receipt | Merchant | Date | Items | Tax | Tip | Total | Check |
|---|---|---|---|---|---|---|---|
| `receipt_1.jpg` | The Corner Bistro | 2026-02-20 | 4 | $6.98 | $15.00 | $99.48 | OK |
| `receipt_2.jpg` | Target Stores | 2026-02-18 | 3 | $3.83 | - | $50.31 | OK |
| `receipt_3.jpg` | Quick-Mart Fuel & More | 2026-02-25 | 2 | $0.28 | - | $44.07 | OK |

Part 2 dispatcher over the same data (`--from 2026-02-01 --to 2026-02-28`):

```
[collector] 3 receipts in 2026-02-01..2026-02-28
[summarizer] totals {'USD': 193.86}, 0 flagged
[recipients] 2 valid, rejected: ['row 4: not-an-email']
[dispatcher] dry-run: asha.rao@example.com
[dispatcher] dry-run: vikram.shah@example.com
```

`193.86 = 44.07 + 50.31 + 99.48`. The invalid row in the Excel sheet was rejected, and the two valid recipients each got an email written to `part2/outbox/` (`.eml`, dry run by default).

**An honest note on receipt 3.** The stained fuel receipt with a signature across the TOTAL/TAX lines is the hard case. With the free vision model it extracts cleanly. With local Tesseract alone, results depend on the machine and Tesseract version: on one Windows setup the total and tax were not recovered, and the app correctly showed **Review** instead of saving a wrong number. That is the intended behaviour of the validation layer, and it is why the VLM path exists.


## Run it

Prerequisites: Python 3.10+, [Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html) and Poppler (`pdftotext`, `pdftoppm`).

```bash
# macOS: brew install tesseract poppler        Ubuntu: sudo apt install tesseract-ocr poppler-utils
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload          # open http://localhost:8000  (API docs: /docs)
```

Windows (PowerShell): install Tesseract and Poppler, add both to `PATH`, then `python -m venv .venv`, `.venv\Scripts\Activate.ps1`, `pip install -r requirements.txt`, `uvicorn app.main:app --reload`.

Or `docker build -t receipts . && docker run -p 8000:8000 receipts`.

Try it with the images in `samples/` (the three receipts from the assessment).

**Optional free vision model.** Copy `.env.example` to **`.env`** (the app reads `.env`, not `.env.example`) and set `OPENROUTER_API_KEY` (free account, free-tier models only, no paid keys). With a key the app asks a free VLM first and falls back to local OCR if the model is unavailable, rate-limited or unsure. Free model IDs get retired often, so `OPENROUTER_MODELS` defaults to the `openrouter/free` router, which picks a free model that accepts images; you can pin a specific ID from https://openrouter.ai/models?max_price=0. Check `/health` to see whether the VLM path is enabled. Without a key everything runs locally. Never commit `.env` (it is in `.gitignore`).

## API

| Method | Path | Notes |
|---|---|---|
| POST | `/upload` | multipart `file` (.png/.jpg/.jpeg/.pdf, <=10 MB). Extracts, saves, returns the stored record (201). 400 bad type or content, 413 too large, 422 unreadable. |
| GET | `/receipts` | Stored records, newest first. |
| GET | `/receipts/{id}` | One record. |
| GET | `/health` | Liveness + whether the VLM path is enabled. |

Stored schema: `id, filename, merchant_name, date, total_amount, extracted_json, created_at`. `extracted_json` holds the full structure (line items with quantity/unit price/line total, subtotal, tax, tip, currency, payment method, engine, confidence, warnings).

## How extraction works

1. **PDF with a text layer** -> `pdftotext` -> rules. Exact, no OCR noise.
2. **Image / scanned PDF** -> free VLM (if configured) -> validate. If the VLM is missing or unsure, continue:
3. **Local OCR** -> crop to the paper, upscale, 2-3 preprocessing variants x 2 Tesseract layouts -> rule-based parser on each.
4. **Checksum-guided merge.** A receipt is its own checksum: `items + tax + tip = total`. Different OCR variants garble different lines, so the merge pools every reading and keeps the combination of fields that actually adds up (including a subset-sum over line items).
5. **Validate everything.** Each result gets `confidence`, `warnings` and `needs_review`. The UI shows a "Review" badge instead of presenting a shaky number as fact.

Why this shape: OCR on phone photos is brittle (wood-grain backgrounds, stains, glare, signatures), VLMs are better but free tiers rate-limit and rotate, so neither alone is dependable. The arithmetic check lets the system know when it is wrong, which matters more than a few extra points of accuracy.

Decisions worth discussing:
- `price` on a line item is the **line total** as printed (`2x Caesar Salad $22.00` -> quantity 2, unit 11.00, price 22.00).
- Merchant names are normalised (`THE CORNER BISTRO` -> `The Corner Bistro`) so the VLM and OCR paths return the same casing.
- Ambiguous dates (`02/03/2026`) default to MM/DD; set `DATE_DAYFIRST=true` for DD/MM. Unambiguous ones (`25/02/2026`) are always read correctly.
- `/upload` is a sync endpoint so FastAPI runs the heavy OCR in its threadpool instead of blocking the event loop. At scale this becomes a job queue (below).
- Uploaded files are not stored; only extracted data is (less PII at rest).
- SQLite with one connection per call: simple, thread-safe, zero setup.

## Tests

```bash
pytest -q                                   # 21 tests; ~1-2 min because test_samples.py runs real OCR/VLM on the sample images
pytest -q --deselect tests/test_samples.py  # fast subset
```

Parser tests use noisy OCR text captured from the three sample receipts; API tests cover validation, errors and persistence; `test_samples.py` runs the full pipeline end to end on the sample images; `test_vlm.py` covers JSON recovery and normalising untrusted model output; `test_part2.py` covers the dispatcher (dry run, review gate, SMTP failure handling, dry-run-then-send, guard). The live OpenRouter call needs your key and is not exercised in CI.

## Known limits / next steps

- Single-column, English, Latin-script receipts; no multi-page or multi-receipt-per-image handling.
- Free VLMs vary between runs when using the `openrouter/free` router; the checksum validation and Tesseract fallback are the safety net.
- Perspective correction (4-point warp) would help skewed photos; the paper detector is colour-based and falls back to the full frame.
- Duplicate detection (hash of the file bytes, or merchant/date/total) so repeat uploads do not create repeat rows, expense categories, FX conversion for mixed currencies, edit/correct in the UI (and use corrections as an eval set), pagination, auth.
- Evaluation harness: a labelled folder of receipts with field-level accuracy per engine, so changing a prompt or preprocessing step is measurable.

---

## Part 2: Multi-Agent Architecture Design

Goal: turn extracted expenses into formal summaries and email them to recipients listed in an Excel sheet via Gmail/SMTP.

**Implemented (Option A)** in `part2/dispatcher.py` (`pytest tests/test_part2.py`), **designed (Option B)** below.

```
            +-----------+   +------------+   +-----------+   +----------+   +--------+   +------------+
 receipts ->| Collector |-->| Summarizer |-->| Recipient |-->| Composer |-->| Guard  |-->| Dispatcher |--> Gmail SMTP
 (SQLite)   | filter by |   | pure code: |   | agent:    |   | template |   | every  |   | idempotent |    (or .eml
            | period    |   | totals,    |   | read .xlsx|   | + opt.   |   | number |   | send log   |     in dry run)
            +-----------+   | by merchant|   | validate  |   | LLM intro|   | must   |   +------------+
                            +-----+------+   +-----------+   +----------+   | match  |
                                  |  needs_review > 0 ?                     +--------+
                                  +--> STOP: human reviews flagged receipts (or --allow-review)
```

| Agent | Responsibility | LLM? |
|---|---|---|
| Collector | Pull receipts for the period from the store | No |
| Summarizer | Totals per currency, by merchant, flagged list | No - arithmetic is never delegated to a model |
| Recipient agent | Read `name,email,role` from Excel; reject invalid/duplicate rows | No |
| Composer | Formal email from a template; one optional intro sentence from a free model, given facts but forbidden from stating numbers | Optional |
| Guard | Every amount in the email must come from the Summarizer; blocks the send otherwise | No |
| Dispatcher | Gmail SMTP (app password) or `.eml` dry run; records a send log per (recipient, summary hash) | No |

**Principles.** LLMs write prose; code does maths, selection and sending. State is one explicit run object, so every step is loggable and replayable. Dry run is the default, `--send` is opt-in. Unreviewed data does not get emailed: flagged receipts stop the run.

**Implemented failure handling.**
- A dry run never blocks a later real send: only a row with status `sent` counts as already dispatched.
- An SMTP error for one recipient is logged as `FAILED` and does not stop the others. A failed send is not recorded as sent, so re-running the command retries it.
- A real send needs `SMTP_USER` and `SMTP_APP_PASSWORD`; without them the run stops with a clear error.

**Designed, not yet implemented.** Retry with exponential backoff and a dead-letter table for permanently failing recipients; OAuth with `gmail.send` scope only instead of an app password.

**Security.** Gmail credentials live in env/secret manager, never in the repo; recipients come only from the sheet (the LLM never chooses addresses); email bodies contain merchant and totals only, no card digits.

**Scaling path.** Upload -> queue (Redis + Celery/Arq) -> extraction workers; dispatch becomes a scheduled job (weekly/monthly) fanning out one task per recipient. Postgres replaces SQLite; object storage if originals must be retained.

**Framework mapping.** The flow is deterministic, so plain functions are enough today. If it grows branches (approval loops, per-department routing, retries with human input), the same agents map directly to LangGraph nodes with the run object as graph state; CrewAI/AutoGen fit better when agents must negotiate open-ended tasks, which this workflow deliberately avoids.

**Ideas to extend.** Policy agent (per-diem limits, missing receipts, duplicates), categorisation agent (LLM proposes, rules verify), anomaly explanations, approve/reject links in the email, Slack/Teams channel, per-employee summaries once receipts carry an owner, FX normalisation.

Run Option A:

```bash
python part2/make_sample_recipients.py
python part2/dispatcher.py --from 2026-02-01 --to 2026-02-28 --recipients part2/recipients.xlsx   # dry run: writes part2/outbox/*.eml
# real send (optional): set SMTP_USER and SMTP_APP_PASSWORD, put your own address in recipients.xlsx, add --send
```
