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
    run_column_migrations()
    logger.info(f"Schema verified/initialized at {DATABASE_PATH}")


# ------------------------------------------------------------
# Column migrations — CREATE TABLE IF NOT EXISTS only helps for
# brand-new tables. When a table already exists on an older volume
# and we add a column to it in Schema.sql, that column silently
# never gets created. This list closes that gap: every column added
# after the very first schema version goes here once, and every
# boot checks + adds anything missing, so a volume never needs to
# be manually wiped again just because the schema grew.
# ------------------------------------------------------------
COLUMN_MIGRATIONS = [
    # (table, column, full ADD COLUMN definition)
    ("users", "has_seen_intro", "INTEGER NOT NULL DEFAULT 0"),
    ("receipts", "telegram_file_id", "TEXT"),
]


def run_column_migrations():
    conn = sqlite3.connect(DATABASE_PATH)
    for table, column, definition in COLUMN_MIGRATIONS:
        existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing_cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            logger.info(f"Migration: added {table}.{column}")
    conn.commit()
    conn.close()


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


def get_or_create_profile(code: str) -> int:
    """Each license code owns exactly one profile. Returns its profile_id,
    creating the profile on first use of that code."""
    conn = get_db()
    row = conn.execute(
        "SELECT profile_id FROM profiles WHERE license_code = ?", (code,)
    ).fetchone()
    if row:
        profile_id = row["profile_id"]
    else:
        cur = conn.execute(
            "INSERT INTO profiles (license_code, label) VALUES (?, ?)", (code, code)
        )
        profile_id = cur.lastrowid
    conn.commit()
    conn.close()
    return profile_id


def log_user_in(user_id: int, code: str):
    """Sets this Telegram user's ACTIVE profile to the one owned by `code`.
    Also updates the users table's display fields for convenience."""
    profile_id = get_or_create_profile(code)
    conn = get_db()
    conn.execute(
        """INSERT INTO user_active_profile (user_id, profile_id, switched_at)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             profile_id = excluded.profile_id, switched_at = excluded.switched_at""",
        (user_id, profile_id, datetime.now().isoformat()),
    )
    conn.execute(
        """UPDATE users SET license_code = ?, logged_in_at = ?
           WHERE user_id = ?""",
        (code, datetime.now().isoformat(), user_id),
    )
    conn.commit()
    conn.close()
    return profile_id


