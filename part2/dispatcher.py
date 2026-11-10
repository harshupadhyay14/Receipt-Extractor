"""Part 2 (Option A): Expense Dispatcher - a small multi-agent pipeline on top of the receipt store.

    Collector -> Summarizer -> Recipient agent -> Composer -> Guard -> Dispatcher
    (DB)         (pure code)   (Excel sheet)      (template   (checks   (SMTP / Gmail,
                                                   + optional  numbers)  dry-run by default)
                                                   LLM prose)

Design rule: LLMs write prose, code does arithmetic, selection and sending. Every number in an outgoing
email comes from the summarizer; the Guard re-checks that no other amounts slipped into the text.
Plain Python "agents" (typed functions + one shared run state) instead of a framework: the flow is
deterministic, so a framework would add dependencies and hide the control flow without adding capability.
See README "Part 2" for how this maps onto LangGraph / CrewAI if branching and retries grow.

Usage:
    python part2/make_sample_recipients.py
    python part2/dispatcher.py --from 2026-02-01 --to 2026-02-28 --recipients part2/recipients.xlsx          # dry run
    SMTP_USER=you@gmail.com SMTP_APP_PASSWORD=xxxx python part2/dispatcher.py ... --send                    # real send
"""
import argparse
import hashlib
import os
import re
import smtplib
import sys
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, db  # noqa: E402
from app.models import ReceiptRecord  # noqa: E402

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OUTBOX = Path(__file__).resolve().parent / "outbox"
SYMBOL = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}


@dataclass
class Recipient:
    name: str
    email: str
    role: str = "finance"


@dataclass
class Summary:
    date_from: str
    date_to: str
    receipts: list[ReceiptRecord]
    totals: dict[str, float]  # per currency
    by_merchant: dict[str, float]
    flagged: list[ReceiptRecord]

    @property
    def key(self) -> str:  # same receipts + same amounts => same key => idempotent sends
        raw = "|".join(f"{r.id}:{r.total_amount}" for r in sorted(self.receipts, key=lambda r: r.id))
        return hashlib.sha256(f"{self.date_from}{self.date_to}{raw}".encode()).hexdigest()[:16]


@dataclass
class RunState:
    log: list[str] = field(default_factory=list)

    def say(self, agent: str, msg: str) -> None:
        self.log.append(f"[{agent}] {msg}")
        print(self.log[-1])


# ---------------- agents ----------------
def collector(date_from: str, date_to: str) -> list[ReceiptRecord]:
    db.init()
    return [r for r in db.list_all(1000) if r.date and date_from <= r.date <= date_to]


def summarizer(receipts: list[ReceiptRecord], date_from: str, date_to: str) -> Summary:
    totals: dict[str, float] = {}
    merchants: dict[str, float] = {}
    for r in receipts:
        cur = r.extracted.currency or "USD"
        totals[cur] = round(totals.get(cur, 0) + (r.total_amount or 0), 2)
        name = r.merchant_name or "Unknown"
        merchants[name] = round(merchants.get(name, 0) + (r.total_amount or 0), 2)
    flagged = [r for r in receipts if r.extracted.needs_review]
    return Summary(date_from, date_to, receipts, totals, dict(sorted(merchants.items(), key=lambda kv: -kv[1])), flagged)


def recipient_agent(xlsx: Path) -> tuple[list[Recipient], list[str]]:
    """Reads name/email/role columns. Invalid or duplicate rows are rejected loudly, never sent to."""
    ws = openpyxl.load_workbook(xlsx, read_only=True, data_only=True).active
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h or "").strip().lower() for h in rows[0]]
    idx = {h: header.index(h) for h in ("name", "email") if h in header}
    if len(idx) < 2:
        raise ValueError("Recipient sheet needs 'name' and 'email' columns")
    role_i = header.index("role") if "role" in header else None
    ok, rejected, seen = [], [], set()
    for n, row in enumerate(rows[1:], start=2):
        email = str(row[idx["email"]] or "").strip().lower()
        if not EMAIL_RE.match(email) or email in seen:
            rejected.append(f"row {n}: {email or '(blank)'}")
            continue
        seen.add(email)
        ok.append(Recipient(str(row[idx["name"]] or email).strip(), email, str(row[role_i] or "finance").strip() if role_i is not None else "finance"))
    return ok, rejected


def _money(v: float, cur: str) -> str:
    return f"{SYMBOL.get(cur, '')}{v:,.2f}"


