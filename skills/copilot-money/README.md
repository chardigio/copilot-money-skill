# Copilot Money Skill

A Claude Code skill for querying personal finance data from the [Copilot Money](https://copilot.money) Mac app.

## What It Does

This skill enables Claude to access and analyze your financial data stored locally by Copilot Money, including:

- Transaction history
- Spending patterns and trends
- Account balances over time
- Category-based expense analysis

## Requirements

- macOS with Copilot Money app installed
- Copilot Money must have synced data locally (the app stores a SQLite database)

## Example Uses

- "How much did I spend on restaurants last month?"
- "Show me my largest transactions this year"
- "What's my monthly spending trend?"
- "Find all Amazon purchases"

## Data Location

Copilot Money stores its data at:
```
~/Library/Group Containers/group.com.copilot.production/database/CopilotDB.sqlite
```

## Privacy Note

All data stays local - this skill queries the SQLite database directly on your Mac. No data is sent to external services.
