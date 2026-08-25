-- ============================================================
-- Spend Trend Bot — Database Schema (SQLite)
-- ============================================================
-- Builds on the same shape as SlippiesBot's receipt data, but
-- adds the tables needed for trend detection + Telegram nudges.
-- ============================================================

-- ------------------------------------------------------------
-- 0. LICENSE_CODES
-- Access gate, carried over from V1's login system. Unlike V1
-- (which tracked logins in an in-memory dict that reset on every
-- redeploy), here the login is persisted on the user row itself.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS license_codes (
    code            TEXT PRIMARY KEY,
    label           TEXT,                   -- optional friendly name
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    active          INTEGER NOT NULL DEFAULT 1
);

-- ------------------------------------------------------------
-- 0b. PROFILES
-- Each license code now owns its OWN separate data profile — this is
-- what lets one Telegram account hold multiple distinct profiles
-- (e.g. personal vs business, or managing a family member's finances).
-- All spend data below is keyed to profile_id, not directly to the
-- Telegram user, so switching codes switches the entire data view.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS profiles (
    profile_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    license_code    TEXT UNIQUE NOT NULL REFERENCES license_codes(code),
    label           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    import_cooldown_days INTEGER NOT NULL DEFAULT 90,
    last_import_at  TEXT
);

-- ------------------------------------------------------------
-- 0c. USER_ACTIVE_PROFILE
-- Which profile is "currently selected" for a given Telegram user.
-- Switches every time they /login with a different code. A single
-- Telegram user can have logged into several profiles over time, but
-- only one is active at once — same phone, different "hats".
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_active_profile (
    user_id         INTEGER PRIMARY KEY REFERENCES users(user_id),
    profile_id      INTEGER NOT NULL REFERENCES profiles(profile_id),
    switched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- 1. USERS
-- One row per Telegram user. Mirrors SlippiesBot's licensing table.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,   -- Telegram user id
    username        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    timezone        TEXT DEFAULT 'Africa/Johannesburg',
    nudge_opt_in    INTEGER NOT NULL DEFAULT 1,   -- 1 = wants proactive messages, 0 = off
    last_nudge_at   TEXT,                   -- prevents spamming; set after every push
    license_code    TEXT REFERENCES license_codes(code),  -- NULL until /login succeeds
    logged_in_at    TEXT,
    has_seen_intro  INTEGER NOT NULL DEFAULT 0  -- 1 once the first-time intro has been shown
);

-- ------------------------------------------------------------
-- 2. RECEIPTS
-- One row per processed receipt (the raw event).
-- If merging into SlippiesBot later, this maps to its existing receipt table.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES profiles(profile_id),
    merchant        TEXT,
    total_amount    REAL NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'ZAR',
    purchased_at    TEXT NOT NULL,          -- actual date on the receipt
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    raw_ocr_text    TEXT,                   -- keep Gemini's raw output for future re-parsing
    source          TEXT DEFAULT 'telegram_photo',  -- 'telegram_photo' or 'bulk_import'
    import_batch_id TEXT,                   -- groups rows from the same uploaded file, NULL for live receipts
    telegram_file_id TEXT                   -- Telegram's own file reference — lets us re-fetch
                                             -- the original photo later at near-zero storage cost,
                                             -- since Telegram hosts the file, not us
);

CREATE INDEX IF NOT EXISTS idx_receipts_profile_date
    ON receipts(profile_id, purchased_at);

-- ------------------------------------------------------------
-- 3. RECEIPT_ITEMS
-- Line-item detail — the thing bank feeds structurally can't see.
-- This table is the actual moat: itemized, categorized spend.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS receipt_items (
    item_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id      INTEGER NOT NULL REFERENCES receipts(receipt_id),
    description     TEXT NOT NULL,          -- raw item text, e.g. "FULL CREAM MILK 2L"
    category        TEXT,                   -- e.g. "groceries", "eating_out", "household"
    amount          REAL NOT NULL,
    quantity        REAL DEFAULT 1,
    category_source TEXT DEFAULT 'model',   -- 'model' = auto-tagged, 'user' = corrected
    corrected_at    TEXT                    -- set when a user overrides the category
);

CREATE INDEX IF NOT EXISTS idx_items_receipt
    ON receipt_items(receipt_id);
CREATE INDEX IF NOT EXISTS idx_items_category
    ON receipt_items(category);

