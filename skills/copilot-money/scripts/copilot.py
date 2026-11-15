#!/usr/bin/env python3
"""Query Copilot Money's local data with the names and rules the app itself uses.

Copilot keeps its data in three places on a Mac:

* `CopilotDB.sqlite`: transactions and daily account balances, with ids only.
* The Firestore LevelDB cache: categories, accounts, budgets, recurrings,
  holdings, securities (see `firestore_cache.py`).
* `widget-data/*.json`: numbers the app computed for its widgets.

`sync` joins the first two into one SQLite file, so any question becomes one SQL
query with real names. The report commands (`spending`, `budgets`, ...) encode
the rules the app uses to get its totals. Each rule was checked against the
app's own widget numbers (`doctor` repeats that check), because a plain
`SUM(amount)` over `Transactions` is wrong in four ways:

1. A pending row stays in the table after its posted row arrives. The posted row
   points back with `pending_transaction_id`, and the app counts only one.
2. A split keeps the original (parent) row, and its children carry the parts.
3. Transfers between your own accounts (`type = 'internal_transfer'`) and
   income are not spending.
4. The app's "spent" for the current month also adds the recurrings that are
   still due before the month ends.

Read-only: nothing here writes to Copilot's files. The derived database holds
no tokens or receipts, because the cache contains bank access tokens and we
copy only allow-listed fields.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import firestore_cache as fc  # noqa: E402

GROUP = Path.home() / "Library/Group Containers/group.com.copilot.production"
SOURCE_DB = GROUP / "database/CopilotDB.sqlite"
WIDGET_DIR = GROUP / "widget-data"
OUT_DB = Path(os.environ.get("COPILOT_MONEY_DB", Path.home() / ".cache/copilot-money/copilot.db"))
STALE_SECONDS = 15 * 60

SECRET_KEY_RE = re.compile(r"token|secret|receipt|password|oauth|akoya|^code$|fcm|appsflyer|transcend", re.I)

# --------------------------------------------------------------------------
# Building the enriched database
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE categories (
  id TEXT PRIMARY KEY, name TEXT, emoji TEXT, color TEXT, excluded INTEGER,
  parent_id TEXT, is_other INTEGER, sort_order INTEGER
);
CREATE TABLE items (
  id TEXT PRIMARY KEY, institution_id TEXT, institution_name TEXT, provider TEXT,
  login_required INTEGER, disconnected INTEGER, is_manual INTEGER, latest_fetch TEXT, error TEXT
);
CREATE TABLE accounts (
  id TEXT PRIMARY KEY, item_id TEXT, name TEXT, nickname TEXT, official_name TEXT, mask TEXT,
  type TEXT, subtype TEXT, institution_name TEXT, iso_currency_code TEXT,
  current_balance REAL, available_balance REAL, credit_limit REAL,
  user_hidden INTEGER, user_closed INTEGER, user_deleted INTEGER, dashboard_active INTEGER,
  is_manual INTEGER, latest_balance_update TEXT, group_id TEXT, group_leader INTEGER
);
CREATE TABLE recurrings (
  id TEXT PRIMARY KEY, name TEXT, emoji TEXT, state TEXT, frequency TEXT,
  amount REAL, min_amount REAL, max_amount REAL, match_string TEXT,
  category_id TEXT, latest_date TEXT
);
CREATE TABLE budgets (
  id TEXT PRIMARY KEY, category_id TEXT, amount REAL, amounts_json TEXT
);
CREATE TABLE securities (
  id TEXT PRIMARY KEY, ticker TEXT, name TEXT, type TEXT,
  close_price REAL, current_price REAL, iso_currency_code TEXT, last_update TEXT
);
CREATE TABLE holdings (
  account_id TEXT, security_id TEXT, quantity REAL, institution_price REAL,
  institution_value REAL, cost_basis REAL, iso_currency_code TEXT, last_update TEXT
);
CREATE TABLE balance_history (
  account_id TEXT, date TEXT, current_balance REAL, available_balance REAL, PRIMARY KEY (account_id, date)
);
CREATE TABLE amazon_orders (
  id TEXT PRIMARY KEY, date TEXT, account_id TEXT, total REAL, match_state TEXT,
  items_json TEXT
);
"""

