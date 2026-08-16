"""
SlippiesBot V2 — Beta Test Bot
--------------------------------
Standalone test bot. Reads config from .env (TELEGRAM_BOT_TOKEN,
GEMINI_API_KEY, DATABASE_PATH). Completely separate from V1 —
different bot token, different local database file.

Run with: python3 bot_beta.py
"""

import os
import sqlite3
import logging
from datetime import datetime

from dotenv import load_dotenv
from google import genai
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


# ------------------------------------------------------------
# Gemini helpers
# ------------------------------------------------------------
INTENT_PROMPT = """Classify this Telegram message into exactly one category:
- "log_transaction": user is telling you about money they spent
- "request_report": user wants a summary/report/statement of their spending
- "other": anything else (greeting, question, unrelated)

Message: "{message}"

Reply with ONLY one word: log_transaction, request_report, or other."""

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


async def classify_intent(message_text: str) -> str:
    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL_NAME,
        contents=INTENT_PROMPT.format(message=message_text),
    )
    intent = response.text.strip().lower()
    if intent not in ("log_transaction", "request_report", "other"):
        return "other"
    return intent


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
            {"mime_type": "image/jpeg", "data": image_bytes},
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

    if not context.args:
        await update.message.reply_text("Usage: /login YOURCODE")
        return

    code = context.args[0].upper()
    if license_code_valid(code):
        log_user_in(user.id, code)
        await update.message.reply_text(f"✅ Access granted — logged in with {code}.")
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
            "\"spent R80 on groceries\", or send a receipt photo."
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user(user.id, user.username or user.first_name)

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
    except Exception as e:
        logger.error(f"Photo processing failed for user={user.id}: {e}")
        await status_msg.edit_text(f"❌ Couldn't read that slip: {e}")


# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("login", login_command))
    app.add_handler(CommandHandler("addcode", addcode_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info("SlippiesBot beta starting...")
    app.run_polling()


if __name__ == "__main__":
    main()