def is_logged_in(user_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT profile_id FROM user_active_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row is not None


def get_active_profile(user_id: int) -> int:
    """The profile_id for whatever this Telegram user last logged into.
    Every data operation (log, query, import) goes through this."""
    conn = get_db()
    row = conn.execute(
        "SELECT profile_id FROM user_active_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["profile_id"] if row else None


def get_active_profile_label(user_id: int) -> str:
    conn = get_db()
    row = conn.execute(
        """SELECT p.label FROM user_active_profile uap
           JOIN profiles p ON uap.profile_id = p.profile_id
           WHERE uap.user_id = ?""",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["label"] if row else "unknown"


def has_seen_intro(user_id: int) -> bool:
    conn = get_db()
    row = conn.execute(
        "SELECT has_seen_intro FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return bool(row and row["has_seen_intro"])


def mark_intro_seen(user_id: int):
    conn = get_db()
    conn.execute("UPDATE users SET has_seen_intro = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


FIRST_TIME_INTRO = (
    "👋 Hi, I'm SlippiesBot — your personal budgeting assistant, right here in chat.\n\n"
    "Here's the quick version:\n"
    "• Send a photo of any receipt — I'll read it automatically AND keep it safely "
    "stored, so you've always got the original slip if you need it later. Photos give "
    "me the most context, so they're the best way to log something.\n"
    "• In a rush? Just tell me instead — \"spent 150 on lunch at Nandos\"\n"
    "• Ask me things — \"how much did I spend on groceries this month?\"\n\n"
    "Type /help anytime for the full rundown."
)


def build_welcome_back_text(user_id: int) -> str:
    label = get_active_profile_label(user_id)
    return (
        f"👋 Welcome back — active profile: {label}\n\n"
        "Quick reminders:\n"
        "• /login CODE — switch profiles (personal, business, etc.)\n"
        "• /help — full list of everything I can do\n"
        "• Send a receipt photo or just tell me what you spent\n"
        "• \"how much did I spend on X\" or \"how much did I get paid\" works anytime\n\n"
        "What's up?"
    )


def insert_receipt_and_item(profile_id: int, merchant: str, amount: float,
                             category: str, description: str, source: str = "telegram_text",
                             telegram_file_id: str = None):
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO receipts (profile_id, merchant, total_amount, purchased_at, source, telegram_file_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (profile_id, merchant, amount, datetime.now().isoformat(), source, telegram_file_id),
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


def insert_income(profile_id: int, source: str, amount: float, received_at: str,
                   category: str = "other_income", source_type: str = "telegram_text"):
    conn = get_db()
    conn.execute(
        """INSERT INTO income (profile_id, source, amount, received_at, category, source_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (profile_id, source, amount, received_at, category, source_type),
    )
    conn.commit()
    conn.close()


def get_income_total(profile_id: int, days: int = 30, category: str = None) -> dict:
    """Sums income over the trailing N days. By default excludes
    transfer_in (money moved between your own accounts isn't 'getting
    paid'), same logic as the spend side excluding internal transfers."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    if category:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount),0) as total, COUNT(*) as cnt FROM income
               WHERE profile_id = ? AND received_at >= ? AND category = ?""",
            (profile_id, cutoff, category),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount),0) as total, COUNT(*) as cnt FROM income
               WHERE profile_id = ? AND received_at >= ? AND category != 'transfer_in'""",
            (profile_id, cutoff),
        ).fetchone()
    conn.close()
    return {"total": row["total"], "count": row["cnt"]}


def categorize_income_locally(description: str, type_suffix: str = "") -> str:
    type_lower = type_suffix.lower()
    desc_lower = description.lower()
    if "ib transfer" in type_lower:
        return "transfer_in"
    if "salar" in desc_lower or "credit transfer" in type_lower:
        return "salary"
    return "other_income"


def bulk_insert_income(profile_id: int, rows: list, batch_id: str) -> int:
    conn = get_db()
    inserted = 0
    for row in rows:
        conn.execute(
            """INSERT INTO income (profile_id, source, amount, received_at, category, source_type)
               VALUES (?, ?, ?, ?, ?, 'bulk_import')""",
            (profile_id, row["source"], row["amount"], row["received_at"], row["category"]),
        )
        inserted += 1
    conn.commit()
    conn.close()
    return inserted


def get_recent_summary(profile_id: int, limit: int = 10) -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT merchant, total_amount, purchased_at
           FROM receipts WHERE profile_id = ?
           ORDER BY purchased_at DESC LIMIT ?""",
        (profile_id, limit),
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


def get_category_spend(profile_id: int, category: str, days: int = 30) -> dict:
    """Sums spend for a category over the trailing N days."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    row = conn.execute(
        """SELECT COALESCE(SUM(ri.amount), 0) as total, COUNT(*) as cnt
           FROM receipt_items ri
           JOIN receipts r ON ri.receipt_id = r.receipt_id
           WHERE r.profile_id = ? AND ri.category = ? AND r.purchased_at >= ?""",
        (profile_id, category, cutoff),
    ).fetchone()
    conn.close()
    return {"total": row["total"], "count": row["cnt"]}


def has_import(profile_id: int) -> bool:
    """True if this profile has ever had a bulk import land (source='bulk_import')."""
    conn = get_db()
    row = conn.execute(
        "SELECT 1 FROM receipts WHERE profile_id = ? AND source = 'bulk_import' LIMIT 1",
        (profile_id,),
    ).fetchone()
    conn.close()
    return row is not None


def get_import_ask_status(profile_id: int) -> dict:
    conn = get_db()
    row = conn.execute(
        "SELECT asked_at, fulfilled FROM pending_imports WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_import_asked(profile_id: int):
    conn = get_db()
    conn.execute(
        """INSERT INTO pending_imports (profile_id, asked_at) VALUES (?, ?)
           ON CONFLICT(profile_id) DO UPDATE SET asked_at = excluded.asked_at""",
        (profile_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def mark_import_fulfilled(profile_id: int):
    conn = get_db()
    conn.execute(
        """INSERT INTO pending_imports (profile_id, fulfilled) VALUES (?, 1)
           ON CONFLICT(profile_id) DO UPDATE SET fulfilled = 1""",
        (profile_id,),
    )
    conn.commit()
    conn.close()


def bulk_insert_receipts(profile_id: int, rows: list, batch_id: str) -> int:
    """rows: list of dicts with merchant, amount, purchased_at, category.
    Writes both a receipts row AND a matching receipt_items row (with the
    locally-derived category) so bulk-imported spend shows up correctly
    in category queries — bank exports don't have line-item detail, but
    they should still count toward category totals."""
    conn = get_db()
    inserted = 0
    for row in rows:
        cur = conn.execute(
            """INSERT INTO receipts (profile_id, merchant, total_amount, purchased_at, source, import_batch_id)
               VALUES (?, ?, ?, ?, 'bulk_import', ?)""",
            (profile_id, row["merchant"], row["amount"], row["purchased_at"], batch_id),
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
    """Finds (user_id, profile_id) pairs where the CURRENTLY ACTIVE profile
    for that Telegram user still has no import, hasn't been asked in 90
    days. Delivery goes to user_id (the Telegram chat), but the check is
    about their currently-selected profile."""
    conn = get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT uap.user_id, uap.profile_id FROM user_active_profile uap
           LEFT JOIN pending_imports pi ON uap.profile_id = pi.profile_id
           WHERE (pi.fulfilled IS NULL OR pi.fulfilled = 0)
           AND (pi.asked_at IS NULL OR pi.asked_at < ?)
           AND NOT EXISTS (
               SELECT 1 FROM receipts r WHERE r.profile_id = uap.profile_id AND r.source = 'bulk_import'
           )""",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [(r["user_id"], r["profile_id"]) for r in rows]


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


def normalize_recurring_merchant(merchant: str) -> str:
    """Service agreement references often embed a unique code per month
    (e.g. MATIESGYMA-CIS5-EUNS-QP-260601, changing every occurrence).
    If a merchant name has 3+ hyphen-separated segments, treat everything
    after the first segment as a reference code and drop it, so the same
    underlying debit order groups together correctly month to month."""
    parts = merchant.split("-")
    if len(parts) >= 4:
        return parts[0].strip()
    return merchant


def register_confirmed_debit_order(profile_id: int, merchant: str, amount: float, purchased_at: str):
    """For debit orders the bank itself already labeled — register
    immediately as recurring rather than waiting for 2+ months of
    pattern-matching. Uses the day-of-month from this single occurrence."""
    # Skip trivial bank-fee line items — not worth a "your payment is due" reminder
    if amount < 20 or merchant.strip().lower() == "fee":
        return

    merchant = normalize_recurring_merchant(merchant)
    day = datetime.fromisoformat(purchased_at).day
    conn = get_db()
    existing = conn.execute(
        "SELECT recurring_id, typical_amount FROM recurring_transactions WHERE profile_id = ? AND merchant = ?",
        (profile_id, merchant),
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE recurring_transactions
               SET typical_amount = ?, typical_day = ?, last_seen = ?
               WHERE recurring_id = ?""",
            (amount, day, purchased_at, existing["recurring_id"]),
        )
    else:
        conn.execute(
            """INSERT INTO recurring_transactions
               (profile_id, merchant, typical_amount, typical_day, last_seen)
               VALUES (?, ?, ?, ?, ?)""",
            (profile_id, merchant, amount, day, purchased_at),
        )
    conn.commit()
    conn.close()


def detect_recurring_transactions(profile_id: int):
    """Groups past receipts by merchant, checks for a similar amount
    landing on a similar day-of-month across 2+ separate months."""
    conn = get_db()
    rows = conn.execute(
        """SELECT merchant, total_amount, purchased_at FROM receipts
           WHERE profile_id = ? AND merchant IS NOT NULL
           ORDER BY merchant, purchased_at""",
        (profile_id,),
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

        existing = conn.execute(
            "SELECT recurring_id FROM recurring_transactions WHERE profile_id = ? AND merchant = ?",
            (profile_id, merchant),
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
                   (profile_id, merchant, typical_amount, typical_day, last_seen)
                   VALUES (?, ?, ?, ?, ?)""",
                (profile_id, merchant, avg_amount, typical_day, last_seen),
            )
    conn.commit()
    conn.close()


def get_due_recurring_reminders() -> list:
    """Recurring items whose typical day is tomorrow, not yet reminded
    this cycle. Joins through user_active_profile to find which Telegram
    user currently has that profile active, so the reminder reaches them."""
    conn = get_db()
    tomorrow = (datetime.now() + timedelta(days=1)).day
    today_str = datetime.now().date().isoformat()
    rows = conn.execute(
        """SELECT rt.*, uap.user_id FROM recurring_transactions rt
           JOIN user_active_profile uap ON rt.profile_id = uap.profile_id
           WHERE rt.active = 1 AND rt.typical_day = ?
           AND (rt.last_reminded_at IS NULL OR rt.last_reminded_at < ?)""",
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
- "log_income": user is telling you about money they received/got paid
- "category_query": user is asking how much they spent on something (e.g. "how much on groceries this month")
- "income_query": user is asking how much they got paid or received (e.g. "how much did I get paid", "how much income this month")
- "request_file": user wants their transaction history as a downloadable file/Excel
- "request_report": user wants a general summary of recent spending
- "other": anything else (greeting, question, unrelated)

Message: "{message}"

Reply with ONLY one word: log_transaction, log_income, category_query, income_query, request_file, request_report, or other."""

PARSE_INCOME_PROMPT = """Extract the income details from this message.
Reply in EXACTLY this format, nothing else:
source: <who paid them, or "unknown">
amount: <numeric amount only, no currency symbol>
category: <one of: salary, other_income>

Message: "{message}"
"""

INCOME_QUERY_PROMPT = """The user is asking how much money they received/got paid.
Reply in EXACTLY this format, nothing else:
days: <number of days to look back — 7 for "this week", 30 for "this month", 365 for "this year", 30 if unclear>

Message: "{message}"
"""

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


def build_excel_export(profile_id: int) -> io.BytesIO:
    """Builds an in-memory .xlsx of a profile's full transaction history,
    including both expenses and income."""
    conn = get_db()
    expense_rows = conn.execute(
        """SELECT purchased_at, merchant, total_amount, source
           FROM receipts WHERE profile_id = ? ORDER BY purchased_at DESC""",
        (profile_id,),
    ).fetchall()
    income_rows = conn.execute(
        """SELECT received_at, source, amount, category
           FROM income WHERE profile_id = ? ORDER BY received_at DESC""",
        (profile_id,),
    ).fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Expenses"
    ws.append(["Date", "Merchant", "Amount (ZAR)", "Source"])
    for r in expense_rows:
        ws.append([r["purchased_at"][:10], r["merchant"], r["total_amount"], r["source"]])

    ws2 = wb.create_sheet("Income")
    ws2.append(["Date", "Source", "Amount (ZAR)", "Category"])
    for r in income_rows:
        ws2.append([r["received_at"][:10], r["source"], r["amount"], r["category"]])

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
    if "ib transfer" in type_lower or "transfer to" in type_lower:
        return "transfer"
    if "cash withdrawal" in type_lower:
        return "cash_withdrawal"
    if "debit order" in type_lower or "service agreement" in type_lower:
        return "debit_order"

    desc_lower = description.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            return category
    return "other"


def is_bank_labeled_debit_order(type_suffix: str) -> bool:
    """The bank itself tells us this is a recurring payment — no need
    to wait for 2+ months of pattern-matching to figure it out."""
    type_lower = type_suffix.lower()
    return "debit order" in type_lower or "service agreement" in type_lower


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


def parse_bulk_excel(file_bytes: bytes) -> dict:
    """Parses a Standard Bank-style transaction export:
    columns Date | Description | In (R) | Out (R) | Bank fees (R) | Balance (R).
    Dates have no year in the row itself — the year appears as its own
    header row (e.g. a lone '2026') partway through the sheet.
    Returns {"expenses": [...], "income": [...]} — outflow rows become
    expenses, inflow (In) rows become income, so both sides get imported."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active

    header_row_idx = None
    headers = []
    for row in ws.iter_rows(min_row=1, max_row=5):
        values = [str(c.value).strip().lower() if c.value else "" for c in row]
        if any("date" in v for v in values):
            header_row_idx = row[0].row
            headers = values
            break

    if header_row_idx is None:
        return {"expenses": [], "income": []}

    date_col = next((i for i, h in enumerate(headers) if "date" in h), None)
    desc_col = next((i for i, h in enumerate(headers) if "description" in h), None)
    in_col = next((i for i, h in enumerate(headers) if h.strip().startswith("in")), None)
    out_col = next((i for i, h in enumerate(headers) if "out" in h), None)
    fee_col = next((i for i, h in enumerate(headers) if "fee" in h), None)

    if date_col is None or desc_col is None:
        return {"expenses": [], "income": []}

    expenses, income = [], []
    current_year = datetime.now().year  # fallback if no year row is ever found

    for row in ws.iter_rows(min_row=header_row_idx + 1, values_only=True):
        date_val = row[date_col] if date_col < len(row) else None
        desc_val = row[desc_col] if desc_col < len(row) else None

        if date_val and str(date_val).strip().isdigit() and len(str(date_val).strip()) == 4:
            current_year = int(str(date_val).strip())
            continue

        if not date_val or not desc_val:
            continue

        in_val = row[in_col] if in_col is not None and in_col < len(row) else None
        out_val = row[out_col] if out_col is not None and out_col < len(row) else None
        fee_val = row[fee_col] if fee_col is not None and fee_col < len(row) else None

        in_amount = abs(in_val) if isinstance(in_val, (int, float)) else 0
        out_amount = abs(out_val) if isinstance(out_val, (int, float)) else 0
        fee_amount = abs(fee_val) if isinstance(fee_val, (int, float)) else 0

        try:
            when = datetime.strptime(f"{str(date_val).strip()} {current_year}", "%d %b %Y").isoformat()
        except ValueError:
            continue  # unparseable date, skip rather than guess

        merchant = clean_merchant_description(str(desc_val))
        type_suffix = str(desc_val).split(" - ")[-1].strip() if " - " in str(desc_val) else ""

        if out_amount + fee_amount > 0:
            category = categorize_locally(merchant, type_suffix)
            expenses.append({
                "merchant": merchant,
                "amount": out_amount + fee_amount,
                "purchased_at": when,
                "category": category,
                "is_debit_order": is_bank_labeled_debit_order(type_suffix),
            })

        if in_amount > 0:
            income.append({
                "source": merchant,
                "amount": in_amount,
                "received_at": when,
                "category": categorize_income_locally(merchant, type_suffix),
            })

    return {"expenses": expenses, "income": income}


COLUMN_DETECTION_PROMPT = """This is the header row and a few sample rows from a bank
transaction export spreadsheet. Identify which column index (0-based) holds each field.

Header row: {headers}
Sample rows:
{samples}

Reply in EXACTLY this format, nothing else, using column INDEX numbers (0-based) or "none":
date_col: <index>
description_col: <index>
amount_col: <index, if there's ONE column with signed amounts (negative=spend, positive=income), else "none">
debit_col: <index, if spend/debit is a SEPARATE column, else "none">
credit_col: <index, if income/credit is a SEPARATE column, else "none">
date_format: <a Python strptime format string matching the date values shown, e.g. %d/%m/%Y or %Y-%m-%d>
"""


async def detect_generic_bank_columns(headers: list, sample_rows: list) -> dict:
    """Uses Gemini once to figure out an unfamiliar bank export's column
    layout, instead of hardcoding every possible bank format."""
    samples_text = "\n".join(str(r) for r in sample_rows[:5])
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=COLUMN_DETECTION_PROMPT.format(headers=headers, samples=samples_text),
    )
    lines = response.text.strip().split("\n")
    parsed = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


async def parse_bulk_excel_generic(file_bytes: bytes) -> dict:
    """Fallback for bank exports that don't match the Standard Bank
    format — asks Gemini to identify the column layout once, then
    parses every row against that mapping."""
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    ws = wb.active

    all_rows = list(ws.iter_rows(min_row=1, max_row=10, values_only=True))
    if not all_rows:
        return {"expenses": [], "income": []}

    # Assume row 1 is the header — reasonable default for bank exports
    headers = [str(h).strip() if h else "" for h in all_rows[0]]
    sample_rows = all_rows[1:6]

    mapping = await detect_generic_bank_columns(headers, sample_rows)

    def to_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    date_col = to_int(mapping.get("date_col"))
    desc_col = to_int(mapping.get("description_col"))
    amount_col = to_int(mapping.get("amount_col"))
    debit_col = to_int(mapping.get("debit_col"))
    credit_col = to_int(mapping.get("credit_col"))
    date_format = mapping.get("date_format", "%Y-%m-%d").strip()

    if date_col is None or desc_col is None:
        return {"expenses": [], "income": []}  # couldn't confidently map this file

    expenses, income = [], []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if date_col >= len(row) or desc_col >= len(row):
            continue
        date_val, desc_val = row[date_col], row[desc_col]
        if not date_val or not desc_val:
            continue

        try:
            if isinstance(date_val, datetime):
                when = date_val.isoformat()
            else:
                when = datetime.strptime(str(date_val).strip(), date_format).isoformat()
        except ValueError:
            continue

        merchant = str(desc_val).strip()
        category = categorize_locally(merchant)

        if amount_col is not None and amount_col < len(row) and isinstance(row[amount_col], (int, float)):
            amt = row[amount_col]
            if amt < 0:
                expenses.append({"merchant": merchant, "amount": abs(amt), "purchased_at": when,
                                  "category": category, "is_debit_order": False})
            elif amt > 0:
                income.append({"source": merchant, "amount": amt, "received_at": when,
                                "category": categorize_income_locally(merchant)})
        else:
            if debit_col is not None and debit_col < len(row) and isinstance(row[debit_col], (int, float)) and row[debit_col]:
                expenses.append({"merchant": merchant, "amount": abs(row[debit_col]), "purchased_at": when,
                                  "category": category, "is_debit_order": False})
            if credit_col is not None and credit_col < len(row) and isinstance(row[credit_col], (int, float)) and row[credit_col]:
                income.append({"source": merchant, "amount": abs(row[credit_col]), "received_at": when,
                                "category": categorize_income_locally(merchant)})

    return {"expenses": expenses, "income": income}
async def classify_intent(message_text: str) -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=INTENT_PROMPT.format(message=message_text),
    )
    intent = response.text.strip().lower()
    valid = ("log_transaction", "log_income", "category_query", "income_query",
              "request_file", "request_report", "other")
    if intent not in valid:
        return "other"
    return intent


async def parse_income_entry(message_text: str) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=PARSE_INCOME_PROMPT.format(message=message_text),
    )
    lines = response.text.strip().split("\n")
    parsed = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


async def parse_income_query(message_text: str) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=INCOME_QUERY_PROMPT.format(message=message_text),
    )
    lines = response.text.strip().split("\n")
    parsed = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


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
        await update.message.reply_text(build_welcome_back_text(user.id))
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
        profile_id = log_user_in(user.id, code)

        if not has_seen_intro(user.id):
            await update.message.reply_text(FIRST_TIME_INTRO)
            mark_intro_seen(user.id)
        else:
            await update.message.reply_text(build_welcome_back_text(user.id))

        if not has_import(profile_id):
            mark_import_asked(profile_id)
            await update.message.reply_text(
                "📎 Got a spreadsheet of the last 3 months of spending for THIS profile? "
                "Upload it here as a file and I'll import it — gives me a real baseline "
                "right away instead of starting from zero. Totally optional, just send a "
                "receipt or transaction whenever you're ready if you'd rather skip this."
            )
    else:
        await update.message.reply_text("❌ License code not found.")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 Everything I can do:\n\n"
        "💸 LOGGING SPEND\n"
        "Just tell me naturally:\n"
        "  \"spent 150 on lunch at Nandos\"\n"
        "  \"bought groceries at Woolworths for 432.50\"\n"
        "Or send a photo of any receipt/slip — I'll read the amount, "
        "merchant, and category automatically, and keep the photo safely on file.\n\n"
        "💰 LOGGING INCOME\n"
        "  \"got paid 5000 salary\"\n"
        "  \"received 200 from Danica\"\n\n"
        "📊 ASKING QUESTIONS\n"
        "  \"how much did I spend on groceries this month?\"\n"
        "  \"how much on transport this week?\"\n"
        "  \"how much did I get paid?\"\n"
        "  \"how much have I spent?\" — gives you the full total\n\n"
        "📁 GETTING YOUR DATA\n"
        "  \"give me my file\" — sends a full Excel export "
        "(expenses + income, separate sheets)\n\n"
        "📎 IMPORTING HISTORY\n"
        "Upload a bank statement Excel file anytime and I'll import "
        "it — itemized spend, income, and recurring debit orders all "
        "get picked up automatically. Ask again anytime, even after "
        "your first import.\n\n"
        "🔔 WHAT I'LL MESSAGE YOU ABOUT (no need to ask)\n"
        "  • A day before a recurring debit order is due\n"
        "  • If I haven't heard from you in 2 days\n"
        "  • A reminder to import your history, if you haven't yet\n\n"
        "🔑 SWITCHING PROFILES\n"
        "  /login ANOTHERCODE — switches your active profile "
        "(e.g. personal vs business). Each profile's data stays "
        "completely separate.\n\n"
        "Just talk to me like a person — no need to remember exact "
        "commands for most of this."
    )


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

    profile_id = get_active_profile(user.id)
    message_text = update.message.text

    intent = await classify_intent(message_text)
    logger.info(f"user={user.id} profile={profile_id} intent={intent} msg={message_text!r}")

    if intent == "log_transaction":
        parsed = await parse_transaction(message_text)
        try:
            amount = float(parsed.get("amount", "0"))
        except ValueError:
            amount = 0

        if amount <= 0:
            await update.message.reply_text(
                "Couldn't catch an amount there — try again with a number, "
                "e.g. \"spent 150 on lunch\"."
            )
            return

        insert_receipt_and_item(
            profile_id=profile_id,
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
        detect_recurring_transactions(profile_id)

    elif intent == "log_income":
        parsed = await parse_income_entry(message_text)
        try:
            amount = float(parsed.get("amount", "0"))
        except ValueError:
            amount = 0

        if amount <= 0:
            await update.message.reply_text(
                "Couldn't catch an amount there — try again with a number, "
                "e.g. \"got paid 5000 salary\"."
            )
            return

        insert_income(
            profile_id=profile_id,
            source=parsed.get("source", "unknown"),
            amount=amount,
            received_at=datetime.now().isoformat(),
            category=parsed.get("category", "other_income"),
            source_type="telegram_text",
        )
        await update.message.reply_text(
            f"💰 Logged income: R{amount:.2f} — {parsed.get('category', 'other_income')} "
            f"({parsed.get('source', 'unknown')})"
        )

    elif intent == "income_query":
        parsed = await parse_income_query(message_text)
        try:
            days = int(parsed.get("days", "30"))
        except ValueError:
            days = 30
        result = get_income_total(profile_id, days)
        if result["count"] == 0:
            await update.message.reply_text(f"No income logged in the last {days} days.")
        else:
            await update.message.reply_text(
                f"💰 You've received R{result['total']:.2f} in the last {days} days "
                f"({result['count']} item{'s' if result['count'] != 1 else ''}). "
                f"(Internal transfers between your own accounts aren't counted.)"
            )

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
                   WHERE r.profile_id = ? AND r.purchased_at >= ?
                   AND ri.category NOT IN ('transfer', 'cash_withdrawal')""",
                (profile_id, cutoff),
            ).fetchone()
            conn.close()
            await update.message.reply_text(
                f"You've spent R{row['total']:.2f} in total over the last {days} days, across {row['cnt']} transactions. "
                f"(Internal transfers and cash withdrawals aren't counted as spend.)"
            )
        else:
            result = get_category_spend(profile_id, category, days)
            if result["count"] == 0:
                await update.message.reply_text(f"No {category} spending found in the last {days} days.")
            else:
                await update.message.reply_text(
                    f"You've spent R{result['total']:.2f} on {category} in the last {days} days "
                    f"({result['count']} item{'s' if result['count'] != 1 else ''})."
                )

    elif intent == "request_file":
        buffer = build_excel_export(profile_id)
        await update.message.reply_document(
            document=buffer,
            filename=f"slippies_transactions_{profile_id}.xlsx",
            caption="📊 Here's your full transaction history for this profile."
        )

    elif intent == "request_report":
        rows = get_recent_summary(profile_id)
        if not rows:
            await update.message.reply_text("📭 Nothing logged yet — nothing to report.")
            return
        lines = [f"• R{r['total_amount']:.2f} — {r['merchant']} ({r['purchased_at'][:10]})"
                 for r in rows]
        await update.message.reply_text("📊 Your recent transactions:\n" + "\n".join(lines))

    else:
        await update.message.reply_text(
            "I mostly understand spending and income updates right now — try something like "
            "\"spent R80 on groceries\", \"how much did I spend on transport this month\", "
            "or send a receipt photo.\n\n"
            "Need help? Type /help for the full list of what I can do."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    touch_user_activity(user.id)

    if not is_logged_in(user.id):
        await update.message.reply_text("🔒 Locked. Please /login YOURCODE first.")
        return

    profile_id = get_active_profile(user.id)
    doc = update.message.document
    if not (doc.file_name or "").lower().endswith((".xlsx", ".xls")):
        await update.message.reply_text("I can only import .xlsx or .xls files right now.")
        return

    status_msg = await update.message.reply_text("Reading your file... 📖")
    try:
        file = await doc.get_file()
        file_bytes = bytes(await file.download_as_bytearray())

        parsed = parse_bulk_excel(file_bytes)
        expense_rows = parsed["expenses"]
        income_rows = parsed["income"]

        used_generic = False
        if not expense_rows and not income_rows:
            # Doesn't match Standard Bank's format — try the generic
            # Gemini-assisted column-detection path instead
            parsed = await parse_bulk_excel_generic(file_bytes)
            expense_rows = parsed["expenses"]
            income_rows = parsed["income"]
            used_generic = True

        if not expense_rows and not income_rows:
            await status_msg.edit_text(
                "❌ Couldn't figure out this file's format. Make sure it's a transaction "
                "export with clear date, description, and amount columns."
            )
            return

        batch_id = f"{profile_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        expenses_inserted = bulk_insert_receipts(profile_id, expense_rows, batch_id)
        income_inserted = bulk_insert_income(profile_id, income_rows, batch_id)
        mark_import_fulfilled(profile_id)

        # Bank already told us which of these are debit orders — register
        # immediately instead of waiting on pattern detection
        debit_order_count = 0
        for row in expense_rows:
            if row.get("is_debit_order"):
                register_confirmed_debit_order(
                    profile_id, row["merchant"], row["amount"], row["purchased_at"]
                )
                debit_order_count += 1

        detect_recurring_transactions(profile_id)

        await status_msg.edit_text(
            f"✅ Imported {expenses_inserted} expenses and {income_inserted} income entries "
            f"into this profile"
            + (f", including {debit_order_count} recurring debit order(s) I'll remind you about."
               if debit_order_count else ".")
            + " I'll use this as the baseline going forward."
            + (" (Used general format detection for this file — categorization may be rougher "
               "than usual, corrections help it improve.)" if used_generic else "")
        )
    except Exception as e:
        logger.error(f"Import failed for user={user.id} profile={profile_id}: {e}")
        await status_msg.edit_text(f"❌ Couldn't process that file: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)
    touch_user_activity(user.id)

    if not is_logged_in(user.id):
        await update.message.reply_text("🔒 Locked. Please /login YOURCODE first.")
        return

    profile_id = get_active_profile(user.id)
    status_msg = await update.message.reply_text("AI is reading your slip... 🧠")
    try:
        telegram_file_id = update.message.photo[-1].file_id  # Telegram already hosts this
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        parsed = await parse_receipt_photo(photo_bytes)
        amount = float(parsed.get("amount", "0"))

        insert_receipt_and_item(
            profile_id=profile_id,
            merchant=parsed.get("merchant", "unknown"),
            amount=amount,
            category=parsed.get("category", "other"),
            description=parsed.get("description", ""),
            source="telegram_photo",
            telegram_file_id=telegram_file_id,
        )

        await status_msg.edit_text(
            f"✅ Logged: R{amount:.2f} — {parsed.get('category', 'other')} "
            f"({parsed.get('merchant', 'unknown')})"
        )
        detect_recurring_transactions(profile_id)
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
    """Nudges users whose currently-active profile still hasn't had a
    3-month history file uploaded, roughly every 3 months, so the offer
    doesn't just get asked once and dropped."""
    for user_id, profile_id in get_users_due_import_reminder(days=90):
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="📎 Still happy to import a spreadsheet of spending history for your "
                     "current profile if you've got one handy — just send it here as a file."
            )
            mark_import_asked(profile_id)
            logger.info(f"Sent import reminder to user={user_id} profile={profile_id}")
        except Exception as e:
            logger.error(f"Failed to send import reminder to user={user_id}: {e}")


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------
def main():
    init_schema()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
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
