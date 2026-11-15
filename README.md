# Copilot Money Skill

A Claude Code plugin for querying personal finance data from the [Copilot Money](https://copilot.money) Mac app.

## Install

### As a Claude Code plugin (recommended)

```bash
claude plugin install copilot-money --source github:chardigio/copilot-money-skill
```

### Manual install

Copy the skill folder (SKILL.md and scripts/) to your Claude Code skills directory:

```bash
git clone --depth 1 https://github.com/chardigio/copilot-money-skill /tmp/copilot-money-skill
mkdir -p ~/.claude/skills
cp -R /tmp/copilot-money-skill/skills/copilot-money ~/.claude/skills/
```

## What It Does

This skill enables Claude to access and analyze your financial data stored locally by Copilot Money, including:

- Transactions with real category, account, and recurring names
- Spending and budgets computed with the same rules as the app (checked against the app's own numbers)
- Upcoming recurring charges, cash flow per month, and net worth today and over time
- Investment holdings valued at live prices, and Amazon order items
- Ad hoc SQL over one enriched SQLite file
- A `doctor` command that cross-checks the numbers against the app

## Requirements

- macOS with Copilot Money app installed
- Copilot Money must have synced data locally (the app stores data in SQLite and a Firestore LevelDB cache)
- Python 3 (the standard library only, nothing to install)

## Example Uses

- "How much did I spend on restaurants last month?"
- "Show me my largest transactions this year"
- "What's my monthly spending trend?"
- "Find all Amazon purchases"
- "Am I over budget this month?"
- "What recurring charges are due in the next two weeks?"
- "How has my net worth changed this year?"

## Privacy

All data stays local. The skill reads the local files on your Mac in read-only mode and sends nothing to external services. The derived database leaves out the bank tokens that the app's cache holds.

## License

MIT