-- ------------------------------------------------------------
-- 4. CATEGORY_CORRECTIONS
-- The feedback-loop table from our conversation — every time a
-- user fixes a category, log it here. This IS the training data
-- for the eventual fine-tuned/XGBoost categorizer.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS category_corrections (
    correction_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES receipt_items(item_id),
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
    old_category    TEXT,
    new_category    TEXT NOT NULL,
    corrected_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- 5. SPEND_AGGREGATES
-- Precomputed weekly rollups per user/category. This is what the
-- trend model reads from — avoids recalculating from raw receipts
-- every time the bot checks for a nudge-worthy trend.
-- Populate this via a scheduled job (daily/weekly cron).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS spend_aggregates (
    agg_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES profiles(profile_id),
    category        TEXT NOT NULL,
    period_start    TEXT NOT NULL,          -- ISO date, Monday of that week
    period_type     TEXT NOT NULL DEFAULT 'week',  -- 'week' or 'month'
    total_spent     REAL NOT NULL,
    receipt_count   INTEGER NOT NULL DEFAULT 0,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(profile_id, category, period_start, period_type)
);

CREATE INDEX IF NOT EXISTS idx_agg_profile_period
    ON spend_aggregates(profile_id, period_start);

-- ------------------------------------------------------------
-- 6. NUDGES_SENT
-- Log of every trend message actually pushed to a user.
-- Prevents duplicate nudges and gives you a record to evaluate
-- "did this nudge actually change behavior" later.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nudges_sent (
    nudge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id),  -- Telegram delivery target
    profile_id      INTEGER REFERENCES profiles(profile_id),      -- which profile this nudge is about
    category        TEXT,                   -- NULL if it's an overall-spend nudge
    nudge_type      TEXT NOT NULL,          -- 'under_trend', 'over_trend', 'milestone'
    predicted_value REAL,                   -- what the model expected
    actual_value    REAL,                   -- what actually happened
    message_text    TEXT,
    sent_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nudges_user
    ON nudges_sent(user_id, sent_at);

-- ------------------------------------------------------------
-- 7. USER_ACTIVITY
-- Tracks last interaction per user, drives the "haven't logged
-- anything today" proactive check-in nudge.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_activity (
    user_id           INTEGER PRIMARY KEY REFERENCES users(user_id),
    last_message_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_checkin_sent TEXT
);

-- ------------------------------------------------------------
-- 8. RECURRING_TRANSACTIONS
-- Detected debit-order-like patterns (same merchant, similar
-- amount, similar day-of-month, 2+ consecutive months).
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recurring_transactions (
    recurring_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id       INTEGER NOT NULL REFERENCES profiles(profile_id),
    merchant         TEXT NOT NULL,
    typical_amount   REAL NOT NULL,
    typical_day      INTEGER,        -- day of month it usually hits
    last_seen        TEXT,
    last_reminded_at TEXT,           -- prevents duplicate day-before reminders
    active           INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_recurring_profile
    ON recurring_transactions(profile_id);

-- ------------------------------------------------------------
-- 9. PENDING_IMPORTS
-- Tracks that a newly-logged-in user has been asked for their
-- 3-month history upload, so we only ask once.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_imports (
    profile_id       INTEGER PRIMARY KEY REFERENCES profiles(profile_id),
    asked_at         TEXT NOT NULL DEFAULT (datetime('now')),
    fulfilled        INTEGER NOT NULL DEFAULT 0
);

-- ------------------------------------------------------------
-- 10. INCOME
-- Money received — salary, refunds, gifts. Separate from receipts
-- (which tracks spend) so "how much did I get paid" and "how much
-- did I spend" are two clean, independent questions.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS income (
    income_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id        INTEGER NOT NULL REFERENCES profiles(profile_id),
    source            TEXT,                  -- e.g. "CASHFOCUS SALARIS", "JM", "SARS refund"
    amount            REAL NOT NULL,
    received_at       TEXT NOT NULL,
    category          TEXT DEFAULT 'other_income',  -- 'salary', 'transfer_in', 'other_income'
    source_type       TEXT DEFAULT 'telegram_text', -- 'telegram_text' or 'bulk_import'
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_income_profile_date
    ON income(profile_id, received_at);

-- ------------------------------------------------------------
-- 11. BALANCE_SNAPSHOTS
-- Captures the real bank balance from the last transaction in each
-- bulk import (Excel/PDF). "How much money is left" reads the most
-- recent snapshot, then adjusts for any live-logged transactions
-- dated after it — this is an honest estimate anchored to real bank
-- data, not a running total the bot might have drifted from.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS balance_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES profiles(profile_id),
    balance         REAL NOT NULL,
    as_of_date      TEXT NOT NULL,      -- date of the last transaction in the import
    source          TEXT DEFAULT 'bulk_import',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_balance_snapshots_profile
    ON balance_snapshots(profile_id, as_of_date);

-- ------------------------------------------------------------
-- 12. ADMIN_USERS
-- Identity-based admin access — no secret typed into chat after the
-- very first bootstrap. Being in this table (by Telegram user_id) is
-- what grants access to /admin, /ping, /addadmin, etc.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admin_users (
    user_id         INTEGER PRIMARY KEY REFERENCES users(user_id),
    added_by        INTEGER,            -- user_id of whoever added them, NULL for bootstrap admin
    label           TEXT,               -- friendly name, e.g. "Dean"
    added_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ------------------------------------------------------------
-- 13. BUDGETS
-- One row per category (or "overall") per profile. Rolling monthly
-- window (30 days), matching the day-window pattern used everywhere
-- else in the bot.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS budgets (
    budget_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER NOT NULL REFERENCES profiles(profile_id),
    category        TEXT NOT NULL,      -- specific category, group term, or "overall"
    amount          REAL NOT NULL,
    period_days     INTEGER NOT NULL DEFAULT 30,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(profile_id, category)
);

-- ------------------------------------------------------------
-- 14. Import cooldown settings — added as columns on profiles
-- (SQLite doesn't support IF NOT EXISTS on ALTER TABLE ADD COLUMN,
-- so these are also listed in COLUMN_MIGRATIONS in bot_beta.py for
-- self-healing on already-deployed volumes)
-- ------------------------------------------------------------
-- profiles.import_cooldown_days INTEGER DEFAULT 90
-- profiles.last_import_at TEXT
