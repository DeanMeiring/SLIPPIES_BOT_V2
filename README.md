# SLIPPIES_BOT_V2
# Spend Trend Bot

A Telegram bot that learns a person's spending patterns from their receipts
(and optionally imported history) and proactively nudges them when they're
trending under or over their normal spend — no app to download, just a chat.

Built as a standalone companion to [SlippiesBot](../SlippiesBot) (Gemini-powered
receipt OCR bot), but designed to work independently.

---

## The idea, in one paragraph

Most budgeting tools (Sage, bank apps) only see the *bank transaction* —
merchant name + total. They can't see what was actually bought. This bot
works from the *receipt itself*, giving it itemized, category-level detail
bank feeds structurally cannot capture — plus it catches cash and
non-bank-linked purchases entirely invisible to bank-feed tools. Over time,
it learns each user's normal spending rhythm and messages them when
something's notably different, the way a person who knows your habits would.

---

## Why this isn't just "another AI wrapper"

See the full reasoning in chat history, but the short version: calling an
LLM API alone is a thin, easily-copied wrapper. The actual moat is:

1. **Itemized, categorized personal spend data** — accumulates per user,
   can't be replicated by a competitor starting from zero.
2. **A correction feedback loop** — every time a user fixes a
   mis-categorized item, that's logged and becomes training data that makes
   the categorizer measurably better over time, specific to real usage
   patterns (not generic receipt data).
3. **Per-user personalization that compounds** — the longer someone uses it,
   the better the trend predictions get, which is a real switching cost a
   generic competitor can't undercut on day one.

---

## Core features (target state)

- **Receipt ingestion** — photo sent via Telegram → Gemini OCR → parsed into
  merchant, total, line items, categories (same pattern as SlippiesBot).
- **Bulk history import** — user can upload a 3-month spending Excel/CSV
  (bank export, personal budget sheet, etc.) to seed the bot with a real
  baseline immediately instead of waiting months for data to accumulate.
  Column mapping is handled flexibly (LLM-assisted mapping, since personal
  exports never have consistent column names/order).
- **Category correction** — user can correct a wrong category via a Telegram
  command; correction is logged for future model improvement.
- **Trend detection** — compares current week/month spend (per category and
  overall) against the user's own historical baseline.
- **Proactive nudges** — bot messages the user when it detects a meaningful
  trend ("under trend," "over trend," or a milestone), without being asked.

---

## Data model

Full schema in [`schema.sql`](./schema.sql). Summary:

| Table | Purpose |
|---|---|
| `users` | One row per Telegram user, opt-in/timezone settings |
| `receipts` | One row per processed receipt or imported transaction. `source` distinguishes live receipts (`telegram_photo`) from bulk-imported history (`bulk_import`); `import_batch_id` groups rows from the same upload |
| `receipt_items` | Line-item detail per receipt — the actual data moat. Only populated for real receipts, not bulk bank-style imports (those typically lack item-level detail) |
| `category_corrections` | Every user correction to a category — future training data |
| `spend_aggregates` | Precomputed weekly/monthly rollups per user/category — this is what the trend model reads from, not raw receipts |
| `nudges_sent` | Log of every message actually pushed — prevents duplicate nudges, lets us later evaluate whether nudges actually change behavior |

---

## Build approach (rules-first, ML later)

Following a walk-forward-validation mindset (same as the ETH 30m XGBoost
model in the STOCK repo): don't reach for ML before there's enough real
per-user data to justify it.

1. **Phase 1 — Rules-based baseline.** Trend = "average of last N weeks."
   Simple, transparent, works from week one, needs no training data.
2. **Phase 2 — Lightweight regression once data exists.** Once users have a
   few months of real (or imported) history, move to a proper per-category
   forecast (XGBoost regression, same family as the ETH model) predicting
   expected spend, flagging meaningful deviation from it.
3. **Phase 3 — Personalized categorizer.** Once `category_corrections` has
   enough volume, fine-tune or few-shot-tune categorization specifically to
   real usage patterns (South African retailers, VAT handling, common
   merchant name variants) instead of relying on generic Gemini output.

---

## Open questions / decisions still to make

- [ ] Onboarding UX: is bulk Excel import optional ("upload for faster
      results") or a required first step?
- [ ] Nudge frequency/tone — how often is helpful vs. annoying?
- [ ] Whether to eventually support exporting itemized data *into* Sage or
      similar tools, positioning this as complementary rather than
      competing with existing accounting software.
- [ ] Fine-tuning vs. few-shot prompting for Phase 3 — depends on how much
      correction data actually accumulates.

---

## Related context

- Sibling project: **SlippiesBot** — Gemini-powered Telegram receipt bot,
  deployed on Railway, SQLite-backed licensing, `/myfile` command. This
  project either extends it or runs alongside it depending on how the repo
  structure ends up working in practice.
- Author's ML background: Random Forest / XGBoost from an academic capstone
  (Medfly outbreak prediction), plus a working XGBoost pipeline already in
  production for crypto price forecasting (see `STOCK` repo) — same
  predict → evaluate → retrain loop applies here.