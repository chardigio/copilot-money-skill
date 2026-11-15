---
name: copilot-money
description: Query and analyze personal finance data from the Copilot Money Mac app. Use when the user asks about their spending, transactions, account balances, budgets, recurring charges, net worth, investments, or financial trends from Copilot Money.
---

# Copilot Money

Read the data that the Copilot Money Mac app keeps on disk, with real names and with the same
rules the app uses for its totals. Read-only: nothing here writes to Copilot.

All commands go through one script in this skill's directory (the "Base directory" shown when
the skill loads):

```bash
python3 <skill-dir>/scripts/copilot.py <command> [options]
```

It needs only the Python 3 standard library. The first command in a session builds an enriched
SQLite file at `~/.cache/copilot-money/copilot.db` (about 6 seconds). Later commands reuse it,
and rebuild it when the app has newer data or the file is older than 15 minutes.

## Start here

| Question | Command |
|----------|---------|
| Are the numbers right? | `doctor` (cross-checks every budget category and net worth against the app) |
| Spending this month, by category | `spending --month 2026-11` |
| Spending by parent category / merchant / account / month / day | `spending --by parent` (or `merchant`, `account`, `month`, `day`) |
| Budget vs spent, exactly as the app shows it | `budgets` or `budgets --month 2026-10` |
| Income, spending, and net per month | `cashflow --months 12` |
| Find transactions | `tx -s "whole foods" --start 2026-01-01`, `tx -c restaurants --month 2026-10`, `tx --min 500` |
| Accounts and balances | `accounts` (`--all` adds hidden, closed, deleted) |
| Net worth today and per day | `networth --days 90 --every 7` |
| Recurring charges | `recurrings`; upcoming: `recurrings --upcoming --days 30` |
| Investments | `holdings` |
| Category tree | `categories` |
| Anything else | `sql "SELECT ..."` (see the schema below; `schema` prints it) |
| Raw Firestore documents | `docs --list`, then `docs <collection>` (secrets redacted) |

Every command takes `-f json` or `-f csv` before the command name, for example
`copilot.py -f json budgets`. Period options: `--month YYYY-MM`, or `--start` / `--end`
(`YYYY-MM-DD`, default the last 30 days).

## How the app counts money (verified against the app)

Each rule below was checked against numbers the app computed itself (its widget files). With all
of them, `budgets` matched the app to the cent for 42 of 42 categories. A plain
`SUM(amount)` over the raw table is wrong, so use the `spend` view or the commands.

1. **Sign.** Positive `amount` is money out. Income and refunds are negative.
2. **Pending duplicates.** A pending row stays after its posted row arrives. The posted row has
   `pending_transaction_id` = the pending row's `id`. Count only the posted row. A pending row with
   no posted row yet counts.
3. **Splits.** A split keeps the original row (it has `children_transaction_ids`). Its children
   (with `parent_transaction_id`) carry the parts. Count only the children.
4. **Types.** `type` is `regular` (spending), `income`, or `internal_transfer` (between your own
   accounts). Spending is `regular` only.
5. **Deleted.** Drop rows with `user_deleted = 1` or `plaid_deleted = 1`.
6. **Excluded categories.** A category with `excluded = 1` (for example rent or business costs)
   still shows its spending, but the app leaves it out of totals and gives it a budget of 0.
   `spending` and `cashflow` drop excluded categories unless you pass `--include-excluded`.
7. **Parent categories.** A parent's spent and budget are the sums of its children.
8. **Budget "spent" includes upcoming recurrings.** For the current month, the app adds each
   active recurring still due before the month ends. It projects from the last payment by the
   recurring's frequency (a charge on the 31st falls on the 30th in a short month). When the
   recurring has no amount, it uses the newest linked transaction's amount. A recurring whose next
   payment is already overdue (a missed payment) adds nothing. `budgets` shows `spent_actual` and
   `upcoming` apart, plus their sum as `spent`.
9. **Budget overrides.** A budget's `amounts` map (`{"YYYY-MM": amount}`) replaces the amount for one month.
10. **Dates.** `date` holds local midnight as a UTC timestamp (`2026-11-02 05:00:00.000`). Its first
    10 characters are the local day. The views expose this as `day` and `month`.
11. **Net worth.** Count every account that is not deleted. Hidden and closed accounts count (their
    balance is 0 now, but their past balances are part of the history). In a merged account group,
    count only the leader. Credit and loan balances are positive numbers that subtract. Investment
    accounts with holdings are worth quantity x live price. Today's value matches the app within
    live price drift. The daily history is approximate (see Limits).

## Enriched database

`~/.cache/copilot-money/copilot.db` (owner-only file; override with `COPILOT_MONEY_DB`).

### Views (use these first)

