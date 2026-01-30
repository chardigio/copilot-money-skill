---
name: copilot-money
description: Query and analyze personal finance data from the Copilot Money Mac app. Use when the user asks about their spending, transactions, account balances, budgets, or financial trends from Copilot Money.
---

# Copilot Money

Query the local SQLite database from the Copilot Money Mac app to analyze transactions, spending patterns, and account balances.

## Database Location

```
~/Library/Group Containers/group.com.copilot.production/database/CopilotDB.sqlite
```

## Schema

### Transactions Table

Primary table for all financial transactions.

| Column | Type | Description |
|--------|------|-------------|
| id | TEXT | Primary key |
| date | DATE | Transaction date |
| name | TEXT | Merchant/transaction name |
| original_name | TEXT | Raw name from bank |
| amount | DOUBLE | Transaction amount (positive = expense) |
| iso_currency_code | TEXT | Currency (e.g., "USD") |
| account_id | TEXT | Linked account reference |
| category_id | TEXT | Category reference |
| pending | BOOLEAN | Whether transaction is pending |
| recurring | BOOLEAN | Whether transaction is recurring |
| user_note | TEXT | User-added notes |
| user_deleted | BOOLEAN | Soft-deleted by user |

### accountDailyBalance Table

Daily balance snapshots per account.

| Column | Type | Description |
|--------|------|-------------|
| date | TEXT | Snapshot date |
| account_id | TEXT | Account reference |
| current_balance | DOUBLE | Balance on that date |
| available_balance | DOUBLE | Available balance |

## Common Queries

### Recent Transactions
```sql
SELECT date, name, amount, category_id
FROM Transactions
WHERE user_deleted = 0
ORDER BY date DESC
LIMIT 20;
```

### Monthly Spending Summary
```sql
SELECT strftime('%Y-%m', date) as month, SUM(amount) as total
FROM Transactions
WHERE amount > 0 AND user_deleted = 0
GROUP BY month
ORDER BY month DESC;
```

### Spending by Category
```sql
SELECT category_id, SUM(amount) as total, COUNT(*) as count
FROM Transactions
WHERE amount > 0 AND user_deleted = 0 AND date >= date('now', '-30 days')
GROUP BY category_id
ORDER BY total DESC;
```

### Search Transactions
```sql
SELECT date, name, amount
FROM Transactions
WHERE name LIKE '%SEARCH_TERM%' AND user_deleted = 0
ORDER BY date DESC;
```

## Usage

Use `sqlite3` to query the database:

```bash
sqlite3 ~/Library/Group\ Containers/group.com.copilot.production/database/CopilotDB.sqlite "YOUR_QUERY"
```

For formatted output:
```bash
sqlite3 -header -column ~/Library/Group\ Containers/group.com.copilot.production/database/CopilotDB.sqlite "YOUR_QUERY"
```

## Notes

- Category IDs are opaque strings - group by them for analysis but names aren't stored locally
- Amounts are positive for expenses, negative for income
- Filter `user_deleted = 0` to exclude deleted transactions
- The database is actively used by the app; read-only access is safe