VIEWS = """
-- Every transaction with names joined in. `day` is the local calendar day
-- (the app stores midnight Eastern as a UTC timestamp, so the first 10
-- characters are the right day).
CREATE VIEW tx AS
SELECT t.id, substr(t.date, 1, 10) AS day, substr(t.date, 1, 7) AS month,
       COALESCE(NULLIF(t.name_override, ''), t.name) AS name, t.original_name,
       t.amount, t.iso_currency_code, t.type, t.pending,
       t.category_id, c.name AS category, c.emoji AS category_emoji, COALESCE(c.excluded, 0) AS category_excluded,
       COALESCE(c.parent_id, c.id) AS parent_category_id, COALESCE(pc.name, c.name) AS parent_category,
       t.account_id, COALESCE(NULLIF(a.nickname, ''), a.name) AS account, a.mask AS account_mask,
       a.type AS account_type, a.institution_name,
       t.recurring_id, r.name AS recurring, t.user_note, t.user_reviewed,
       t.is_amazon, t.parent_transaction_id, t.children_transaction_ids, t.tag_ids,
       COALESCE(t.user_deleted, 0) AS user_deleted, COALESCE(t.plaid_deleted, 0) AS plaid_deleted,
       (COALESCE(t.user_deleted, 0) = 0 AND COALESCE(t.plaid_deleted, 0) = 0
        AND NOT (t.pending = 1 AND EXISTS (
            SELECT 1 FROM transactions p WHERE p.pending = 0
              AND p.pending_transaction_id IN (t.id, t.transaction_id)))
        AND COALESCE(t.children_transaction_ids, '[]') IN ('', '[]')) AS live
FROM transactions t
LEFT JOIN categories c ON c.id = t.category_id
LEFT JOIN categories pc ON pc.id = c.parent_id
LEFT JOIN accounts a ON a.id = t.account_id
LEFT JOIN recurrings r ON r.id = t.recurring_id;

-- The rows the app counts as spending (positive = money out, refunds negative).
-- Excluded categories stay in, flagged by `category_excluded`, because the app
-- still shows their spending; drop them for totals.
CREATE VIEW spend AS SELECT * FROM tx WHERE live = 1 AND type = 'regular';

CREATE VIEW income AS SELECT * FROM tx WHERE live = 1 AND type = 'income';

-- Balance snapshots from both sources. SQLite misses some accounts entirely
-- (often investment accounts), and the Firestore cache has them. On a day in
-- both, the Firestore value wins because the app writes it first. A snapshot
-- exists only on days the balance changed: carry the last value forward.
CREATE VIEW balances AS
SELECT account_id, date AS day, current_balance, available_balance, 'firestore' AS source FROM balance_history
UNION ALL
SELECT b.account_id, b.date, b.current_balance, b.available_balance, 'sqlite' FROM daily_balances b
WHERE NOT EXISTS (SELECT 1 FROM balance_history h WHERE h.account_id = b.account_id AND h.date = b.date);

-- Accounts that count toward net worth, the way the app counts them: every
-- account that is not deleted, and for a merged group only the leader (the
-- members repeat the leader's money). Hidden and closed accounts stay in: their
-- balance is 0 now, but their past balances are part of the history.
CREATE VIEW networth_accounts AS
SELECT * FROM accounts
WHERE COALESCE(user_deleted, 0) = 0
  AND NOT (group_id IS NOT NULL AND COALESCE(group_leader, 0) = 0);
"""

_BLOB_COLUMNS = (
    "plaid_category_strings", "venmo_extra_data", "children_transaction_ids", "amazon",
    "intelligence_suggested_category_ids", "tag_ids", "internal_tx_match", "associated_member_uids", "type",
)


def _b(v: Any) -> Optional[int]:
    return None if v is None else int(bool(v))


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, dt.datetime) else v


