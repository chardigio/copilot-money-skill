# Copilot Money Skill

A Claude Code plugin for querying personal finance data from the [Copilot Money](https://copilot.money) Mac app.

## Install

### As a Claude Code plugin (recommended)

```bash
claude plugin install copilot-money --source github:chardigio/copilot-money-skill
```

### Manual install

Copy the skill to your Claude Code skills directory:

```bash
mkdir -p ~/.claude/skills/copilot-money
curl -sL https://raw.githubusercontent.com/chardigio/copilot-money-skill/main/skills/copilot-money/SKILL.md \
  -o ~/.claude/skills/copilot-money/SKILL.md
```

## What It Does

This skill enables Claude to access and analyze your financial data stored locally by Copilot Money, including:

- Transaction history and spending patterns
- Account balances over time
- Category-based expense analysis
- Recurring transaction definitions (rent, subscriptions, etc.)
- Budget amounts and investment data

## Requirements

- macOS with Copilot Money app installed
- Copilot Money must have synced data locally (the app stores data in SQLite and Firestore LevelDB cache)

## Example Uses

- "How much did I spend on restaurants last month?"
- "Show me my largest transactions this year"
- "What's my monthly spending trend?"
- "Find all Amazon purchases"

## Privacy

All data stays local - this skill queries the local databases directly on your Mac. No data is sent to external services.

## License

MIT
