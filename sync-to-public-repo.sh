#!/usr/bin/env bash
# Sync the copilot-money skill from the monorepo to the public chardigio/copilot-money-skill repo.
#
# This script generates the Claude Code Plugin structure from the monorepo's
# .claude/skills/copilot-money/ directory.
#
# Usage:
#   .claude/skills/copilot-money/sync-to-public-repo.sh [--push]
#
# The --push flag will commit and push changes to the public repo.
# Without it, the script only copies files (dry run for review).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$SCRIPT_DIR"
PUSH=false

if [[ "${1:-}" == "--push" ]]; then
    PUSH=true
fi

# Clone or update the public repo
PUBLIC_REPO_DIR="/tmp/copilot-money-skill-sync"
if [[ -d "$PUBLIC_REPO_DIR/.git" ]]; then
    echo "Updating existing clone..."
    git -C "$PUBLIC_REPO_DIR" fetch origin
    git -C "$PUBLIC_REPO_DIR" checkout main
    git -C "$PUBLIC_REPO_DIR" reset --hard origin/main
else
    echo "Cloning public repo..."
    git clone git@github.com:chardigio/copilot-money-skill.git "$PUBLIC_REPO_DIR"
fi

# Clean the repo (keep .git)
find "$PUBLIC_REPO_DIR" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf {} +

# Create the plugin directory structure
mkdir -p "$PUBLIC_REPO_DIR/.claude-plugin"
mkdir -p "$PUBLIC_REPO_DIR/skills/copilot-money"

# Copy skill files into the proper plugin skills directory
cp "$SKILL_DIR/SKILL.md" "$PUBLIC_REPO_DIR/skills/copilot-money/SKILL.md"
cp "$SKILL_DIR/README.md" "$PUBLIC_REPO_DIR/skills/copilot-money/README.md"

# Generate plugin.json
cat > "$PUBLIC_REPO_DIR/.claude-plugin/plugin.json" << 'PLUGIN_JSON'
{
  "name": "copilot-money",
  "version": "1.0.0",
  "description": "Query and analyze personal finance data from the Copilot Money Mac app",
  "author": {
    "name": "Charlie DiGiovanna",
    "url": "https://github.com/chardigio"
  },
  "repository": "https://github.com/chardigio/copilot-money-skill",
  "license": "MIT",
  "keywords": [
    "copilot-money",
    "personal-finance",
    "budgeting",
    "transactions",
    "sqlite",
    "macos"
  ]
}
PLUGIN_JSON

# Generate README.md with installation instructions
cat > "$PUBLIC_REPO_DIR/README.md" << 'README'
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

## Data Locations

**SQLite Database** (transactions, balances):
```
~/Library/Group Containers/group.com.copilot.production/database/CopilotDB.sqlite
```

**Firestore LevelDB Cache** (recurring names, budgets, investments):
```
~/Library/Containers/com.copilot.production/Data/Library/Application Support/firestore/__FIRAPP_DEFAULT/copilot-production-22904/main/*.ldb
```

## Privacy

All data stays local - this skill queries the local databases directly on your Mac. No data is sent to external services.

## License

MIT
README

# Generate LICENSE
cat > "$PUBLIC_REPO_DIR/LICENSE" << 'LICENSE'
MIT License

Copyright (c) 2026 Charlie DiGiovanna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
LICENSE

echo ""
echo "Generated plugin structure:"
find "$PUBLIC_REPO_DIR" -not -path '*/.git/*' -not -path '*/.git' | sort | sed "s|$PUBLIC_REPO_DIR/||" | grep -v '^$'
echo ""

if [[ "$PUSH" == true ]]; then
    cd "$PUBLIC_REPO_DIR"
    git add -A
    if git diff --cached --quiet; then
        echo "No changes to commit."
    else
        git commit -m "Scaffold Claude Code Plugin format for SkillsMP indexing

- Add .claude-plugin/plugin.json with plugin metadata
- Move SKILL.md into skills/copilot-money/ directory structure
- Add MIT LICENSE
- Update README with plugin installation instructions
- Structure follows the Agent Skills specification"
        git push origin main
        echo ""
        echo "Pushed to chardigio/copilot-money-skill!"
    fi
else
    echo "Dry run complete. Use --push to commit and push changes."
    echo ""
    echo "Review the generated files at: $PUBLIC_REPO_DIR"
fi
