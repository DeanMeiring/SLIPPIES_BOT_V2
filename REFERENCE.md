# SlippiesBot V2 — Complete Project Reference

A Telegram bot that tracks personal/business spending through natural
conversation — no app to install, just chat. Built on Gemini AI for
understanding, SQLite for storage, hosted on Railway.

---

## Core Concept

Users log spending by texting naturally ("spent 150 on lunch at Nandos")
or sending receipt photos. The bot reads, categorizes, and stores it.
Users can then ask questions in plain language ("how much did I spend
on groceries this month?") and get real answers pulled from their
actual logged data.

The bot supports multiple separate "profiles" per person (e.g. personal
vs business) and can import full bank statements (Excel or PDF) to
bootstrap real history instead of starting from zero.

---

## Architecture

- **Bot framework:** `python-telegram-bot` (async, polling-based)
- **AI:** Google Gemini — `gemini-3.1-flash-lite` for high-volume
  categorization/parsing (cheap), `gemini-3.5-flash` for spending
  advice (low-frequency, quality matters more than cost)
- **Database:** SQLite, single file on a Railway persistent volume
- **Hosting:** Railway (separate service + volume from anything else)
- **File parsing:** `openpyxl` (Excel), `pdfplumber` (PDF)

---

## Key Design Decisions

### Profiles, not just users
Early on, license codes were tied directly to Telegram accounts. This
was redesigned: **each license code owns its own separate data profile.**
One Telegram account can hold multiple profiles (personal, business,
managing a family member's finances) with completely isolated data.
`/login CODE` switches your *active* profile.

### Self-healing schema migrations
Every schema change goes in `COLUMN_MIGRATIONS` — the bot checks for
missing columns on every boot and adds them automatically. **No volume
wipe is ever needed for additive changes** (new columns/tables). This
was built after several early iterations required manual wipes.

### Tiered AI usage
High-frequency, low-complexity calls (categorization, parsing) use the
cheap Flash-Lite model. Low-frequency, quality-matters calls (spending
advice) use the stronger Flash model. Estimated cost: ~$0.0002 per
logged transaction — negligible at realistic scale.

### Identity-based admin access
No secret typed into chat after initial setup. `/becomeadmin SECRET`
bootstraps the first admin once, then permanently disables itself.
Every admin action after that checks the person's actual Telegram
identity against an `admin_users` table.

---

## Features Built & Tested

### Core logging
- Natural language transaction logging ("spent 150 on lunch")
- Receipt photo OCR via Gemini vision — merchant, amount, category
  extracted automatically
- Income logging ("got paid 5000 salary")
- Photos are stored via Telegram's own `file_id` (near-zero cost —
  Telegram hosts the file, we just keep a reference)

### Querying
- Category-specific spend queries ("how much on groceries this month")
- **Category grouping** — broad terms (transport, eating_out, food,
  utilities, medical) correctly sum across all their sub-categories
  instead of Gemini guessing one
- Income queries
- Balance estimate ("how much money is left") — anchored to the real
  balance from your last bank statement import, adjusted for anything
  logged since
- Full transaction list export (Excel, two sheets: Expenses + Income)

### Fixing mistakes
- "delete last" — removes the most recently logged entry
- "actually it was 90 not 150" — corrects the last entry's amount
- Both restricted to live-logged entries only (text/photo) — bulk-
  imported bank data is never touched via chat, for safety
- Fixed a real tie-breaking bug where rapid same-second entries could
  delete the wrong one (now uses row ID as a reliable tiebreaker)

### Bulk import (Excel & PDF)
- Standard Bank format parsed via fast regex (validated against real
  6-month statements — totals matched the bank's own summary to the
  cent, including balance capture)
- Generic fallback for other banks: Gemini reads the column headers/
  layout and maps them dynamically
- PDF support via `pdfplumber` text extraction, same dual-parser
  approach
- **Deduplication:** strict exact-match (date + amount + merchant) for
  bulk imports — catches both within-batch and re-upload duplicates.
  Fuzzy match (±1 day, amount, similar merchant name) for live-logged
  entries, with a ⚠️ warning shown inline
- Balance snapshot captured from the last transaction in any import —
  powers the balance estimate feature

### Debit orders
- Bank-labeled debit orders/service agreements registered immediately
  on import (no need to wait for pattern detection)
- Reference-code normalization (e.g. strips per-month unique codes so
  "MATIESGYMA-CIS5-EUNS-QP-260601" and "MATIESGYMA-2NMY-JOLG-OE-260701"
  are recognized as the same recurring gym payment)
- Day-before reminder, sent automatically

### Budgets (just built)
- Set per-category or overall budgets ("set my grocery budget to 2000")
- Check status on demand ("how am I doing on my budget")
- Upsert-safe (setting again updates in place, no duplicates)

### Spending advice (just built)
- "Where can I save money" pulls real transaction/budget/recurring-
  payment data and feeds it to the stronger Gemini model for genuinely
  specific advice (not generic tips)

### Admin system
- `/becomeadmin` (one-time bootstrap) → `/addadmin @username` from then on
- `/admin` — overview of all profiles: active/slowing/quiet tiers,
  transaction counts, last-active dates. **No financial amounts or
  merchant details shown** — engagement visibility only
- `/ping CODE [message]` — nudge whoever's currently active on a profile
- Two-stage account deletion for safety:
  - `/deactivate CODE` — blocks logins, data stays fully intact
    (reversible)
  - `/reactivate CODE` — undoes it
  - `/harddelete CODE` — only works if already deactivated; sends a
    backup Excel first; requires `/harddelete CODE CONFIRM` to actually
    permanently erase everything (cascades across every table)
- `/adminhelp` — dedicated admin command reference, invisible to
  non-admins (verified: gated by a direct DB check against
  `admin_users`, not client-side hiding)

### Onboarding
- First-time intro message (once ever, per Telegram user)
- Welcome-back message on subsequent logins, shows active profile
- New-profile prompt to upload 3-month history, repeats every ~90 days
  if still not fulfilled
- `/help` — full command reference, admin section only shown to admins

### Scheduled jobs (all via `python-telegram-bot`'s JobQueue)
- Daily: check-in nudge if inactive 48+ hours
- Daily: recurring debit order reminders (day before due)
- Daily: import reminder for profiles still missing a bulk import

---

## Database Schema (key tables)

- `users` — Telegram identity
- `license_codes` / `profiles` — access codes, each owning one data profile
- `user_active_profile` — which profile a Telegram user currently has active
- `receipts` / `receipt_items` — expense records
- `income` — income records
- `recurring_transactions` — detected/confirmed debit orders
- `balance_snapshots` — real bank balance anchors from imports
- `budgets` — spending goals per category/profile
- `admin_users` — identity-based admin access
- `pending_imports` — tracks the 3-month-import ask/reminder cycle

---

## Backlog (Designed, Not Yet Built)

- **Import cooldown + admin override** — per-profile cooldown
  (default 90 days, admin-adjustable), `/requestupload <reason>` pings
  admins for early access, `/allowupload CODE` grants one-time bypass
- **"What if" hypothetical impact** — simple version (arithmetic against
  budgets) buildable now; predictive/ML version is a future project
  once more real usage data exists

---

## Deployment

- Railway service `SLIPPIES_BOT_V2`, dedicated volume, SQLite at
  `/data/slippies_v3.db`
- Code changes: edit directly via GitHub's web editor (most reliable
  method found tonight — bypasses local sync issues), commit to `main`,
  Railway auto-redeploys
- Schema changes are additive-only by design (self-healing migrations)
  — no volume wipes needed going forward
- Environment variables: `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`,
  `ADMIN_SECRET` (one-time bootstrap only), `DATABASE_PATH`

---

## Testing Philosophy (what made tonight's work reliable)

Every feature was tested against realistic data before being shared —
not just "looks right." Real bugs caught this way, before deployment:
- Connection isolation bug in dedup (within-batch duplicates weren't
  caught because the check ran on a separate uncommitted connection)
- Tie-breaking bug in delete/last (second-resolution timestamps caused
  wrong entry deletion on rapid same-second logs)
- Missing function headers from earlier manual edits (caught via full
  module-load tests, not just syntax checks)

Standard test pattern used throughout: real SQLite operations against
a throwaway test database, asserting exact expected values, not just
"it ran without erroring."
