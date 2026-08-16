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
    logged_in_at    TEXT
);

-- ------------------------------------------------------------
-- 2. RECEIPTS
-- One row per processed receipt (the raw event).
-- If merging into SlippiesBot later, this maps to its existing receipt table.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
    merchant        TEXT,
    total_amount    REAL NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'ZAR',
    purchased_at    TEXT NOT NULL,          -- actual date on the receipt
    processed_at    TEXT NOT NULL DEFAULT (datetime('now')),
    raw_ocr_text    TEXT,                   -- keep Gemini's raw output for future re-parsing
    source          TEXT DEFAULT 'telegram_photo',  -- 'telegram_photo' or 'bulk_import'
    import_batch_id TEXT                    -- groups rows from the same uploaded file, NULL for live receipts
);

CREATE INDEX IF NOT EXISTS idx_receipts_user_date
    ON receipts(user_id, purchased_at);

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
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
    category        TEXT NOT NULL,
    period_start    TEXT NOT NULL,          -- ISO date, Monday of that week
    period_type     TEXT NOT NULL DEFAULT 'week',  -- 'week' or 'month'
    total_spent     REAL NOT NULL,
    receipt_count   INTEGER NOT NULL DEFAULT 0,
    computed_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, category, period_start, period_type)
);

CREATE INDEX IF NOT EXISTS idx_agg_user_period
    ON spend_aggregates(user_id, period_start);

-- ------------------------------------------------------------
-- 6. NUDGES_SENT
-- Log of every trend message actually pushed to a user.
-- Prevents duplicate nudges and gives you a record to evaluate
-- "did this nudge actually change behavior" later.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nudges_sent (
    nudge_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(user_id),
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