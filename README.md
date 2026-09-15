# SLIPPIES_BOT_V2 — Spend Trend Bot

A personal Telegram bot that tracks spending through natural conversation and
receipt photos — no app to install, just chat. It reads receipts (via Gemini
vision OCR) or plain-text messages ("spent 150 on lunch at Nandos"),
categorizes each transaction, and stores it in SQLite so the user can ask
questions in plain language ("how much did I spend on groceries this
month?") and get answers pulled from their real logged data. It supports
multiple isolated "profiles" per person (e.g. personal vs. business) and can
bootstrap a profile with real history by importing a bank statement
(Excel or PDF).

This is a personal/solo side project by Dean, built as a rewrite/successor
to an earlier prototype ([SlippiesBot](https://github.com/DeanMeiring/Slippies-Bot)),
significantly expanded with profiles, budgets, bulk import, and an admin
system.

---

## Key features

- **Natural-language transaction logging** — free-text expense/income
  messages are parsed and categorized automatically.
- **Receipt photo OCR** — a photo sent via Telegram is read by Gemini
  vision to extract merchant, total, and line items.
- **Bulk history import (Excel & PDF)** — fast regex parsing for Standard
  Bank statements, with a generic Gemini-assisted column-mapping fallback
  for other formats; deduplicates on import and on live entries.
- **Profiles** — one license code = one isolated data profile; a single
  Telegram account can hold and switch between several (`/login CODE`).
- **Budgets** — set per-category or overall budgets and check status on
  demand.
- **Debit order / recurring payment tracking** — recognizes recurring
  bank-labeled debit orders across statements (normalizing per-month
  reference codes) and sends a reminder the day before they're due.
- **Balance estimate** — anchored to the real balance captured from the
  last bank import, adjusted for everything logged since.
- **Spending advice** — pulls a user's real transaction/budget/recurring
  data and asks Gemini for advice grounded in it, not generic tips.
- **Corrections** — "delete last" / "actually it was 90 not 150" fix the
  most recently *live-logged* entry (bulk-imported data is never touched
  via chat).
- **Excel export** — full transaction history as a two-sheet (Expenses +
  Income) Excel file.
- **Identity-based admin system** — one-time `/becomeadmin SECRET`
  bootstrap, then admin access is checked against a DB table, not a
  password typed in chat. Includes an engagement-only admin overview (no
  amounts or merchant names shown), profile ping/deactivate/reactivate,
  and a two-stage, confirmation-gated hard delete.
- **Self-healing schema migrations** — missing columns/tables are added
  automatically on boot, so schema changes don't require wiping the
  database.
- **Scheduled jobs** — daily inactivity check-ins, debit order reminders,
  and import-history nudges via `python-telegram-bot`'s JobQueue.

Full command-by-command and architecture notes live in
[`REFERENCE.md`](./REFERENCE.md); the database layout is in
[`Schema.sql`](./Schema.sql).

---

## Tech stack

- **Language:** Python 3
- **Bot framework:** [`python-telegram-bot`](https://github.com/python-telegram-bot/python-telegram-bot) (async, polling-based, with JobQueue for scheduled jobs)
- **AI:** Google Gemini (`google-genai`) — a cheap Flash-Lite model for
  high-volume categorization/OCR, a stronger Flash model for spending
  advice
- **Database:** SQLite (single file, intended to sit on a persistent
  volume in production)
- **File parsing:** `openpyxl` (Excel), `pdfplumber` (PDF)
- **Hosting:** designed for [Railway](https://railway.app) (see `Procfile`)

---

## Setup / running locally

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create a `.env` file in the project root (never commit this file) with:
   ```
   TELEGRAM_BOT_TOKEN=
   GEMINI_API_KEY=
   ADMIN_SECRET=
   DATABASE_PATH=
   ```
   - `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather).
   - `GEMINI_API_KEY` — a Google Gemini API key.
   - `ADMIN_SECRET` — a one-time secret used only for the first
     `/becomeadmin` bootstrap; after that, admin access is identity-based
     and this value is no longer needed at runtime.
   - `DATABASE_PATH` — optional; defaults to `./data/beta_test.db`.
3. Run the bot:
   ```bash
   python3 bot_beta.py
   ```
   The schema (`Schema.sql`) is applied automatically on startup, including
   any additive migrations.

For a production deploy, the `Procfile` runs the bot as a worker process
(e.g. on Railway) — set the same environment variables as platform
variables rather than shipping a `.env` file.

---

## Data model

Full schema in [`Schema.sql`](./Schema.sql). Summary of the core tables:

| Table | Purpose |
|---|---|
| `license_codes` / `profiles` | Access codes; each owns one isolated data profile |
| `user_active_profile` | Which profile a Telegram user currently has active |
| `receipts` / `receipt_items` | Expense records; `source` distinguishes live receipts (`telegram_photo`) from bulk-imported history (`bulk_import`) |
| `income` | Income records |
| `category_corrections` | Every user correction to a category — potential future training data |
| `spend_aggregates` | Precomputed weekly/monthly rollups per profile/category |
| `recurring_transactions` | Detected/confirmed debit orders |
| `balance_snapshots` | Real bank balance anchors captured from imports |
| `budgets` | Spending goals per category/profile |
| `admin_users` | Identity-based admin access |
| `pending_imports` | Tracks the bulk-import ask/reminder cycle |
| `nudges_sent` | Log of every proactive message sent, to avoid duplicates |

---

## Project status

Actively developed personal project (see commit history / `REFERENCE.md`
for what's built vs. still backlog). Not a commercial product — built to
solve the author's own budgeting/expense-tracking problem, with room left
to explore a lightweight forecasting model once enough real per-user data
accumulates.
