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
import re
import json
import pdfplumber
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


def save_balance_snapshot(profile_id: int, balance: float, as_of_date: str, source: str = "bulk_import"):
    conn = get_db()
    conn.execute(
        """INSERT INTO balance_snapshots (profile_id, balance, as_of_date, source)
           VALUES (?, ?, ?, ?)""",
        (profile_id, balance, as_of_date, source),
    )
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM admin_users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def any_admin_exists() -> bool:
    conn = get_db()
    row = conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone()
    conn.close()
    return row is not None


def add_admin(user_id: int, added_by: int, label: str):
    conn = get_db()
    conn.execute(
        """INSERT INTO admin_users (user_id, added_by, label) VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET label = excluded.label""",
        (user_id, added_by, label),
    )
    conn.commit()
    conn.close()


def remove_admin(user_id: int) -> bool:
    conn = get_db()
    cur = conn.execute("DELETE FROM admin_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_admins() -> list:
    conn = get_db()
    rows = conn.execute(
        """SELECT a.user_id, a.label, a.added_at, u.username
           FROM admin_users a LEFT JOIN users u ON a.user_id = u.user_id
           ORDER BY a.added_at"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_user_id_by_username(username: str) -> int:
    """Resolves a @username to a Telegram user_id — only works if that
    person has messaged the bot at least once before (so we have their
    username on record)."""
    clean = username.lstrip("@").lower()
    conn = get_db()
    row = conn.execute(
        "SELECT user_id FROM users WHERE LOWER(username) = ?", (clean,)
    ).fetchone()
    conn.close()
    return row["user_id"] if row else None


def deactivate_profile(code: str) -> bool:
    """Soft delete — blocks new logins (license_code_valid already checks
    active=1) but leaves every row of data fully intact."""
    conn = get_db()
    cur = conn.execute("UPDATE license_codes SET active = 0 WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def reactivate_profile(code: str) -> bool:
    conn = get_db()
    cur = conn.execute("UPDATE license_codes SET active = 1 WHERE code = ?", (code,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def get_profile_id_for_code(code: str) -> int:
    conn = get_db()
    row = conn.execute("SELECT profile_id FROM profiles WHERE license_code = ?", (code,)).fetchone()
    conn.close()
    return row["profile_id"] if row else None


def is_profile_active(code: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT active FROM license_codes WHERE code = ?", (code,)).fetchone()
    conn.close()
    return bool(row and row["active"])


def hard_delete_profile_data(profile_id: int, code: str) -> dict:
    """Permanently removes every row tied to this profile, across every
    table. Only ever called after the profile has already been
    deactivated (soft-deleted) — see harddelete_command's two-stage gate."""
    conn = get_db()

    receipt_ids = [r["receipt_id"] for r in conn.execute(
        "SELECT receipt_id FROM receipts WHERE profile_id = ?", (profile_id,)
    ).fetchall()]

    counts = {}
    for rid in receipt_ids:
        conn.execute("DELETE FROM category_corrections WHERE item_id IN "
                     "(SELECT item_id FROM receipt_items WHERE receipt_id = ?)", (rid,))
    conn.execute(
        "DELETE FROM receipt_items WHERE receipt_id IN "
        "(SELECT receipt_id FROM receipts WHERE profile_id = ?)", (profile_id,)
    )
    counts["receipts"] = conn.execute("DELETE FROM receipts WHERE profile_id = ?", (profile_id,)).rowcount
    counts["income"] = conn.execute("DELETE FROM income WHERE profile_id = ?", (profile_id,)).rowcount
    counts["recurring_transactions"] = conn.execute(
        "DELETE FROM recurring_transactions WHERE profile_id = ?", (profile_id,)).rowcount
    conn.execute("DELETE FROM spend_aggregates WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM nudges_sent WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM pending_imports WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM balance_snapshots WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM user_active_profile WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM profiles WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM license_codes WHERE code = ?", (code,))

    conn.commit()
    conn.close()
    return counts


def get_admin_overview() -> list:
    """Per-profile activity summary for /admin — counts and last-active
    date only, no financial amounts or merchant details."""
    conn = get_db()
    profiles = conn.execute("SELECT profile_id, license_code, label FROM profiles").fetchall()
    overview = []
    for p in profiles:
        pid = p["profile_id"]
        receipt_stats = conn.execute(
            "SELECT COUNT(*) as cnt, MAX(processed_at) as last FROM receipts WHERE profile_id = ?",
            (pid,),
        ).fetchone()
        income_stats = conn.execute(
            "SELECT COUNT(*) as cnt, MAX(created_at) as last FROM income WHERE profile_id = ?",
            (pid,),
        ).fetchone()
        total_txns = receipt_stats["cnt"] + income_stats["cnt"]
        last_dates = [d for d in (receipt_stats["last"], income_stats["last"]) if d]
        last_active = max(last_dates) if last_dates else None
        overview.append({
            "profile_id": pid,
            "license_code": p["license_code"],
            "label": p["label"],
            "total_txns": total_txns,
            "last_active": last_active,
        })
    conn.close()
    return overview


def get_active_telegram_user_for_profile(profile_id: int) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT user_id FROM user_active_profile WHERE profile_id = ?", (profile_id,)
    ).fetchone()
    conn.close()
    return row["user_id"] if row else None


def get_last_logged_entry(profile_id: int) -> dict:
    """Finds whichever the user logged most recently — expense or
    income — restricted to live-logged entries (text/photo), never
    bulk-imported bank data, so a casual 'delete last' can't wipe
    real historical statement rows by accident. Sorts by ID as a
    tiebreaker since processed_at/created_at only have second-level
    resolution and can tie for rapid back-to-back entries."""
    conn = get_db()
    expense = conn.execute(
        """SELECT receipt_id as id, merchant as label, total_amount as amount,
                  purchased_at as date, 'expense' as kind
           FROM receipts WHERE profile_id = ? AND source IN ('telegram_text', 'telegram_photo')
           ORDER BY processed_at DESC, receipt_id DESC LIMIT 1""",
        (profile_id,),
    ).fetchone()
    income = conn.execute(
        """SELECT income_id as id, source as label, amount,
                  received_at as date, 'income' as kind
           FROM income WHERE profile_id = ? AND source_type = 'telegram_text'
           ORDER BY created_at DESC, income_id DESC LIMIT 1""",
        (profile_id,),
    ).fetchone()
    conn.close()

    candidates = [dict(r) for r in (expense, income) if r]
    if not candidates:
        return None
    # Use id as the true tiebreaker for "most recent" — ids only ever
    # increase with insert order, unlike second-resolution timestamps
    return max(candidates, key=lambda c: (c["date"], c["id"]))


def delete_last_logged_entry(profile_id: int) -> dict:
    entry = get_last_logged_entry(profile_id)
    if not entry:
        return None
    conn = get_db()
    if entry["kind"] == "expense":
        conn.execute("DELETE FROM receipt_items WHERE receipt_id = ?", (entry["id"],))
        conn.execute("DELETE FROM receipts WHERE receipt_id = ?", (entry["id"],))
    else:
        conn.execute("DELETE FROM income WHERE income_id = ?", (entry["id"],))
    conn.commit()
    conn.close()
    return entry


def edit_last_logged_entry_amount(profile_id: int, new_amount: float) -> dict:
    entry = get_last_logged_entry(profile_id)
    if not entry:
        return None
    conn = get_db()
    if entry["kind"] == "expense":
        conn.execute("UPDATE receipts SET total_amount = ? WHERE receipt_id = ?", (new_amount, entry["id"]))
        conn.execute("UPDATE receipt_items SET amount = ? WHERE receipt_id = ?", (new_amount, entry["id"]))
    else:
        conn.execute("UPDATE income SET amount = ? WHERE income_id = ?", (new_amount, entry["id"]))
    conn.commit()
    conn.close()
    entry["old_amount"] = entry["amount"]
    entry["amount"] = new_amount
    return entry


def get_current_balance_estimate(profile_id: int) -> dict:
    """Anchors to the real bank balance from the most recent import,
    then adjusts for anything logged live (text/photo) since then —
    an honest estimate, not a claim of live bank connectivity."""
    conn = get_db()
    snap = conn.execute(
        """SELECT balance, as_of_date FROM balance_snapshots
           WHERE profile_id = ? ORDER BY as_of_date DESC LIMIT 1""",
        (profile_id,),
    ).fetchone()

    if not snap:
        conn.close()
        return {"has_snapshot": False}

    since = snap["as_of_date"]
    expense_since = conn.execute(
        """SELECT COALESCE(SUM(total_amount),0) as total FROM receipts
           WHERE profile_id = ? AND purchased_at > ? AND source != 'bulk_import'""",
        (profile_id, since),
    ).fetchone()["total"]
    income_since = conn.execute(
        """SELECT COALESCE(SUM(amount),0) as total FROM income
           WHERE profile_id = ? AND received_at > ? AND source_type != 'bulk_import'""",
        (profile_id, since),
    ).fetchone()["total"]
    conn.close()

    estimated = snap["balance"] - expense_since + income_since
    return {
        "has_snapshot": True,
        "snapshot_balance": snap["balance"],
        "as_of_date": since,
        "expense_since": expense_since,
        "income_since": income_since,
        "estimated_balance": estimated,
    }


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


def is_exact_duplicate_expense(profile_id: int, merchant: str, amount: float, purchased_at: str) -> bool:
    """Strict check for bulk imports: same date, same amount to the cent,
    same description. Bank data is precise enough that this rarely
    misses a real duplicate or flags a false one."""
    date_only = purchased_at[:10]
    conn = get_db()
    row = conn.execute(
        """SELECT 1 FROM receipts
           WHERE profile_id = ? AND merchant = ? AND total_amount = ?
           AND substr(purchased_at, 1, 10) = ?""",
        (profile_id, merchant, amount, date_only),
    ).fetchone()
    conn.close()
    return row is not None


def is_exact_duplicate_income(profile_id: int, source: str, amount: float, received_at: str) -> bool:
    date_only = received_at[:10]
    conn = get_db()
    row = conn.execute(
        """SELECT 1 FROM income
           WHERE profile_id = ? AND source = ? AND amount = ?
           AND substr(received_at, 1, 10) = ?""",
        (profile_id, source, amount, date_only),
    ).fetchone()
    conn.close()
    return row is not None


def _merchant_similarity(a: str, b: str) -> float:
    """Cheap fuzzy match — good enough to catch 'Nandos' vs
    'C*THE CRAZY S' style mismatches being genuinely different,
    while catching near-identical spellings of the same merchant."""
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if shorter in longer:
        return 0.85
    a_words, b_words = set(a.split()), set(b.split())
    if not a_words or not b_words:
        return 0.0
    overlap = len(a_words & b_words) / len(a_words | b_words)
    return overlap


def is_likely_duplicate_expense(profile_id: int, merchant: str, amount: float, purchased_at: str) -> bool:
    """Looser check for live-logged (text/photo) entries: ±1 day,
    matching amount, fuzzy merchant name. Won't be perfect across a
    receipt photo vs a bank-statement line for the same purchase, but
    catches the common cases (same source logged twice, etc.)."""
    date_obj = datetime.fromisoformat(purchased_at)
    window_start = (date_obj - timedelta(days=1)).isoformat()
    window_end = (date_obj + timedelta(days=1)).isoformat()
    conn = get_db()
    candidates = conn.execute(
        """SELECT merchant FROM receipts
           WHERE profile_id = ? AND total_amount = ?
           AND purchased_at BETWEEN ? AND ?""",
        (profile_id, amount, window_start, window_end),
    ).fetchall()
    conn.close()
    return any(_merchant_similarity(merchant, c["merchant"] or "") >= 0.5 for c in candidates)


def bulk_insert_income(profile_id: int, rows: list, batch_id: str) -> dict:
    conn = get_db()
    inserted, skipped = 0, 0
    for row in rows:
        date_only = row["received_at"][:10]
        existing = conn.execute(
            """SELECT 1 FROM income
               WHERE profile_id = ? AND source = ? AND amount = ?
               AND substr(received_at, 1, 10) = ?""",
            (profile_id, row["source"], row["amount"], date_only),
        ).fetchone()
        if existing:
            skipped += 1
            continue
        conn.execute(
            """INSERT INTO income (profile_id, source, amount, received_at, category, source_type)
               VALUES (?, ?, ?, ?, ?, 'bulk_import')""",
            (profile_id, row["source"], row["amount"], row["received_at"], row["category"]),
        )
        inserted += 1
    conn.commit()
    conn.close()
    return {"inserted": inserted, "skipped_duplicates": skipped}


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


def bulk_insert_receipts(profile_id: int, rows: list, batch_id: str) -> dict:
    """rows: list of dicts with merchant, amount, purchased_at, category.
    Writes both a receipts row AND a matching receipt_items row (with the
    locally-derived category) so bulk-imported spend shows up correctly
    in category queries — bank exports don't have line-item detail, but
    they should still count toward category totals. Skips exact
    duplicates (same date, amount, merchant) already in the profile."""
    conn = get_db()
    inserted, skipped = 0, 0
    for row in rows:
        date_only = row["purchased_at"][:10]
        existing = conn.execute(
            """SELECT 1 FROM receipts
               WHERE profile_id = ? AND merchant = ? AND total_amount = ?
               AND substr(purchased_at, 1, 10) = ?""",
            (profile_id, row["merchant"], row["amount"], date_only),
        ).fetchone()
        if existing:
            skipped += 1
            continue
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
    return {"inserted": inserted, "skipped_duplicates": skipped}


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
- "category_query": user is asking for a TOTAL amount spent, either on a specific category or overall (e.g. "how much on groceries this month", "how much have I spent", "total spent in the last 15 days", "what did I spend this week")
- "income_query": user is asking how much they got paid or received (e.g. "how much did I get paid", "how much income this month")
- "balance_query": user is asking how much money they have left/remaining (e.g. "how much money is left", "what's my balance")
- "delete_last": user wants to delete/remove/undo the last thing they logged (e.g. "delete last", "remove that", "undo", "oops I sent that twice")
- "edit_last": user wants to correct the amount of the last thing they logged (e.g. "actually it was 90 not 150", "change last amount to 200", "fix that to R80")
- "request_file": user wants their transaction history as a downloadable file/Excel
- "request_report": user wants to SEE a LIST of individual recent transactions, not a total (e.g. "show me my recent transactions", "what have I bought lately")
- "other": anything else (greeting, question, unrelated)

If the user is asking for a NUMBER/TOTAL (how much, what's the total, what did I spend), always use category_query — never request_report. request_report is only for when they want to see a list of individual line items.

Message: "{message}"

Reply with ONLY one word: log_transaction, log_income, category_query, income_query, balance_query, delete_last, edit_last, request_file, request_report, or other."""

PARSE_EDIT_AMOUNT_PROMPT = """The user wants to correct the amount of their last logged entry.
Reply in EXACTLY this format, nothing else:
amount: <the correct numeric amount only, no currency symbol>

Message: "{message}"
"""

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
category: <one of: groceries, client_entertainment, staff_meals, fuel, vehicle_maintenance, parking_tolls, travel_flights, health, entertainment, electricity, water, telecoms_mobile, insurance, professional_services, repairs_maintenance, household, other>
description: <short description of what was bought>

Message: "{message}"
"""

PARSE_RECEIPT_PHOTO_PROMPT = """This is a photo of a purchase receipt or slip.
Extract the details and reply in EXACTLY this format, nothing else:
merchant: <vendor/store name>
amount: <total numeric amount only, no currency symbol>
category: <one of: groceries, client_entertainment, staff_meals, fuel, vehicle_maintenance, parking_tolls, travel_flights, health, entertainment, electricity, water, telecoms_mobile, insurance, professional_services, repairs_maintenance, household, other>
description: <brief summary of main items, or the merchant name if items aren't legible>

If any field is unclear from the image, make your best reasonable guess rather than leaving it blank."""

CATEGORY_QUERY_PROMPT = """The user is asking for a total amount spent — either on a
specific category, or an overall total if they didn't mention one.
Reply in EXACTLY this format, nothing else:
category: <one of: groceries, client_entertainment, staff_meals, fuel, vehicle_maintenance, parking_tolls, travel_flights, health, entertainment, electricity, water, telecoms_mobile, insurance, professional_services, repairs_maintenance, household, other, or "all" if not specific>
days: <number of days to look back. If the user states an exact number (e.g. "last 15 days" -> 15, "last 45 days" -> 45), use that exact number. Otherwise: 7 for "this week", 30 for "this month", 365 for "this year", 30 if genuinely unclear>

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
                  "food lover", "boxer", "fruit", "cc fresh", "biz afrika", "fresh x"],
    "client_entertainment": ["mugg", "milky lane", "vida", "cafe", "caffe", "coffee",
                              "bistro", "restaur", "kitchen", "grill", "wine", "wimpy"],
    "staff_meals": ["kfc", "steers", "nandos", "mcd", "debonairs", "poke", "yoco",
                     "pizza", "uber eats", "mr d", "taste", "ginos", "simplygreek",
                     "motherdough", "bootleggers", "fat cactus", "bakery"],
    "fuel": ["shell", "engen", "sasol", "bp disa", "bp tyger", "total somerse",
             "petrol", "lynedoch serv", "caltex"],
    "vehicle_maintenance": ["dekra", "tyre", "auto st", "car service", "motor ",
                             "panelbeater", "wheel"],
    "parking_tolls": ["parking", "toll", "str parking", "cticc parking"],
    "travel_flights": ["europcar", "travelstart", "acsa", "flight", "airbnb", "booking.com"],
    "health": ["dischem", "clicks", "pharmacy", "dr ", "doctor", "medical", "dentist",
               "life groenklo", "clinic"],
    "entertainment": ["sterkinekor", "ster kinekor", "steamgames", "playtomic",
                       "matiesgym", "gym", "movie", "netflix", "showmax", "spotify",
                       "golf", "museum"],
    "electricity": ["electricity"],
    "water": ["water purchase", "crp water"],
    "telecoms_mobile": ["vodacom", "mtn", "cell c", "prepaid mobile", "vodashop",
                         "telecom", "voda"],
    "insurance": ["insurance premium", "disclife", "disc prem", "medical aid",
                   "mom_insure", "regsvir"],
    "professional_services": ["accountant", "legal", "consult", "audit"],
    "repairs_maintenance": ["dulux paint", "hardware", "repair"],
    "household": ["takealot", "gadget", "gadgettime", "evetech", "microsoft",
                   "godaddy", "openai"],
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
    balance_col = next((i for i, h in enumerate(headers) if "balance" in h), None)

    if date_col is None or desc_col is None:
        return {"expenses": [], "income": [], "last_balance": None, "last_balance_date": None}

    expenses, income = [], []
    last_balance, last_balance_date = None, None
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

        bal_val = row[balance_col] if balance_col is not None and balance_col < len(row) else None
        if isinstance(bal_val, (int, float)):
            if last_balance_date is None or when >= last_balance_date:
                last_balance = float(bal_val)
                last_balance_date = when

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

    return {"expenses": expenses, "income": income, "last_balance": last_balance,
             "last_balance_date": last_balance_date}


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
        return {"expenses": [], "income": [], "last_balance": None, "last_balance_date": None}

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
        return {"expenses": [], "income": [], "last_balance": None, "last_balance_date": None}  # couldn't confidently map this file

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

    return {"expenses": expenses, "income": income, "last_balance": None, "last_balance_date": None}


PDF_TRANSACTION_EXTRACTION_PROMPT = """This is raw text extracted from a bank statement PDF.
Extract every transaction you can find. Reply as a JSON array, nothing else — no markdown,
no explanation. Each item must look exactly like this:

{{"date": "YYYY-MM-DD", "description": "merchant/description text", "amount": -150.00, "type": "transaction type if shown, else empty string"}}

Rules:
- amount is negative for money OUT (spend/debit), positive for money IN (deposit/credit)
- If a year isn't shown per-row, infer it from statement header dates or context
- Skip lines that are headers, page footers, balance-only lines, or account summaries
- Only include actual transaction lines

Text:
{text}
"""


async def parse_bulk_pdf(file_bytes: bytes) -> dict:
    """Parses a bank statement PDF. Tries the fast Standard Bank text
    pattern first (same format we already handle for Excel, since some
    PDF exports linearize to the same Date/Description/Type/Amount
    shape). Falls back to a Gemini extraction pass over the raw PDF
    text for other banks' PDF layouts."""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    # Try the fast Standard Bank text-pattern parser first
    result = parse_standard_bank_text(full_text)
    if result["expenses"] or result["income"]:
        return result

    # Fall back to Gemini extraction for other banks' PDF layouts.
    # Chunk the text to stay within reasonable prompt size for long statements.
    chunk_size = 6000
    chunks = [full_text[i:i + chunk_size] for i in range(0, len(full_text), chunk_size)]

    expenses, income = [], []
    for chunk in chunks:
        if not chunk.strip():
            continue
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=PDF_TRANSACTION_EXTRACTION_PROMPT.format(text=chunk),
        )
        try:
            raw = response.text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            rows = json.loads(raw)
        except (json.JSONDecodeError, IndexError):
            continue

        for row in rows:
            try:
                when = datetime.strptime(row["date"], "%Y-%m-%d").isoformat()
                amt = float(row["amount"])
                merchant = clean_merchant_description(str(row.get("description", "unknown")))
                type_suffix = str(row.get("type", ""))
            except (KeyError, ValueError, TypeError):
                continue

            if amt < 0:
                category = categorize_locally(merchant, type_suffix)
                expenses.append({
                    "merchant": merchant, "amount": abs(amt), "purchased_at": when,
                    "category": category, "is_debit_order": is_bank_labeled_debit_order(type_suffix),
                })
            elif amt > 0:
                income.append({
                    "source": merchant, "amount": amt, "received_at": when,
                    "category": categorize_income_locally(merchant, type_suffix),
                })

    return {"expenses": expenses, "income": income, "last_balance": None, "last_balance_date": None}


def parse_standard_bank_text(full_text: str) -> dict:
    """Shared parsing logic for Standard Bank's Date/Description/Type/
    Amount+Balance pattern, usable against either linearized PDF text
    or (in principle) any text extraction with the same shape."""
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]

    date_re = re.compile(r"^(\d{2}) (\w{3}) (\d{2,4}) (.+)$")
    amount_re = re.compile(r"^(-?[\d,]+\.\d{2})\s+([\d,]+\.\d{2})$")
    months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

    expenses, income = [], []
    last_balance, last_balance_date = None, None
    i = 0
    while i < len(lines):
        m = date_re.match(lines[i])
        if m:
            day, mon, yr, desc = m.groups()
            type_label = lines[i + 1] if i + 1 < len(lines) else ""
            amt_line = lines[i + 2] if i + 2 < len(lines) else ""
            am = amount_re.match(amt_line)
            if am and mon in months:
                amount = float(am.group(1).replace(",", ""))
                balance = float(am.group(2).replace(",", ""))
                year = int(yr) if len(yr) == 4 else 2000 + int(yr)
                try:
                    when = datetime(year, months[mon], int(day)).isoformat()
                except ValueError:
                    i += 1
                    continue

                merchant = clean_merchant_description(desc)
                type_suffix = type_label

                if last_balance_date is None or when >= last_balance_date:
                    last_balance, last_balance_date = balance, when

                if amount < 0:
                    category = categorize_locally(merchant, type_suffix)
                    expenses.append({
                        "merchant": merchant, "amount": abs(amount), "purchased_at": when,
                        "category": category, "is_debit_order": is_bank_labeled_debit_order(type_suffix),
                    })
                elif amount > 0:
                    income.append({
                        "source": merchant, "amount": amount, "received_at": when,
                        "category": categorize_income_locally(merchant, type_suffix),
                    })
                i += 3
                continue
        i += 1

    return {"expenses": expenses, "income": income, "last_balance": last_balance,
             "last_balance_date": last_balance_date}


