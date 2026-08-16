"""
SlippiesBot V2 — Beta Test Bot
--------------------------------
Standalone test bot. Reads config from .env (TELEGRAM_BOT_TOKEN,
GEMINI_API_KEY, DATABASE_PATH). Completely separate from V1 —
different bot token, different local database file.

Run with: python3 bot_beta.py
"""

import os
import io
import sqlite3
import logging
from datetime import datetime, timedelta

import openpyxl
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------
# Config
# ------------------------------------------------------------
load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
ADMIN_SECRET = os.environ["ADMIN_SECRET"]
DATABASE_PATH = os.environ.get("DATABASE_PATH", "./data/beta_test.db")
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL_NAME = "gemini-3.1-flash-lite"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# DB helpers — every query is scoped to the calling user's id.
# This is the actual security boundary discussed earlier: Gemini
# never sees more than one user's data in a single call because
# we never assemble a cross-user prompt in the first place.
# ------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema():
    """Creates all tables if they don't exist yet — safe to run every startup,
    since every CREATE statement in Schema.sql uses IF NOT EXISTS."""
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Schema.sql")
    conn = sqlite3.connect(DATABASE_PATH)
    with open(schema_path) as f:
        conn.executescript(f.read())
    conn.commit()
    conn.close()
    logger.info(f"Schema verified/initialized at {DATABASE_PATH}")


def ensure_user(user_id: int, username: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO users (user_id, username)
           VALUES (?, ?)
           ON CONFLICT(user_id) DO NOTHING""",
        (user_id, username),
    )
    conn.commit()
    conn.close()


def license_code_valid(code: str) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT code FROM license_codes WHERE code = ? AND active = 1", (code,)
    ).fetchone()
    conn.close()
    return row is not None


def create_license_code(code: str, label: str = ""):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO license_codes (code, label) VALUES (?, ?)",
        (code, label),
    )
    conn.commit()
    conn.close()


def log_user_in(user_id: int, code: str):
    conn = get_db()
    conn.execute(
        """UPDATE users SET license_code = ?, logged_in_at = ?
           WHERE user_id = ?""",
        (code, datetime.now().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def is_logged_in(user_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT license_code FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None and row["license_code"] is not None


def insert_receipt_and_item(user_id: int, merchant: str, amount: float,
                             category: str, description: str, source: str = "telegram_text"):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO receipts (user_id, merchant, total_amount, purchased_at, source)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, merchant, amount, datetime.now().isoformat(), source),
    )
    receipt_id = cur.lastrowid
    conn.execute(
        """INSERT INTO receipt_items (receipt_id, description, category, amount, category_source)
           VALUES (?, ?, ?, ?, 'model')""",
        (receipt_id, description, category, amount),
    )
    conn.commit()
    conn.close()
    return receipt_id


def get_recent_summary(user_id: int, limit: int = 10) -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT merchant, total_amount, purchased_at
           FROM receipts WHERE user_id = ?
           ORDER BY purchased_at DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def touch_user_activity(user_id: int):
    """Called on every incoming message — powers the 2-day check-in nudge."""
    conn = get_db()
    conn.execute(
        """INSERT INTO user_activity (user_id, last_message_at)
           VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET last_message_at = excluded.last_message_at""",
        (user_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_category_spend(user_id: int, category: str, days: int = 30) -> dict:
    """Sums spend for a category over the trailing N days."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    row = conn.execute(
        """SELECT COALESCE(SUM(ri.amount), 0) as total, COUNT(*) as cnt
           FROM receipt_items ri
           JOIN receipts r ON ri.receipt_id = r.receipt_id
           WHERE r.user_id = ? AND ri.category = ? AND r.purchased_at >= ?""",
        (user_id, category, cutoff),
    ).fetchone()
    conn.close()
    return {"total": row["total"], "count": row["cnt"]}