| View | What it holds |
|------|---------------|
| `tx` | Every transaction with names: `day`, `month`, `name` (the user's rename if any), `amount`, `type`, `pending`, `category`, `category_emoji`, `category_excluded`, `parent_category`, `account`, `account_mask`, `account_type`, `institution_name`, `recurring`, `user_note`, `user_reviewed`, `is_amazon`, `live` (1 = counts, after rules 2, 3, 5) |
| `spend` | `tx` rows that are spending: `live = 1 AND type = 'regular'` |
| `income` | `tx` rows that are income |
| `balances` | Balance snapshots per account and day, from SQLite and Firestore merged. A row exists only on days the balance changed |
| `networth_accounts` | The accounts that count toward net worth (rule 11) |

### Tables

| Table | Source | Columns |
|-------|--------|---------|
| `transactions` | SQLite | Every column of the app's table; JSON blobs (`tag_ids`, `children_transaction_ids`, `amazon`, `venmo_extra_data`, `plaid_category_strings`, ...) cast to text |
| `daily_balances` | SQLite | `date`, `account_id`, `current_balance`, `available_balance`, `limit`, `source` |
| `categories` | Firestore | `id`, `name`, `emoji`, `color`, `excluded`, `parent_id`, `is_other`, `sort_order` |
| `accounts` | Firestore | `id`, `item_id`, `name`, `nickname`, `official_name`, `mask`, `type` (depository, credit, loan, investment, real-estate), `subtype`, `institution_name`, `current_balance`, `available_balance`, `credit_limit`, `user_hidden`, `user_closed`, `user_deleted`, `group_id`, `group_leader`, `latest_balance_update` |
| `items` | Firestore | Bank connections: `institution_name`, `login_required`, `disconnected`, `latest_fetch`, `error` |
| `recurrings` | Firestore | `name`, `emoji`, `state` (active, paused, archived), `frequency` (weekly, monthly, semi-annually, annually, ...), `amount`, `min_amount`, `max_amount`, `match_string`, `category_id`, `latest_date` |
| `budgets` | Firestore | `category_id`, `amount`, `amounts_json` (per-month overrides) |
| `balance_history` | Firestore | `account_id`, `date`, `current_balance` (fills accounts that SQLite lacks) |
| `holdings` | Firestore | `account_id`, `security_id`, `quantity`, `institution_price`, `institution_value`, `cost_basis` |
| `securities` | Firestore | `ticker`, `name`, `type`, `close_price`, `current_price` |
| `amazon_orders` | Firestore | `date`, `total`, `match_state`, `items_json` (item names, prices, links) |
| `meta` | Script | `synced_at`, `source_db`, `firestore_documents` |

### Example queries

```bash
S=<skill-dir>/scripts/copilot.py
# Top merchants this year, excluded categories left out
python3 $S sql "SELECT name, round(sum(amount),2) AS spent, count(*) n FROM spend
                WHERE day >= '2026-01-01' AND category_excluded = 0 GROUP BY name ORDER BY spent DESC LIMIT 15"
# Month-over-month for one parent category
python3 $S sql "SELECT month, round(sum(amount),2) FROM spend WHERE parent_category = 'Going Out' GROUP BY month"
# Transactions the user has not reviewed
python3 $S sql "SELECT day, name, amount, category FROM tx WHERE live = 1 AND user_reviewed = 0 ORDER BY day DESC"
# Amazon items bought in a month
python3 $S sql "SELECT date, total, items_json FROM amazon_orders WHERE date LIKE '2026-10%'"
# Card utilization
python3 $S sql "SELECT name, mask, current_balance, credit_limit, round(100.0*current_balance/credit_limit,1) AS pct
                FROM accounts WHERE type = 'credit' AND credit_limit > 0 AND COALESCE(user_closed,0) = 0"
```

## Where the data lives

| Data | Path |
|------|------|
| Transactions, daily balances | `~/Library/Group Containers/group.com.copilot.production/database/CopilotDB.sqlite` (WAL mode) |
| Firestore offline cache (names, budgets, recurrings, accounts, holdings) | `~/Library/Containers/com.copilot.production/Data/Library/Application Support/firestore/__FIRAPP_DEFAULT/<project>/main/` (LevelDB). Newer app builds use `copilot-production-22904.copilot-production-firestore`, older ones `copilot-production-22904`. If both exist, the script uses the one with more data |
| Numbers the app computed for widgets | `~/Library/Group Containers/group.com.copilot.production/widget-data/*.json`: per-category `spent`/`left`, accounts, recent transactions, net worth by day (`widgets-account-netWorth`). `doctor` uses these as the answer key |

`scripts/firestore_cache.py` decodes the cache: LevelDB tables and logs (with Snappy), newest
write wins, deletions hide older values, and each value is a Firestore `MaybeDocument` protobuf.
Collections in the cache include `users/<uid>/categories`, `users/<uid>/budgets`,
`users/<uid>/recurring`, `items/<id>/accounts/<id>` (with `holdings`), `.../balance_history/<date>`,
`.../holdings_history`, `.../statements`, `securities`, `investment_prices`,
`investment_performance`, `investment_splits`, and `amazon/<id>/orders`.

## Safety

- Read-only. The script opens the app's SQLite file with `mode=ro` and copies the LevelDB folder
  (an APFS clone) before it reads, so it never touches the app's files.
- The Firestore cache holds bank access tokens, OAuth tokens, and App Store receipts. The enriched
  database copies only allow-listed fields, and `docs` redacts any key that looks like a secret.
  Do not print raw `.ldb` content with `strings`.
- Treat output as private financial data. Do not send it to outside services unless the user asks.

## Limits

- **Not in the local data:** tag names (only `tag_ids` exist, and they are usually empty) and goals.
- **Net worth history** comes from the local cache, which holds only part of the server's history.
  It matches the app on many days and is usually within a few hundred dollars on the rest. Use
  today's value (`networth --days 0`) when an exact number matters.
- **Freshness:** the local data is as new as the app's last sync. Open the app to refresh it, then
  run `sync`. `doctor` shows the age of the widget data.
- If `doctor` reports mismatches, the app's rules may have changed. Compare the `spent_actual` and
  `upcoming` columns of `budgets` with the app's budget screen to find which rule moved.