async def classify_intent(message_text: str) -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=INTENT_PROMPT.format(message=message_text),
    )
    intent = response.text.strip().lower()
    valid = ("log_transaction", "log_income", "category_query", "income_query",
              "balance_query", "delete_last", "edit_last", "request_file", "request_report", "other")
    if intent not in valid:
        return "other"
    return intent


async def parse_edit_amount(message_text: str) -> dict:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=PARSE_EDIT_AMOUNT_PROMPT.format(message=message_text),
    )
    lines = response.text.strip().split("\n")
    parsed = {}
    for line in lines:
        if ":" in line:
            key, _, value = line.partition(":")
            parsed[key.strip()] = value.strip()
    return parsed


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


ADMIN_HELP_TEXT = (
    "👑 ADMIN COMMANDS\n\n"
    "📊 MONITORING\n"
    "  /admin — overview of all accounts and activity (active/slowing/quiet, "
    "transaction counts — no financial amounts shown)\n"
    "  /ping CODE [message] — nudge whoever's currently active on a profile\n\n"
    "🔑 MANAGING ACCESS\n"
    "  /addcode CODE [label] — create a new license code\n"
    "  /addadmin @username — grant admin access (they must have messaged "
    "the bot at least once first)\n"
    "  /removeadmin @username — revoke admin access\n"
    "  /listadmins — see current admins\n\n"
    "🗑️ DELETING AN ACCOUNT (two-stage, for safety)\n"
    "  /deactivate CODE — blocks logins, keeps all data fully intact\n"
    "  /reactivate CODE — undoes a deactivation\n"
    "  /harddelete CODE — only works once already deactivated; sends a "
    "backup Excel first, then requires /harddelete CODE CONFIRM to "
    "permanently erase everything (cannot be undone)"
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    text = (
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
        "✏️ FIXING MISTAKES\n"
        "  \"delete last\" — removes the last thing you logged (great for "
        "accidental duplicate photos)\n"
        "  \"actually it was 90 not 150\" — corrects the amount on your last entry\n"
        "  (Only works on things you logged via chat — imported bank statement "
        "data isn't touched, for safety.)\n\n"
        "📊 ASKING QUESTIONS\n"
        "  \"how much did I spend on groceries this month?\"\n"
        "  \"how much on fuel this week?\" — categories are now more detailed: "
        "groceries, client entertainment, staff meals, fuel, vehicle maintenance, "
        "parking/tolls, travel, electricity, water, telecoms, insurance, and more\n"
        "  \"how much did I get paid?\"\n"
        "  \"how much have I spent?\" — full total\n"
        "  \"how much money is left?\" — an estimate based on your last imported "
        "bank balance plus anything logged since\n\n"
        "📁 GETTING YOUR DATA\n"
        "  \"give me my file\" — full Excel export (expenses + income, separate sheets)\n\n"
        "📎 IMPORTING HISTORY\n"
        "Upload a bank statement as Excel (.xlsx) or PDF anytime — itemized "
        "spend, income, recurring debit orders, and your account balance all "
        "get picked up automatically. Duplicate transactions are detected and "
        "skipped automatically. Ask again anytime, even after your first import.\n\n"
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

    if is_admin(user.id):
        text += "\n\n" + ADMIN_HELP_TEXT + "\n\n(Type /adminhelp anytime to see just this section.)"

    await update.message.reply_text(text)


async def adminhelp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only. Regular commands are in /help.")
        return

    await update.message.reply_text(ADMIN_HELP_TEXT)


async def addcode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only. See /becomeadmin if this is a fresh setup.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /addcode NEWCODE123 [optional_label]")
        return

    new_code = context.args[0].upper()
    label = context.args[1] if len(context.args) >= 2 else ""
    create_license_code(new_code, label)
    await update.message.reply_text(f"✅ New code created: {new_code}")


async def becomeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One-time bootstrap: the very first admin uses the env-var secret
    once. After that, this command is permanently disabled — everything
    else runs on Telegram identity via /addadmin, never a typed secret."""
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if any_admin_exists():
        await update.message.reply_text(
            "❌ An admin already exists — this bootstrap command is now disabled. "
            "Ask an existing admin to run /addadmin @yourusername."
        )
        return

    if not context.args or context.args[0] != ADMIN_SECRET:
        await update.message.reply_text("Usage: /becomeadmin your_admin_secret")
        return

    add_admin(user.id, added_by=None, label=user.username or user.first_name or "admin")
    try:
        await update.message.delete()
    except Exception:
        pass
    await update.message.reply_text(
        "✅ You're now the first admin. The secret is no longer needed — "
        "use /addadmin @username to add others from here on."
    )


async def addadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /addadmin @username")
        return

    target_id = find_user_id_by_username(context.args[0])
    if not target_id:
        await update.message.reply_text(
            "❌ Couldn't find that user — they need to have messaged me at least once first."
        )
        return

    add_admin(target_id, added_by=user.id, label=context.args[0].lstrip("@"))
    await update.message.reply_text(f"✅ {context.args[0]} added as an admin.")


async def removeadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /removeadmin @username")
        return

    target_id = find_user_id_by_username(context.args[0])
    if not target_id or not remove_admin(target_id):
        await update.message.reply_text("❌ Couldn't find that admin.")
        return
    await update.message.reply_text(f"✅ {context.args[0]} removed as an admin.")


async def listadmins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    admins = list_admins()
    if not admins:
        await update.message.reply_text("No admins yet.")
        return
    lines = [f"• {a['label'] or a['username'] or a['user_id']} (since {a['added_at'][:10]})" for a in admins]
    await update.message.reply_text("👑 Current admins:\n" + "\n".join(lines))


async def admin_overview_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    overview = get_admin_overview()
    if not overview:
        await update.message.reply_text("No accounts exist yet.")
        return

    now = datetime.now()
    active, slowing, quiet = [], [], []
    for p in overview:
        if not p["last_active"]:
            quiet.append(p)
            continue
        days_since = (now - datetime.fromisoformat(p["last_active"])).days
        if days_since <= 3:
            active.append(p)
        elif days_since <= 14:
            slowing.append(p)
        else:
            quiet.append(p)

    lines = [
        f"📊 Admin Overview\n",
        f"Total accounts: {len(overview)} profiles\n",
        f"🟢 Active (last 3 days): {len(active)}",
        f"🟡 Slowing down (4-14 days): {len(slowing)}",
        f"🔴 Gone quiet (14+ days) or never used: {len(quiet)}\n",
        "Per account:",
    ]
    for p in sorted(overview, key=lambda x: x["last_active"] or "", reverse=True):
        last = p["last_active"][:10] if p["last_active"] else "never"
        label = p["label"] or p["license_code"]
        lines.append(f"  {label:<16} — last active {last}  ({p['total_txns']} txns)")

    await update.message.reply_text("\n".join(lines))


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /ping CODE [optional custom message]")
        return

    code = context.args[0].upper()
    conn = get_db()
    profile_row = conn.execute(
        "SELECT profile_id FROM profiles WHERE license_code = ?", (code,)
    ).fetchone()
    conn.close()

    if not profile_row:
        await update.message.reply_text(f"❌ No profile found for code {code}.")
        return

    target_user = get_active_telegram_user_for_profile(profile_row["profile_id"])
    if not target_user:
        await update.message.reply_text(
            f"⚠️ No one currently has {code} as their active profile — can't deliver a ping right now."
        )
        return

    custom_message = " ".join(context.args[1:]) if len(context.args) > 1 else None
    message_text = custom_message or "👋 Just checking in — anything to log?"

    try:
        await context.bot.send_message(chat_id=target_user, text=message_text)
        await update.message.reply_text(f"✅ Pinged whoever's active on {code}.")
    except Exception as e:
        await update.message.reply_text(f"❌ Couldn't deliver the ping: {e}")


async def deactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /deactivate CODE")
        return

    code = context.args[0].upper()
    if not deactivate_profile(code):
        await update.message.reply_text(f"❌ No profile found for code {code}.")
        return
    await update.message.reply_text(
        f"🔒 {code} deactivated — no one can log into it anymore, but all its data is "
        f"still fully intact. Use /reactivate {code} to undo this, or "
        f"/harddelete {code} to permanently erase it."
    )


async def reactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /reactivate CODE")
        return

    code = context.args[0].upper()
    if not reactivate_profile(code):
        await update.message.reply_text(f"❌ No profile found for code {code}.")
        return
    await update.message.reply_text(f"✅ {code} reactivated — logins work again.")


async def harddelete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Two-stage safety: the profile must already be deactivated first
    (so there's a deliberate pause before anything permanent happens),
    and the admin must explicitly add CONFIRM — a plain /harddelete
    CODE always just explains what will happen instead of doing it."""
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

    if not is_admin(user.id):
        await update.message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /harddelete CODE")
        return

    code = context.args[0].upper()
    profile_id = get_profile_id_for_code(code)
    if not profile_id:
        await update.message.reply_text(f"❌ No profile found for code {code}.")
        return

    if is_profile_active(code):
        await update.message.reply_text(
            f"⚠️ {code} is still active. Run /deactivate {code} first — hard delete only "
            f"works on already-deactivated profiles, as a safety pause."
        )
        return

    confirmed = len(context.args) > 1 and context.args[1].upper() == "CONFIRM"

    if not confirmed:
        buffer = build_excel_export(profile_id)
        await update.message.reply_document(
            document=buffer,
            filename=f"backup_{code}_before_delete.xlsx",
            caption=(
                f"⚠️ This is a backup of everything on {code} before you decide.\n\n"
                f"This action is PERMANENT and cannot be undone. To actually proceed, run:\n"
                f"/harddelete {code} CONFIRM"
            ),
        )
        return

    counts = hard_delete_profile_data(profile_id, code)
    await update.message.reply_text(
        f"🗑️ {code} permanently deleted.\n"
        f"Removed: {counts['receipts']} receipts, {counts['income']} income entries, "
        f"{counts['recurring_transactions']} recurring payment records, and all associated "
        f"snapshots/history. This cannot be undone."
    )


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

        now_iso = datetime.now().isoformat()
        merchant = parsed.get("merchant", "unknown")
        possible_dupe = is_likely_duplicate_expense(profile_id, merchant, amount, now_iso)

        insert_receipt_and_item(
            profile_id=profile_id,
            merchant=merchant,
            amount=amount,
            category=parsed.get("category", "other"),
            description=parsed.get("description", message_text),
            source="telegram_text",
        )
        dupe_note = " ⚠️ (looks similar to something already logged — check for a double-entry)" if possible_dupe else ""
        await update.message.reply_text(
            f"Logged: R{amount:.2f} — {parsed.get('category', 'other')} "
            f"({merchant}){dupe_note}"
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

    elif intent == "balance_query":
        bal = get_current_balance_estimate(profile_id)
        if not bal["has_snapshot"]:
            await update.message.reply_text(
                "I don't have a real balance to work from yet — upload a bank statement "
                "(Excel or PDF) and I'll use its ending balance as a starting point."
            )
        else:
            await update.message.reply_text(
                f"💰 Estimated balance: R{bal['estimated_balance']:.2f}\n\n"
                f"Based on R{bal['snapshot_balance']:.2f} as of {bal['as_of_date'][:10]} "
                f"(your last imported statement), plus R{bal['income_since']:.2f} logged in "
                f"and minus R{bal['expense_since']:.2f} logged out since then.\n\n"
                f"⚠️ This is an estimate based on what's been logged — not a live bank connection. "
                f"Upload a fresh statement anytime to re-anchor it to your real balance."
            )

    elif intent == "delete_last":
        deleted = delete_last_logged_entry(profile_id)
        if not deleted:
            await update.message.reply_text(
                "Nothing to delete — you haven't logged anything yet (or it was from a "
                "bank statement import, which I don't delete via chat for safety)."
            )
        else:
            kind_label = "expense" if deleted["kind"] == "expense" else "income"
            await update.message.reply_text(
                f"🗑️ Deleted: R{deleted['amount']:.2f} {kind_label} — {deleted['label']}"
            )

    elif intent == "edit_last":
        parsed = await parse_edit_amount(message_text)
        try:
            new_amount = float(parsed.get("amount", "0"))
        except ValueError:
            new_amount = 0

        if new_amount <= 0:
            await update.message.reply_text(
                "Couldn't catch the corrected amount — try again with a number, "
                "e.g. \"actually it was 90 not 150\"."
            )
            return

        edited = edit_last_logged_entry_amount(profile_id, new_amount)
        if not edited:
            await update.message.reply_text(
                "Nothing to edit — you haven't logged anything yet (or it was from a "
                "bank statement import, which I don't edit via chat for safety)."
            )
        else:
            await update.message.reply_text(
                f"✏️ Updated: {edited['label']} — R{edited['old_amount']:.2f} → R{edited['amount']:.2f}"
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
    filename = (doc.file_name or "").lower()
    is_pdf = filename.endswith(".pdf")
    is_excel = filename.endswith((".xlsx", ".xls"))

    if not (is_pdf or is_excel):
        await update.message.reply_text("I can import .xlsx, .xls, or .pdf bank statements right now.")
        return

    status_msg = await update.message.reply_text("Reading your file... 📖")
    try:
        file = await doc.get_file()
        file_bytes = bytes(await file.download_as_bytearray())

        used_generic = False
        if is_pdf:
            parsed = await parse_bulk_pdf(file_bytes)
        else:
            parsed = parse_bulk_excel(file_bytes)
            if not parsed["expenses"] and not parsed["income"]:
                # Doesn't match Standard Bank's format — try the generic
                # Gemini-assisted column-detection path instead
                parsed = await parse_bulk_excel_generic(file_bytes)
                used_generic = True

        expense_rows = parsed["expenses"]
        income_rows = parsed["income"]

        if not expense_rows and not income_rows:
            await status_msg.edit_text(
                "❌ Couldn't figure out this file's format. Make sure it's a transaction "
                "export with clear date, description, and amount columns."
            )
            return

        batch_id = f"{profile_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        expense_result = bulk_insert_receipts(profile_id, expense_rows, batch_id)
        income_result = bulk_insert_income(profile_id, income_rows, batch_id)
        mark_import_fulfilled(profile_id)

        # Save a real balance anchor if the statement gave us one (Standard
        # Bank format, Excel or PDF) — powers "how much money is left"
        if parsed.get("last_balance") is not None:
            save_balance_snapshot(profile_id, parsed["last_balance"], parsed["last_balance_date"])

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

        total_skipped = expense_result["skipped_duplicates"] + income_result["skipped_duplicates"]

        await status_msg.edit_text(
            f"✅ Imported {expense_result['inserted']} expenses and {income_result['inserted']} "
            f"income entries into this profile"
            + (f", including {debit_order_count} recurring debit order(s) I'll remind you about."
               if debit_order_count else ".")
            + (f"\n⚠️ Skipped {total_skipped} that looked like exact duplicates of transactions "
               f"already logged." if total_skipped else "")
            + (f"\n💰 Balance as of {parsed['last_balance_date'][:10]}: R{parsed['last_balance']:.2f} "
               f"— I'll use this for 'how much money is left'." if parsed.get("last_balance") is not None else "")
            + "\nI'll use this as the baseline going forward."
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
        merchant = parsed.get("merchant", "unknown")
        now_iso = datetime.now().isoformat()
        possible_dupe = is_likely_duplicate_expense(profile_id, merchant, amount, now_iso)

        insert_receipt_and_item(
            profile_id=profile_id,
            merchant=merchant,
            amount=amount,
            category=parsed.get("category", "other"),
            description=parsed.get("description", ""),
            source="telegram_photo",
            telegram_file_id=telegram_file_id,
        )

        dupe_note = " ⚠️ (looks similar to something already logged — check for a double-entry)" if possible_dupe else ""
        await status_msg.edit_text(
            f"✅ Logged: R{amount:.2f} — {parsed.get('category', 'other')} "
            f"({merchant}){dupe_note}"
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
    app.add_handler(CommandHandler("adminhelp", adminhelp_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("addcode", addcode_command))
    app.add_handler(CommandHandler("becomeadmin", becomeadmin_command))
    app.add_handler(CommandHandler("addadmin", addadmin_command))
    app.add_handler(CommandHandler("removeadmin", removeadmin_command))
    app.add_handler(CommandHandler("listadmins", listadmins_command))
    app.add_handler(CommandHandler("admin", admin_overview_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("deactivate", deactivate_command))
    app.add_handler(CommandHandler("reactivate", reactivate_command))
    app.add_handler(CommandHandler("harddelete", harddelete_command))
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