def _llm_intro(s: Summary) -> str | None:
    """Optional free-tier LLM writes ONE intro sentence from facts we hand it. Numbers stay ours."""
    if not config.VLM_ENABLED:
        return None
    import httpx
    facts = f"{len(s.receipts)} receipts, {s.date_from} to {s.date_to}, top merchant {next(iter(s.by_merchant), 'n/a')}"
    try:
        r = httpx.post("https://openrouter.ai/api/v1/chat/completions", timeout=30,
                       headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
                       json={"model": config.OPENROUTER_MODELS[0], "temperature": 0.2, "messages": [{"role": "user", "content":
                             f"Write one formal sentence introducing an expense summary. Facts: {facts}. Do not include any numbers or amounts."}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip().splitlines()[0]
    except Exception:
        return None


def composer(s: Summary, to: Recipient, use_llm: bool = False) -> str:
    intro = (_llm_intro(s) if use_llm else None) or "Please find the expense summary for the period below."
    lines = [f"Dear {to.name},", "", intro, "", f"Period: {s.date_from} to {s.date_to}", f"Receipts: {len(s.receipts)}"]
    lines += [f"Total ({cur}): {_money(v, cur)}" for cur, v in s.totals.items()]
    lines += ["", "By merchant:"]
    lines += [f"  - {m}: {_money(v, next(iter(s.totals)))}" for m, v in s.by_merchant.items()]
    lines += ["", "Receipts:"]
    lines += [f"  - {r.date}  {r.merchant_name or 'Unknown'}  {_money(r.total_amount or 0, r.extracted.currency or 'USD')}" for r in s.receipts]
    if s.flagged:
        lines += ["", f"Needs manual review ({len(s.flagged)}):"]
        lines += [f"  - #{r.id} {r.merchant_name or r.filename}: {'; '.join(r.extracted.warnings)}" for r in s.flagged]
    lines += ["", "Regards,", "Expense Dispatcher"]
    return "\n".join(lines)


def guard(s: Summary, body: str) -> list[str]:
    """Every amount in the email must be one the summarizer produced. Catches LLM-introduced numbers."""
    allowed = {f"{v:,.2f}" for v in list(s.totals.values()) + list(s.by_merchant.values())}
    allowed |= {f"{(r.total_amount or 0):,.2f}" for r in s.receipts}
    body_wo_text = re.sub(r"#\d+|\b\d{4}-\d{2}-\d{2}\b", "", body)
    found = set(re.findall(r"\d[\d,]*\.\d{2}", body_wo_text))
    # warnings quote arithmetic (e.g. 'add up to 43.79'), so only enforce on the structured part of the email
    structured = body_wo_text.split("Needs manual review")[0]
    return [f"Unexpected amount {a}" for a in sorted(set(re.findall(r"\d[\d,]*\.\d{2}", structured)) - allowed)] if found else []


def _ensure_log(c) -> None:
    c.execute("CREATE TABLE IF NOT EXISTS dispatch_log (recipient TEXT, period_key TEXT, status TEXT, "
              "created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(recipient, period_key))")


def _already_sent(email: str, key: str) -> bool:
    with db.connect() as c:
        _ensure_log(c)
        return c.execute(
            "SELECT 1 FROM dispatch_log WHERE recipient=? AND period_key=? AND status='sent'",
            (email, key),
        ).fetchone() is not None


def _mark_sent(email: str, key: str, status: str) -> None:
    with db.connect() as c:
        _ensure_log(c)
        c.execute(
            "INSERT INTO dispatch_log (recipient, period_key, status) VALUES (?,?,?) "
            "ON CONFLICT(recipient, period_key) DO UPDATE SET status=excluded.status",
            (email, key, status),
        )


def dispatcher(s: Summary, to: Recipient, body: str, send: bool, state: RunState) -> str:
    if _already_sent(to.email, s.key):
        state.say("dispatcher", f"skip {to.email}: this summary was already dispatched")
        return "skipped"
    msg = EmailMessage()
    msg["Subject"] = f"Expense summary {s.date_from} to {s.date_to}"
    msg["To"] = to.email
    msg["From"] = os.environ.get("SMTP_USER", "expenses@example.com")
    msg.set_content(body)
    if send:
        user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_APP_PASSWORD")
        if not (user and pw):
            raise RuntimeError("Set SMTP_USER and SMTP_APP_PASSWORD (a Gmail app password) to send for real")
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                smtp.login(user, pw)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            state.say("dispatcher", f"FAILED {to.email}: {type(e).__name__}: {e}")
            return "failed"  # not recorded as sent, so a re-run retries it
        status = "sent"
    else:
        OUTBOX.mkdir(exist_ok=True)
        (OUTBOX / f"{s.key}_{to.email}.eml").write_bytes(bytes(msg))
        status = "dry-run"
    _mark_sent(to.email, s.key, status)
    state.say("dispatcher", f"{status}: {to.email}")
    return status


# ---------------- orchestrator ----------------
def run(date_from: str, date_to: str, recipients_xlsx: Path, send: bool = False, use_llm: bool = False,
        allow_review: bool = False) -> RunState:
    state = RunState()
    receipts = collector(date_from, date_to)
    state.say("collector", f"{len(receipts)} receipts in {date_from}..{date_to}")
    if not receipts:
        state.say("orchestrator", "nothing to send")
        return state
    summary = summarizer(receipts, date_from, date_to)
    state.say("summarizer", f"totals {summary.totals}, {len(summary.flagged)} flagged")
    if summary.flagged and not allow_review:  # human-in-the-loop gate: never email numbers we aren't sure of
        state.say("orchestrator", f"STOP: {len(summary.flagged)} receipt(s) need review. Fix them or pass --allow-review.")
        return state
    recipients, rejected = recipient_agent(recipients_xlsx)
    state.say("recipients", f"{len(recipients)} valid, rejected: {rejected or 'none'}")
    for r in recipients:
        body = composer(summary, r, use_llm)
        if problems := guard(summary, body):
            state.say("guard", f"blocked email to {r.email}: {problems}")
            continue
        dispatcher(summary, r, body, send, state)
    return state


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from", required=True)
    ap.add_argument("--to", dest="date_to", required=True)
    ap.add_argument("--recipients", type=Path, required=True)
    ap.add_argument("--send", action="store_true", help="really send via Gmail SMTP (default: write .eml files to part2/outbox)")
    ap.add_argument("--llm", action="store_true", help="let a free OpenRouter model write the intro sentence")
    ap.add_argument("--allow-review", action="store_true", help="send even if some receipts are flagged for review")
    a = ap.parse_args()
    run(a.date_from, a.date_to, a.recipients, a.send, a.llm, a.allow_review)