def has_import(user_id: int) -> bool:
    """True if this user has ever had a bulk import land (source='bulk_import')."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM receipts WHERE user_id = ? AND source = 'bulk_import' LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row is not None


def get_import_ask_status(user_id: int) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT asked_at, fulfilled FROM pending_imports WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_import_asked(user_id: int):
    conn = get_db()
    conn.execute(
        """INSERT INTO pending_imports (user_id, asked_at) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET asked_at = excluded.asked_at""",
        (user_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def mark_import_fulfilled(user_id: int):
    conn = get_db()
    conn.execute(
        """INSERT INTO pending_imports (user_id, fulfilled) VALUES (?, 1)
           ON CONFLICT(user_id) DO UPDATE SET fulfilled = 1""",
        (user_id,),
    )
    conn.commit()
    conn.close()


def bulk_insert_receipts(user_id: int, rows: list, batch_id: str) -> int:
    """rows: list of dicts with merchant, amount, purchased_at, category.
    Writes both a receipts row AND a matching receipt_items row (with the
    locally-derived category) so bulk-imported spend shows up correctly
    in category queries — bank exports don't have line-item detail, but
    they should still count toward category totals."""
    conn = get_db()
    inserted = 0
    for row in rows:
        cur = conn.execute(
            """INSERT INTO receipts (user_id, merchant, total_amount, purchased_at, source, import_batch_id)
               VALUES (?, ?, ?, ?, 'bulk_import', ?)""",
            (user_id, row["merchant"], row["amount"], row["purchased_at"], batch_id),
        )
        receipt_id = cur.lastrowid
        conn.execute(
            """INSERT INTO receipt_items (receipt_id, description, category, amount, category_source)
               VALUES (?, ?, ?, ?, 'model')""",
            (receipt_id, row["merchant"], row.get("category", "other"), row["amount"]),
        )
        inserted += 1
    conn.commit()
    conn.close()
    return inserted


def get_users_due_import_reminder(days: int = 90) -> list:
    """Users who are logged in, still have no import, and haven't been
    asked in the last N days (default 3 months)."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT u.user_id FROM users u
           LEFT JOIN pending_imports pi ON u.user_id = pi.user_id
           WHERE u.license_code IS NOT NULL
           AND (pi.fulfilled IS NULL OR pi.fulfilled = 0)
           AND (pi.asked_at IS NULL OR pi.asked_at < ?)
           AND NOT EXISTS (
               SELECT 1 FROM receipts r WHERE r.user_id = u.user_id AND r.source = 'bulk_import'
           )""",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_all_user_ids() -> list:
    conn = get_db()
    rows = conn.execute("SELECT user_id FROM users WHERE nudge_opt_in = 1").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def get_inactive_users(hours: int = 48) -> list:
    conn = get_db()
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    today = datetime.now().date().isoformat()
    rows = conn.execute(
        """SELECT ua.user_id FROM user_activity ua
           JOIN users u ON ua.user_id = u.user_id
           WHERE ua.last_message_at < ? AND u.nudge_opt_in = 1
           AND (ua.last_checkin_sent IS NULL OR ua.last_checkin_sent < ?)""",
        (cutoff, today),
    ).fetchall()
    conn.close()
    return [r["user_id"] for r in rows]


def mark_checkin_sent(user_id: int):
    conn = get_db()
    conn.execute(
        "UPDATE user_activity SET last_checkin_sent = ? WHERE user_id = ?",
        (datetime.now().date().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def detect_recurring_transactions(user_id: int):
    """Groups past receipts by merchant, checks for a similar amount
    landing on a similar day-of-month across 2+ separate months."""
    conn = get_db()
    rows = conn.execute(
        """SELECT merchant, total_amount, purchased_at FROM receipts
           WHERE user_id = ? AND merchant IS NOT NULL
           ORDER BY merchant, purchased_at""",
        (user_id,),
    ).fetchall()

    by_merchant = {}
    for r in rows:
        by_merchant.setdefault(r["merchant"], []).append(r)

    for merchant, txns in by_merchant.items():
        if len(txns) < 2:
            continue
        months_seen = set()
        days = []
        amounts = []
        for t in txns:
            dt = datetime.fromisoformat(t["purchased_at"])
            months_seen.add((dt.year, dt.month))
            days.append(dt.day)
            amounts.append(t["total_amount"])

        if len(months_seen) < 2:
            continue
        avg_amount = sum(amounts) / len(amounts)
        if max(amounts) > avg_amount * 1.1 or min(amounts) < avg_amount * 0.9:
            continue  # amounts too inconsistent to call it "recurring"

        typical_day = round(sum(days) / len(days))
        last_seen = max(t["purchased_at"] for t in txns)

        # Manual upsert (no UNIQUE constraint declared on user_id+merchant)
        existing = conn.execute(
            "SELECT recurring_id FROM recurring_transactions WHERE user_id = ? AND merchant = ?",
            (user_id, merchant),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE recurring_transactions
                   SET typical_amount = ?, typical_day = ?, last_seen = ?
                   WHERE recurring_id = ?""",
                (avg_amount, typical_day, last_seen, existing["recurring_id"]),
            )
        else:
            conn.execute(
                """INSERT INTO recurring_transactions
                   (user_id, merchant, typical_amount, typical_day, last_seen)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, merchant, avg_amount, typical_day, last_seen),
            )
    conn.commit()
    conn.close()


def get_due_recurring_reminders() -> list:
    """Recurring items whose typical day is tomorrow, not yet reminded this cycle."""
    conn = get_db()
    tomorrow = (datetime.now() + timedelta(days=1)).day
    today_str = datetime.now().date().isoformat()
    rows = conn.execute(
        """SELECT * FROM recurring_transactions
           WHERE active = 1 AND typical_day = ?
           AND (last_reminded_at IS NULL OR last_reminded_at < ?)""",
        (tomorrow, today_str),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_recurring_reminded(recurring_id: int):
    conn = get_db()
    conn.execute(
        "UPDATE recurring_transactions SET last_reminded_at = ? WHERE recurring_id = ?",
        (datetime.now().date().isoformat(), recurring_id),
    )
    conn.commit()
    conn.close()


# ------------------------------------------------------------
# Gemini helpers
# ------------------------------------------------------------
INTENT_PROMPT = """Classify this Telegram message into exactly one category:
- "log_transaction": user is telling you about money they spent
- "category_query": user is asking how much they spent on something (e.g. "how much on groceries this month")
- "request_file": user wants their transaction history as a downloadable file/Excel
- "request_report": user wants a general summary of recent spending
- "other": anything else (greeting, question, unrelated)

Message: "{message}"

Reply with ONLY one word: log_transaction, category_query, request_file, request_report, or other."""

PARSE_TRANSACTION_PROMPT = """Extract the transaction details from this message.
Reply in EXACTLY this format, nothing else:
merchant: <merchant or vendor name, or "unknown">
amount: <numeric amount only, no currency symbol>
category: <one of: groceries, eating_out, transport, household, entertainment, health, other>
description: <short description of what was bought>

Message: "{message}"
"""

PARSE_RECEIPT_PHOTO_PROMPT = """This is a photo of a purchase receipt or slip.
Extract the details and reply in EXACTLY this format, nothing else:
merchant: <vendor/store name>
amount: <total numeric amount only, no currency symbol>
category: <one of: groceries, eating_out, transport, household, entertainment, health, other>
description: <brief summary of main items, or the merchant name if items aren't legible>

If any field is unclear from the image, make your best reasonable guess rather than leaving it blank."""

CATEGORY_QUERY_PROMPT = """The user is asking how much they spent on something.
Reply in EXACTLY this format, nothing else:
category: <one of: groceries, eating_out, transport, household, entertainment, health, other, or "all" if not specific>
days: <number of days to look back — 7 for "this week", 30 for "this month", 365 for "this year", 30 if unclear>

Message: "{message}"
"""


def build_excel_export(user_id: int) -> io.BytesIO:
    """Builds an in-memory .xlsx of a user's full transaction history."""
    conn = get_db()
    rows = conn.execute(
        """SELECT purchased_at, merchant, total_amount, source
           FROM receipts WHERE user_id = ? ORDER BY purchased_at DESC""",
        (user_id,),
    ).fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Date", "Merchant", "Amount (ZAR)", "Source"])
    for r in rows:
        ws.append([r["purchased_at"][:10], r["merchant"], r["total_amount"], r["source"]])

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer


import re

CATEGORY_KEYWORDS = {
    "groceries": ["spar", "pnp", "pick n pay", "checkers", "woolworths", "kwikspar",
                  "food lover", "boxer", "fruit", "dischem", "clicks"],
    "eating_out": ["kfc", "steers", "nandos", "mcd", "debonairs", "wimpy", "vida",
                   "mugg", "milky lane", "poke", "yoco", "cafe", "coffee", "restaur",
                   "pizza", "uber eats", "mr d", "taste", "ginos", "simplygreek",
                   "motherdough", "bootleggers", "fat cactus"],
    "transport": ["shell", "engen", "bp ", "sasol", "total", "fuel", "petrol",
                  "uber", "bolt", "railway", "gautrain", "toll"],
    "health": ["dischem", "clicks", "pharmacy", "dr ", "doctor", "medical", "dentist"],
    "entertainment": ["sterkinekor", "ster kinekor", "steamgames", "playtomic",
                       "matiesgym", "gym", "movie", "netflix", "showmax", "spotify"],
    "household": ["takealot", "gadget", "gadgettime", "evetech", "microsoft",
                   "vodacom", "mtn", "cell c", "prepaid mobile", "vodashop"],
}


def categorize_locally(description: str, type_suffix: str = "") -> str:
    """Fast keyword-based categorization for bulk imports — avoids an
    expensive per-row Gemini call across potentially hundreds of rows."""
    type_lower = type_suffix.lower()
    # Internal account transfers aren't real spending — tag separately
    # so they don't inflate category totals, but keep the data (in case
    # the person wants to see it later).
    if "ib transfer" in type_lower or "transfer to" in type_lower:
        return "transfer"
    if "cash withdrawal" in type_lower:
        return "cash_withdrawal"

    desc_lower = description.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            return category
    return "other"


def clean_merchant_description(raw: str) -> str:
    """Strips the trailing ' - debit card purchase' style suffix and
    card/date noise like '5196*5223 18 MAY' from a raw bank description."""
    if not raw:
        return "unknown"
    # Drop the ' - <transaction type>' suffix
    name = raw.split(" - ")[0].strip()
    # Drop trailing masked-card + date pattern, e.g. "5196*5223 18 MAY"
    name = re.sub(r"\s+\d{4}\*\d{4}(\s+\d{1,2}\s+[A-Za-z]{3})?\s*$", "", name)
    # Collapse repeated whitespace
    name = re.sub(r"\s+", " ", name).strip()
    return name or "unknown"


def parse_bulk_excel(file_bytes: bytes) -> list:
    """Parses a Standard Bank-style transaction export:
    columns Date | Description | In (R) | Out (R) | Bank fees (R) | Balance (R).
    Dates have no year in the row itself — the year appears as its own
    header row (e.g. a lone '2026') partway through the sheet.
    Only outflow (Out + Bank fees) rows are imported as spend; pure
    deposits/income rows are skipped, since this bot tracks spending."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active

    # Locate header row and map column names flexibly
    header_row_idx = None
    headers = []
    for row in ws.iter_rows(min_row=1, max_row=5):
        values = [str(c.value).strip().lower() if c.value else "" for c in row]
        if any("date" in v for v in values):
            header_row_idx = row[0].row
            headers = values
            break

    if header_row_idx is None:
        return []

    date_col = next((i for i, h in enumerate(headers) if "date" in h), None)
    desc_col = next((i for i, h in enumerate(headers) if "description" in h), None)
    out_col = next((i for i, h in enumerate(headers) if "out" in h), None)
    fee_col = next((i for i, h in enumerate(headers) if "fee" in h), None)

    if date_col is None or desc_col is None:
        return []

    results = []
    current_year = datetime.now().year  # fallback if no year row is ever found

    for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
        date_val = row[date_col] if date_col < len(row) else None
        desc_val = row[desc_col] if desc_col < len(row) else None

        # A lone 4-digit year row (e.g. "2026") sets context, not a transaction
        if date_val and str(date_val).strip().isdigit() and len(str(date_val).strip()) == 4:
            current_year = int(str(date_val).strip())
            continue

        if not date_val or not desc_val:
            continue

        out_val = row[out_col] if out_col is not None and out_col < len(row) else None
        fee_val = row[fee_col] if fee_col is not None and fee_col < len(row) else None

        out_amount = abs(out_val) if isinstance(out_val, (int, float)) else 0
        fee_amount = abs(fee_val) if isinstance(fee_val, (int, float)) else 0
        total_amount = out_amount + fee_amount

        if total_amount <= 0:
            continue  # skip deposits/income rows — not spend

        try:
            purchased_at = datetime.strptime(f"{str(date_val).strip()} {current_year}", "%d %b %Y").isoformat()
        except ValueError:
            continue  # unparseable date, skip rather than guess

        merchant = clean_merchant_description(str(desc_val))
        type_suffix = str(desc_val).split(" - ")[-1].strip() if " - " in str(desc_val) else ""
        category = categorize_locally(merchant, type_suffix)

        results.append({
            "merchant": merchant,
            "amount": total_amount,
            "purchased_at": purchased_at,
            "category": category,
        })
    return results


async def classify_intent(message_text: str) -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=INTENT_PROMPT.format(message=message_text),
    )
    intent = response.text.strip().lower()
    valid = ("log_transaction", "category_query", "request_file", "request_report", "other")
    if intent not in valid:
        return "other"
    return intent


async def parse_category_query(message_text: str) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=CATEGORY_QUERY_PROMPT.format(message=message_text),
    )
    lines = response.text.strip().split("\n")
    parsed = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


async def parse_transaction(message_text: str) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=PARSE_TRANSACTION_PROMPT.format(message=message_text),
    )
    lines = response.text.strip().split("\n")
    parsed = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


async def parse_receipt_photo(image_bytes: bytes) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=[
            PARSE_RECEIPT_PHOTO_PROMPT,
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
        ],
    )
    lines = response.text.strip().split("\n")
    parsed = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


# ------------------------------------------------------------
# Telegram handlers
# ------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    if is_logged_in(user.id):
        await update.message.reply_text(
            "Welcome back — SlippiesBot beta is ready. Send a receipt photo or "
            "just tell me what you spent, e.g. \"spent R150 on lunch at Nandos\"."
        )
    else:
        await update.message.reply_text(
            "SlippiesBot beta is ONLINE.\n"
            "Use /login YOURCODE to get started."
        )


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    touch_user_activity(user.id)

    if not context.args:
        await update.message.reply_text("Usage: /login YOURCODE")
        return

    code = context.args[0].upper()
    if license_code_valid(code):
        log_user_in(user.id, code)
        await update.message.reply_text(f"✅ Access granted — logged in with {code}.")

        if not has_import(user.id):
            mark_import_asked(user.id)
            await update.message.reply_text(
                "📎 Got a spreadsheet of your last 3 months of spending? Upload it here "
                "as a file and I'll import it — gives me a real baseline right away "
                "instead of starting from zero. Totally optional, just send a receipt "
                "or transaction whenever you're ready if you'd rather skip this."
            )
    else:
        await update.message.reply_text("❌ License code not found.")


async def addcode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /addcode NEWCODE123 admin_secret [optional_label]
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /addcode NEWCODE123 your_admin_secret [optional_label]"
        )
        return

    new_code = context.args[0].upper()
    provided_secret = context.args[1]

    if provided_secret != ADMIN_SECRET:
        await update.message.reply_text("❌ Invalid admin secret.")
        return

    label = context.args[2] if len(context.args) >= 3 else ""
    create_license_code(new_code, label)

    # Try to delete the message so the admin secret doesn't sit in chat history
    try:
        await update.message.delete()
    except Exception:
        pass

    await update.message.reply_text(f"✅ New code created: {new_code}")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    touch_user_activity(user.id)

    if not is_logged_in(user.id):
        await update.message.reply_text("🔒 Locked. Please /login YOURCODE first.")
        return

    message_text = update.message.text

    intent = await classify_intent(message_text)
    logger.info(f"user={user.id} intent={intent} msg={message_text!r}")

    if intent == "log_transaction":
        parsed = await parse_transaction(message_text)
        try:
            amount = float(parsed.get("amount", "0"))
        except ValueError:
            await update.message.reply_text(
                "Couldn't quite catch the amount there — try again with a number, "
                "e.g. \"spent 150 on lunch\"."
            )
            return

        insert_receipt_and_item(
            user_id=user.id,
            merchant=parsed.get("merchant", "unknown"),
            amount=amount,
            category=parsed.get("category", "other"),
            description=parsed.get("description", message_text),
            source="telegram_text",
        )
        await update.message.reply_text(
            f"Logged: R{amount:.2f} — {parsed.get('category', 'other')} "
            f"({parsed.get('merchant', 'unknown')})"
        )
        detect_recurring_transactions(user.id)

    elif intent == "category_query":
        parsed = await parse_category_query(message_text)
        category = parsed.get("category", "all")
        try:
            days = int(parsed.get("days", "30"))
        except ValueError:
            days = 30

        if category == "all":
            conn = get_db()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            row = conn.execute(
                """SELECT COALESCE(SUM(ri.amount),0) as total, COUNT(*) as cnt
                   FROM receipt_items ri
                   JOIN receipts r ON ri.receipt_id = r.receipt_id
                   WHERE r.user_id = ? AND r.purchased_at >= ?
                   AND ri.category NOT IN ('transfer', 'cash_withdrawal')""",
                (user.id, cutoff),
            ).fetchone()
            conn.close()
            await update.message.reply_text(
                f"You've spent R{row['total']:.2f} in total over the last {days} days, across {row['cnt']} transactions. "
                f"(Internal transfers and cash withdrawals aren't counted as spend.)"
            )
        else:
            result = get_category_spend(user.id, category, days)
            if result["count"] == 0:
                await update.message.reply_text(f"No {category} spending found in the last {days} days.")
            else:
                await update.message.reply_text(
                    f"You've spent R{result['total']:.2f} on {category} in the last {days} days "
                    f"({result['count']} item{'s' if result['count'] != 1 else ''})."
                )

    elif intent == "request_file":
        buffer = build_excel_export(user.id)
        await update.message.reply_document(
            document=buffer,
            filename=f"slippies_transactions_{user.id}.xlsx",
            caption="📊 Here's your full transaction history."
        )

    elif intent == "request_report":
        rows = get_recent_summary(user.id)
        if not rows:
            await update.message.reply_text("📭 Nothing logged yet — nothing to report.")
            return
        lines = [f"• R{r['total_amount']:.2f} — {r['merchant']} ({r['purchased_at'][:10]})"
                 for r in rows]
        await update.message.reply_text("📊 Your recent transactions:\n" + "\n".join(lines))

    else:
        await update.message.reply_text(
            "I mostly understand spending updates right now — try something like "
            "\"spent R80 on groceries\", \"how much did I spend on transport this month\", "
            "or send a receipt photo."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    touch_user_activity(user.id)

    if not is_logged_in(user.id):
        await update.message.reply_text("🔒 Locked. Please /login YOURCODE first.")
        return

    doc = update.message.document
    if not (doc.file_name or "").lower().endswith((".xlsx", ".xls")):
        await update.message.reply_text("I can only import .xlsx or .xls files right now.")
        return

    status_msg = await update.message.reply_text("Reading your file... 📖")
    try:
        file = await doc.get_file()
        file_bytes = bytes(await file.download_as_bytearray())

        rows = parse_bulk_excel(file_bytes)
        if not rows:
            await status_msg.edit_text(
                "❌ Couldn't find recognizable date/amount columns in that file. "
                "Make sure it has headers like 'Date' and 'Amount' somewhere in the first few rows."
            )
            return

        batch_id = f"{user.id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        inserted = bulk_insert_receipts(user.id, rows, batch_id)
        mark_import_fulfilled(user.id)
        detect_recurring_transactions(user.id)

        await status_msg.edit_text(
            f"✅ Imported {inserted} transactions from your file. "
            f"I'll use this as your baseline going forward."
        )
    except Exception as e:
        logger.error(f"Import failed for user={user.id}: {e}")
        await status_msg.edit_text(f"❌ Couldn't process that file: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    touch_user_activity(user.id)

    if not is_logged_in(user.id):
        await update.message.reply_text("🔒 Locked. Please /login YOURCODE first.")
        return

    status_msg = await update.message.reply_text("AI is reading your slip... 🧠")
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        parsed = await parse_receipt_photo(photo_bytes)
        amount = float(parsed.get("amount", "0"))

        insert_receipt_and_item(
            user_id=user.id,
            merchant=parsed.get("merchant", "unknown"),
            amount=amount,
            category=parsed.get("category", "other"),
            description=parsed.get("description", ""),
            source="telegram_photo",
        )

        await status_msg.edit_text(
            f"✅ Logged: R{amount:.2f} — {parsed.get('category', 'other')} "
            f"({parsed.get('merchant', 'unknown')})"
        )
        detect_recurring_transactions(user.id)
    except Exception as e:
        logger.error(f"Photo processing failed for user={user.id}: {e}")
        await status_msg.edit_text(f"❌ Couldn't read that slip: {e}")


# ------------------------------------------------------------
# Scheduled jobs — run inside the bot process via JobQueue,
# no separate cron infrastructure needed.
# ------------------------------------------------------------
async def check_inactive_users(context: ContextTypes.DEFAULT_TYPE):
    for user_id in get_inactive_users(hours=48):
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="👋 Haven't heard from you in a couple of days — anything to log? "
                     "Just tell me what you spent, or send a receipt photo."
            )
            mark_checkin_sent(user_id)
            logger.info(f"Sent check-in nudge to user={user_id}")
        except Exception as e:
            logger.error(f"Failed to send check-in to user={user_id}: {e}")


async def check_recurring_reminders(context: ContextTypes.DEFAULT_TYPE):
    for item in get_due_recurring_reminders():
        try:
            await context.bot.send_message(
                chat_id=item["user_id"],
                text=f"📅 Heads up — your usual R{item['typical_amount']:.2f} payment to "
                     f"{item['merchant']} typically hits tomorrow. Make sure there's cover."
            )
            mark_recurring_reminded(item["recurring_id"])
            logger.info(f"Sent recurring reminder to user={item['user_id']} for {item['merchant']}")
        except Exception as e:
            logger.error(f"Failed to send recurring reminder: {e}")


async def check_import_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Nudges users who still haven't uploaded a 3-month history file,
    roughly every 3 months, so the offer doesn't just get asked once and dropped."""
    for user_id in get_users_due_import_reminder(days=90):
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="📎 Still happy to import a spreadsheet of your spending history "
                     "if you've got one handy — just send it here as a file whenever suits."
            )
            mark_import_asked(user_id)
            logger.info(f"Sent import reminder to user={user_id}")
        except Exception as e:
            logger.error(f"Failed to send import reminder to user={user_id}: {e}")


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------
def main():
    init_schema()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("addcode", addcode_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Daily checks — run once every 24h, starting shortly after boot
    app.job_queue.run_repeating(check_inactive_users, interval=timedelta(hours=24), first=60)
    app.job_queue.run_repeating(check_recurring_reminders, interval=timedelta(hours=24), first=90)
    app.job_queue.run_repeating(check_import_reminders, interval=timedelta(hours=24), first=120)

    logger.info("SlippiesBot beta starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