def _rows_from_docs(docs: Sequence[fc.Doc]) -> Dict[str, List[tuple]]:
    rows: Dict[str, List[tuple]] = {k: [] for k in (
        "categories", "items", "accounts", "recurrings", "budgets", "securities", "holdings", "amazon_orders", "balance_history")}
    seen_cats = set()

    def add_cat(cid: str, f: Dict[str, Any], parent: Optional[str]) -> None:
        if cid in seen_cats:
            return
        seen_cats.add(cid)
        rows["categories"].append((cid, f.get("name"), f.get("emoji"), f.get("color"), _b(f.get("excluded")),
                                   f.get("parent_category_id") or parent, _b(f.get("is_other")), f.get("order")))

    for d in docs:
        f, col = d.fields, d.collection
        if col == "categories" and "users" in d.parent_ids:
            add_cat(d.id, f, None)
            for child in f.get("children_categories") or []:
                if isinstance(child, dict) and child.get("id"):
                    add_cat(child["id"], child, d.id)
        elif col == "items" and len(d.segments) == 2:
            rows["items"].append((d.id, f.get("institution_id"), f.get("institution_name"), f.get("provider"),
                                  _b(f.get("login_required")), _b(f.get("disconnected")), _b(f.get("is_manual")),
                                  _iso(f.get("latest_fetch")), json.dumps(f.get("error")) if f.get("error") else None))
        elif col == "accounts" and "items" in d.parent_ids:
            rows["accounts"].append((
                d.id, d.parent_ids["items"], f.get("name"), f.get("nickname"), f.get("official_name"), f.get("mask"),
                f.get("type"), f.get("subtype"), f.get("institution_name"), f.get("iso_currency_code"),
                f.get("current_balance"), f.get("available_balance"), f.get("limit"),
                _b(f.get("user_hidden")), _b(f.get("user_closed")), _b(f.get("user_deleted")),
                _b(f.get("dashboard_active")), _b(f.get("is_manual")), _iso(f.get("latest_balance_update")),
                f.get("group_id"), _b(f.get("group_leader"))))
            for h in f.get("holdings") or []:
                if isinstance(h, dict):
                    rows["holdings"].append((d.id, h.get("security_id"), h.get("quantity"), h.get("institution_price"),
                                             h.get("institution_value"), h.get("cost_basis"),
                                             h.get("iso_currency_code"), _iso(h.get("last_update"))))
        elif col == "recurring" and "users" in d.parent_ids:
            rows["recurrings"].append((d.id, f.get("name"), f.get("emoji"), f.get("state"), f.get("frequency"),
                                       f.get("amount"), f.get("min_amount"), f.get("max_amount"),
                                       f.get("match_string"), f.get("category_id"), f.get("latest_date")))
        elif col == "budgets" and "users" in d.parent_ids:
            rows["budgets"].append((d.id, f.get("category_id"), f.get("amount"),
                                    json.dumps(f.get("amounts") or {}, sort_keys=True)))
        elif col == "securities" and len(d.segments) == 2:
            rows["securities"].append((d.id, f.get("ticker_symbol"), f.get("name"), f.get("type"),
                                       f.get("close_price"), f.get("current_price"),
                                       f.get("iso_currency_code"), _iso(f.get("last_update"))))
        elif col == "balance_history" and "accounts" in d.parent_ids:
            rows["balance_history"].append((d.parent_ids["accounts"], d.id, f.get("current_balance"),
                                            f.get("available_balance")))
        elif col == "orders" and "amazon" in d.parent_ids:
            det = f.get("details") or {}
            rows["amazon_orders"].append((d.id, f.get("date"), f.get("account_id"), det.get("total"),
                                          f.get("match_state"), json.dumps(fc.jsonable(f.get("items") or []))))
    return rows


def build_db(out: Path, source_db: Path, docs: Sequence[fc.Doc]) -> None:
    """Write the enriched database to `out` (atomically, through a temp file)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(f"file:{quote(str(tmp))}", uri=True)
    try:
        conn.executescript(SCHEMA)
        # The app holds the source open in WAL mode. A read-only URI connection
        # sees committed WAL pages, which a plain file copy would miss.
        conn.execute("ATTACH DATABASE ? AS src", (f"file:{quote(str(source_db))}?mode=ro",))
        blob_casts = ", ".join(
            f'CAST("{c}" AS TEXT) AS "{c}"' if c in _BLOB_COLUMNS else f'"{c}"'
            for c in [r[1] for r in conn.execute("PRAGMA src.table_info(Transactions)")])
        conn.execute(f"CREATE TABLE transactions AS SELECT {blob_casts} FROM src.Transactions")
        conn.execute("CREATE TABLE daily_balances AS SELECT * FROM src.accountDailyBalance")
        conn.commit()
        conn.execute("DETACH DATABASE src")
        for table, rows in _rows_from_docs(docs).items():
            if rows:
                marks = ",".join("?" * len(rows[0]))
                conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({marks})", rows)
        conn.executescript("""
            CREATE INDEX bh_acct ON balance_history(account_id, date);
            CREATE INDEX tx_date ON transactions(date);
            CREATE INDEX tx_cat ON transactions(category_id);
            CREATE INDEX tx_pending ON transactions(pending_transaction_id);
            CREATE INDEX tx_recurring ON transactions(recurring_id);
            CREATE INDEX bal_acct ON daily_balances(account_id, date);
        """)
        conn.executescript(VIEWS)
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("synced_at", dt.datetime.now(dt.timezone.utc).isoformat()),
            ("source_db", str(source_db)),
            ("firestore_documents", str(len(docs))),
        ])
        conn.commit()
    finally:
        conn.close()
    # The file holds a full copy of someone's finances: owner-only.
    os.chmod(tmp, 0o600)
    os.replace(tmp, out)


def _source_mtime() -> float:
    paths = [SOURCE_DB, SOURCE_DB.with_name(SOURCE_DB.name + "-wal")]
    try:
        paths += list(fc.find_cache_dir().iterdir())
    except FileNotFoundError:
        pass
    return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)


def ensure_db(force: bool = False, quiet: bool = False) -> sqlite3.Connection:
    """Open the enriched database, and rebuild it first when the app has newer data."""
    if force or not OUT_DB.exists() or (
            OUT_DB.stat().st_mtime < _source_mtime() and dt.datetime.now().timestamp() - OUT_DB.stat().st_mtime > 60) \
            or dt.datetime.now().timestamp() - OUT_DB.stat().st_mtime > STALE_SECONDS:
        if not SOURCE_DB.exists():
            sys.exit(f"Copilot Money database not found at {SOURCE_DB}. Is the Mac app installed and signed in?")
        if not quiet:
            print(f"syncing {OUT_DB} ...", file=sys.stderr)
        build_db(OUT_DB, SOURCE_DB, fc.load_documents())
    conn = sqlite3.connect(f"file:{quote(str(OUT_DB))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------
# Rules: recurrings and budgets
# --------------------------------------------------------------------------

_FREQ_MONTHS = {"monthly": 1, "bimonthly": 2, "quarterly": 3, "semi-annually": 6, "semiannually": 6,
                "annually": 12, "yearly": 12}
_FREQ_DAYS = {"weekly": 7, "biweekly": 14}


def add_period(d: dt.date, frequency: str) -> Optional[dt.date]:
    """Return the next due date after `d`, or None for an unknown frequency.

    Month-based steps clamp to the last day of a short month (Oct 31 -> Nov 30),
    which is what the app shows for a charge on the 31st.
    """
    if frequency in _FREQ_DAYS:
        return d + dt.timedelta(days=_FREQ_DAYS[frequency])
    months = _FREQ_MONTHS.get(frequency)
    if months is None:
        return None
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def due_dates(last_paid: dt.date, frequency: str, start: dt.date, end: dt.date) -> List[dt.date]:
    """Due dates after `last_paid` that fall in [start, end].

    If the first expected payment after `last_paid` is already before `start`,
    the payment was missed, and the app does not project the recurring at all
    (checked against the app: a monthly car payment last paid two months ago
    adds nothing to this month's "spent"). So we return no dates for it.
    """
    out: List[dt.date] = []
    d = add_period(last_paid, frequency)
    if d is None or d < start:
        return out
    while d is not None and d <= end:
        out.append(d)
        d = add_period(d, frequency)
    return out


def months_back(today: dt.date, n: int) -> str:
    """The `YYYY-MM` that is `n` calendar months before `today`'s month."""
    y, m = divmod(today.year * 12 + today.month - 1 - n, 12)
    return f"{y:04d}-{m + 1:02d}"


def month_bounds(month: str) -> Tuple[dt.date, dt.date]:
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


def upcoming_recurrings(conn: sqlite3.Connection, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Active recurrings still due in [start, end], with the amount the app expects.

    The last payment date is the later of the recurring's `latest_date` and its
    newest linked transaction. When the recurring has no `amount`, the app uses
    the newest linked transaction's amount, so we do too. A recurring whose next
    payment is already overdue on `start` is left out (see `due_dates`).
    """
    out = []
    for r in conn.execute("""
        SELECT r.*, c.name AS category,
               (SELECT max(substr(date,1,10)) FROM transactions t WHERE t.recurring_id = r.id
                  AND COALESCE(t.user_deleted,0) = 0) AS last_tx_day,
               (SELECT amount FROM transactions t WHERE t.recurring_id = r.id AND COALESCE(t.user_deleted,0) = 0
                  ORDER BY date DESC LIMIT 1) AS last_tx_amount
        FROM recurrings r LEFT JOIN categories c ON c.id = r.category_id
        WHERE r.state = 'active'"""):
        last = max(filter(None, [r["latest_date"], r["last_tx_day"]]), default=None)
        amount = r["amount"] if r["amount"] is not None else r["last_tx_amount"]
        if not last or amount is None:
            continue
        for d in due_dates(dt.date.fromisoformat(last[:10]), r["frequency"] or "", start, end):
            out.append({"id": r["id"], "name": r["name"], "category_id": r["category_id"],
                        "category": r["category"], "due": d.isoformat(), "amount": round(amount, 2),
                        "frequency": r["frequency"]})
    return sorted(out, key=lambda x: (x["due"], x["name"] or ""))


def budget_report(conn: sqlite3.Connection, month: str, today: Optional[dt.date] = None,
                  include_upcoming: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Spent, budget, and left per category, the way the app's budget screen computes them.

    * Leaf spent = live regular transactions in the month (refunds subtract).
    * For the current month, the app adds recurrings still due before month end.
      Past months have nothing upcoming; future months are all upcoming.
    * An excluded category has a budget of 0, but its spending still shows.
    * A parent's spent and budget are the sums over its children (plus any
      transactions filed on the parent itself).
    * A budget's `amounts` map can override the amount for one month (`YYYY-MM`).
    """
    today = today or dt.date.today()
    start, end = month_bounds(month)
    if include_upcoming is None:
        include_upcoming = end >= today
    spent: Dict[str, float] = {r[0]: r[1] for r in conn.execute(
        "SELECT category_id, sum(amount) FROM spend WHERE month = ? GROUP BY 1", (month,))}
    upcoming: Dict[str, float] = {}
    if include_upcoming:
        for u in upcoming_recurrings(conn, max(start, today), end):
            upcoming[u["category_id"]] = upcoming.get(u["category_id"], 0.0) + u["amount"]
    budgets: Dict[str, float] = {}
    for b in conn.execute("SELECT category_id, amount, amounts_json FROM budgets"):
        amt = json.loads(b["amounts_json"] or "{}").get(month, b["amount"])
        budgets[b["category_id"]] = float(amt or 0)

    cats = [dict(r) for r in conn.execute("SELECT * FROM categories ORDER BY sort_order")]
    children: Dict[str, List[str]] = {}
    for c in cats:
        if c["parent_id"]:
            children.setdefault(c["parent_id"], []).append(c["id"])

    def leaf(c: Dict[str, Any]) -> Dict[str, Any]:
        s = spent.get(c["id"], 0.0)
        u = upcoming.get(c["id"], 0.0)
        bud = 0.0 if c["excluded"] else budgets.get(c["id"], 0.0)
        return {"spent_actual": s, "upcoming": u, "budget": bud}

    rows = {c["id"]: {**c, **leaf(c)} for c in cats}
    for pid, kids in children.items():
        if pid in rows:
            p = rows[pid]
            for k in ("spent_actual", "upcoming", "budget"):
                p[k] = p[k] + sum(rows[k_][k] for k_ in kids if k_ in rows)
    out = []
    for c in cats:
        r = rows[c["id"]]
        r["spent"] = round(r["spent_actual"] + r["upcoming"], 2)
        r["left"] = round(r["budget"] - r["spent"], 2)
        for k in ("spent_actual", "upcoming", "budget"):
            r[k] = round(r[k], 2)
        r["is_parent"] = c["id"] in children
        out.append(r)
    return out


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


def emit(rows: Iterable[Any], fmt: str, columns: Optional[List[str]] = None) -> None:
    rows = [dict(r) for r in rows]
    if fmt == "json":
        print(json.dumps(rows, indent=1, default=str, ensure_ascii=False))
        return
    if not rows:
        print("(no rows)")
        return
    columns = columns or list(rows[0].keys())
    if fmt == "csv":
        w = csv.DictWriter(sys.stdout, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return

    def cell(v: Any) -> str:
        if isinstance(v, float):
            return f"{v:,.2f}"
        return "" if v is None else str(v)

    table = [[cell(r.get(c)) for c in columns] for r in rows]
    widths = [min(max(len(c), *(len(t[i]) for t in table)), 48) for i, c in enumerate(columns)]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for t in table:
        print("  ".join((v[: w - 1] + "…" if len(v) > w else v).rjust(w) if re.fullmatch(r"-?[\d,]+\.\d\d", v)
                        else (v[: w - 1] + "…" if len(v) > w else v).ljust(w) for v, w in zip(t, widths)))


def redact(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: ("<redacted>" if SECRET_KEY_RE.search(k) else redact(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    return v


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _period(args: argparse.Namespace) -> Tuple[str, str]:
    if getattr(args, "month", None):
        s, e = month_bounds(args.month)
        return s.isoformat(), e.isoformat()
    start = args.start or (dt.date.today() - dt.timedelta(days=30)).isoformat()
    end = args.end or dt.date.today().isoformat()
    return start, end


def cmd_sync(args: argparse.Namespace) -> None:
    conn = ensure_db(force=True)
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in (
        "transactions", "daily_balances", "categories", "accounts", "recurrings", "budgets", "securities",
        "holdings", "amazon_orders", "balance_history")}
    print(json.dumps({"db": str(OUT_DB), **counts}, indent=1))


def cmd_sql(args: argparse.Namespace) -> None:
    conn = ensure_db(force=args.sync)
    emit(conn.execute(args.query), args.format)


def cmd_schema(args: argparse.Namespace) -> None:
    conn = ensure_db()
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name"):
        print(sql + ";\n")


def cmd_categories(args: argparse.Namespace) -> None:
    conn = ensure_db()
    emit(conn.execute("""SELECT c.id, c.emoji, c.name, p.name AS parent, c.excluded
                         FROM categories c LEFT JOIN categories p ON p.id = c.parent_id
                         ORDER BY COALESCE(p.sort_order, c.sort_order), c.parent_id IS NOT NULL, c.sort_order"""),
         args.format)


def cmd_accounts(args: argparse.Namespace) -> None:
    conn = ensure_db()
    where = "" if args.all else "WHERE COALESCE(user_deleted,0)=0 AND COALESCE(user_closed,0)=0 AND COALESCE(user_hidden,0)=0"
    emit(conn.execute(f"""SELECT id, institution_name AS institution, COALESCE(NULLIF(nickname,''), name) AS name, mask,
                                 type, subtype, current_balance, available_balance, credit_limit,
                                 substr(latest_balance_update,1,16) AS updated
                          FROM accounts {where} ORDER BY type, current_balance DESC"""), args.format)


def cmd_transactions(args: argparse.Namespace) -> None:
    conn = ensure_db()
    start, end = _period(args)
    clauses, params = ["day BETWEEN ? AND ?"], [start, end]
    if not args.all:
        clauses.append("live = 1")
    for col, val in (("category", args.category), ("account", args.account)):
        if val:
            clauses.append(f"({col} LIKE ? OR parent_category LIKE ?)" if col == "category" else f"{col} LIKE ?")
            params += [f"%{val}%"] * (2 if col == "category" else 1)
    if args.search:
        clauses.append("(name LIKE ? OR original_name LIKE ? OR user_note LIKE ?)")
        params += [f"%{args.search}%"] * 3
    if args.min is not None:
        clauses.append("abs(amount) >= ?")
        params.append(args.min)
    if args.type:
        clauses.append("type = ?")
        params.append(args.type)
    emit(conn.execute(f"""SELECT day, name, amount, category, account, type,
                                 CASE WHEN pending=1 THEN 'pending' ELSE '' END AS status, user_note AS note
                          FROM tx WHERE {' AND '.join(clauses)} ORDER BY day DESC, abs(amount) DESC LIMIT ?""",
                      params + [args.limit]), args.format)


def cmd_spending(args: argparse.Namespace) -> None:
    conn = ensure_db()
    start, end = _period(args)
    group = {"category": "category", "parent": "parent_category", "merchant": "name", "account": "account",
             "month": "month", "day": "day"}[args.by]
    excl = "" if args.include_excluded else "AND category_excluded = 0"
    emit(conn.execute(f"""SELECT {group} AS {args.by}, round(sum(amount), 2) AS spent, count(*) AS n
                          FROM spend WHERE day BETWEEN ? AND ? {excl}
                          GROUP BY 1 ORDER BY {'1' if args.by in ('month','day') else 'spent DESC'} LIMIT ?""",
                      (start, end, args.limit)), args.format)


def cmd_cashflow(args: argparse.Namespace) -> None:
    conn = ensure_db()
    emit(conn.execute("""
        SELECT month,
               round(-sum(CASE WHEN type='income' THEN amount ELSE 0 END), 2) AS income,
               round(sum(CASE WHEN type='regular' AND category_excluded=0 THEN amount ELSE 0 END), 2) AS spent,
               round(-sum(CASE WHEN type='income' THEN amount ELSE 0 END)
                     - sum(CASE WHEN type='regular' AND category_excluded=0 THEN amount ELSE 0 END), 2) AS net
        FROM tx WHERE live = 1 AND month >= ? GROUP BY month ORDER BY month""",
                      (months_back(dt.date.today(), args.months - 1),)),
         args.format)


def cmd_budgets(args: argparse.Namespace) -> None:
    conn = ensure_db()
    month = args.month or dt.date.today().strftime("%Y-%m")
    rows = budget_report(conn, month)
    if not args.all:
        rows = [r for r in rows if r["budget"] or r["spent"]]
    for r in rows:
        r["category"] = ("" if not r["parent_id"] else "  ") + f"{r['emoji'] or ''} {r['name']}".strip()
    emit(rows, args.format, ["category", "spent", "budget", "left", "spent_actual", "upcoming", "excluded"])


def cmd_recurrings(args: argparse.Namespace) -> None:
    conn = ensure_db()
    if args.upcoming:
        today = dt.date.today()
        emit(upcoming_recurrings(conn, today, today + dt.timedelta(days=args.days)), args.format,
             ["due", "name", "amount", "category", "frequency"])
        return
    where = "" if args.all else "WHERE r.state = 'active'"
    emit(conn.execute(f"""SELECT r.emoji, r.name, r.state, r.frequency, r.amount, c.name AS category, r.latest_date,
                                 (SELECT count(*) FROM transactions t WHERE t.recurring_id = r.id) AS n_tx
                          FROM recurrings r LEFT JOIN categories c ON c.id = r.category_id {where}
                          ORDER BY r.state, r.frequency, r.amount DESC"""), args.format)


LIABILITY_TYPES = ("credit", "loan")


def current_networth(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Today's net worth as the app shows it.

    An investment account with holdings is valued at live security prices
    (quantity x current price), because its stored `current_balance` is only
    refreshed when the institution syncs. Liabilities are stored as positive
    numbers and subtract.
    """
    assets = debts = 0.0
    n = 0
    for a in conn.execute("""
        SELECT a.type, a.current_balance,
               (SELECT sum(h.quantity * COALESCE(s.current_price, h.institution_price))
                FROM holdings h LEFT JOIN securities s ON s.id = h.security_id
                WHERE h.account_id = a.id) AS live_value
        FROM networth_accounts a"""):
        n += 1
        if a["type"] in LIABILITY_TYPES:
            debts += abs(a["current_balance"] or 0)
        else:
            assets += a["live_value"] if a["live_value"] is not None else (a["current_balance"] or 0)
    return {"net_worth": round(assets - debts, 2), "assets": round(assets, 2), "debts": round(debts, 2), "accounts": n}


def networth_history(conn: sqlite3.Connection, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Net worth per day, carrying each account's last snapshot forward."""
    types = {r["id"]: r["type"] for r in conn.execute("SELECT id, type FROM networth_accounts")}
    series: Dict[str, List[Tuple[str, float]]] = {}
    for r in conn.execute("SELECT account_id, day, current_balance FROM balances ORDER BY day"):
        if r["account_id"] in types:
            series.setdefault(r["account_id"], []).append((r["day"], r["current_balance"] or 0.0))
    import bisect

    days_index = {aid: [d for d, _ in s] for aid, s in series.items()}
    out = []
    day = end
    while day >= start:
        key = day.isoformat()
        assets = debts = 0.0
        for aid, s in series.items():
            i = bisect.bisect_right(days_index[aid], key) - 1
            if i < 0:
                continue
            v = s[i][1]
            if types[aid] in LIABILITY_TYPES:
                debts += abs(v)
            else:
                assets += v
        out.append({"day": key, "net_worth": round(assets - debts, 2), "assets": round(assets, 2),
                    "debts": round(debts, 2)})
        day -= dt.timedelta(days=1)
    return out


def cmd_networth(args: argparse.Namespace) -> None:
    conn = ensure_db()
    today = dt.date.today()
    rows = [{"day": "now", **current_networth(conn)}]
    if args.days:
        step = max(1, args.every)
        hist = networth_history(conn, today - dt.timedelta(days=args.days), today - dt.timedelta(days=1))
        rows += hist[::step]
    emit(rows, args.format, ["day", "net_worth", "assets", "debts"])


def cmd_holdings(args: argparse.Namespace) -> None:
    conn = ensure_db()
    emit(conn.execute("""
        SELECT s.ticker, s.name, COALESCE(NULLIF(a.nickname,''), a.name) AS account,
               round(h.quantity, 4) AS quantity, COALESCE(s.current_price, h.institution_price) AS price,
               round(h.quantity * COALESCE(s.current_price, h.institution_price), 2) AS value, h.cost_basis
        FROM holdings h JOIN accounts a ON a.id = h.account_id LEFT JOIN securities s ON s.id = h.security_id
        WHERE COALESCE(a.user_deleted,0) = 0 AND h.quantity > 0
        ORDER BY value DESC"""), args.format)


def cmd_docs(args: argparse.Namespace) -> None:
    docs = [d for d in fc.load_documents() if d.collection == args.collection]
    if args.list or not args.collection:
        import collections
        emit([{"collection": k, "docs": v} for k, v in
              collections.Counter(d.collection for d in fc.load_documents()).most_common()], args.format)
        return
    print(json.dumps([{"path": d.path, **redact(fc.jsonable(d.fields))} for d in docs[: args.limit]],
                     indent=1, ensure_ascii=False))


def doctor_checks(conn: sqlite3.Connection, widget_dir: Path, today: dt.date) -> List[Dict[str, Any]]:
    """Compare our budget numbers with the per-category numbers the app wrote for its widgets."""
    out = []
    month = today.strftime("%Y-%m")
    ours = {r["id"]: r for r in budget_report(conn, month, today)}
    for f in sorted(widget_dir.glob("widgets-category-category_*.json")):
        w = json.loads(f.read_text())
        r = ours.get(w["id"])
        if r is None:
            out.append({"category": w.get("name"), "app_spent": w.get("spent"), "our_spent": None, "ok": False})
            continue
        ok = abs(r["spent"] - w.get("spent", 0)) < 0.01 and abs(r["left"] - w.get("left", 0)) < 0.01
        out.append({"category": w.get("name"), "app_spent": round(w.get("spent", 0), 2), "our_spent": r["spent"],
                    "app_left": round(w.get("left", 0), 2), "our_left": r["left"], "ok": ok})
    return out


def cmd_doctor(args: argparse.Namespace) -> None:
    print(f"source db:        {SOURCE_DB} ({'ok' if SOURCE_DB.exists() else 'MISSING'})")
    try:
        print(f"firestore cache:  {fc.find_cache_dir()}")
    except FileNotFoundError as e:
        print(f"firestore cache:  MISSING ({e})")
    conn = ensure_db(force=True)
    for k, v in conn.execute("SELECT key, value FROM meta"):
        print(f"{k + ':':18}{v}")
    if not WIDGET_DIR.exists():
        print("widget data missing: cannot cross-check against the app")
        return
    age = dt.datetime.now().timestamp() - max(f.stat().st_mtime for f in WIDGET_DIR.glob("widgets-category-*.json"))
    print(f"widget data age:  {age / 3600:.1f} h (the app rewrites it when it is open)")
    checks = doctor_checks(conn, WIDGET_DIR, dt.date.today())
    bad = [c for c in checks if not c["ok"]]
    print(f"budget cross-check: {len(checks) - len(bad)}/{len(checks)} categories match the app")
    if bad:
        emit(bad, args.format)
    nw_file = WIDGET_DIR / "widgets-account-netWorth"
    if nw_file.exists():
        app = json.loads(nw_file.read_text())
        ours = current_networth(conn)
        drift = abs(ours["net_worth"] - app.get("balance", 0)) / max(abs(app.get("balance", 0)), 1)
        # Live security prices move between the app's write and ours, so allow a small drift.
        print(f"net worth:        ours {ours['net_worth']:,.2f} vs app {app.get('balance', 0):,.2f} "
              f"({drift:.3%} apart; accounts {ours['accounts']} vs {app.get('accountsCount')}) "
              f"{'ok' if drift < 0.005 and ours['accounts'] == app.get('accountsCount') else 'CHECK'}")


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--format", "-f", choices=["table", "json", "csv"], default="table")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def period(p: argparse.ArgumentParser) -> None:
        p.add_argument("--month", help="YYYY-MM (overrides --start/--end)")
        p.add_argument("--start", help="YYYY-MM-DD (default: 30 days ago)")
        p.add_argument("--end", help="YYYY-MM-DD (default: today)")

    sub.add_parser("sync", help="rebuild the enriched database now").set_defaults(fn=cmd_sync)
    p = sub.add_parser("sql", help="run SQL against the enriched database")
    p.add_argument("query")
    p.add_argument("--sync", action="store_true", help="rebuild first")
    p.set_defaults(fn=cmd_sql)
    sub.add_parser("schema", help="print tables and views").set_defaults(fn=cmd_schema)
    sub.add_parser("categories", help="category tree").set_defaults(fn=cmd_categories)
    p = sub.add_parser("accounts", help="accounts and balances")
    p.add_argument("--all", action="store_true", help="include hidden, closed, deleted")
    p.set_defaults(fn=cmd_accounts)
    p = sub.add_parser("transactions", aliases=["tx"], help="list or search transactions")
    period(p)
    p.add_argument("--search", "-s")
    p.add_argument("--category", "-c")
    p.add_argument("--account", "-a")
    p.add_argument("--type", choices=["regular", "income", "internal_transfer"])
    p.add_argument("--min", type=float, help="minimum absolute amount")
    p.add_argument("--all", action="store_true", help="include deleted, superseded pending, split parents")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_transactions)
    p = sub.add_parser("spending", help="spending grouped by category, merchant, ...")
    period(p)
    p.add_argument("--by", choices=["category", "parent", "merchant", "account", "month", "day"], default="category")
    p.add_argument("--include-excluded", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(fn=cmd_spending)
    p = sub.add_parser("cashflow", help="income, spending, net per month")
    p.add_argument("--months", type=int, default=12)
    p.set_defaults(fn=cmd_cashflow)
    p = sub.add_parser("budgets", help="budget vs spent, as the app computes it")
    p.add_argument("--month", help="YYYY-MM (default: this month)")
    p.add_argument("--all", action="store_true", help="include categories with no budget and no spending")
    p.set_defaults(fn=cmd_budgets)
    p = sub.add_parser("recurrings", help="recurring definitions or upcoming charges")
    p.add_argument("--upcoming", action="store_true")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--all", action="store_true", help="include paused and archived")
    p.set_defaults(fn=cmd_recurrings)
    p = sub.add_parser("networth", help="net worth per day from daily balances")
    p.add_argument("--days", type=int, default=30, help="days of history (0 for today only)")
    p.add_argument("--every", type=int, default=1, help="show every Nth day")
    p.set_defaults(fn=cmd_networth)
    sub.add_parser("holdings", help="investment holdings").set_defaults(fn=cmd_holdings)
    p = sub.add_parser("docs", help="raw Firestore documents of one collection (secrets redacted)")
    p.add_argument("collection", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_docs)
    sub.add_parser("doctor", help="check paths and cross-check numbers with the app").set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
