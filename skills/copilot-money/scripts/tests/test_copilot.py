"""Tests for the Copilot Money readers, with synthetic data only.

This folder is published to a public repo, so no fixture may hold real
financial data. The helpers below encode tiny LevelDB files and Firestore
protobufs by hand, which also pins the byte formats the decoder relies on.

Run: python3 -m pytest .agents/skills/copilot-money/scripts/tests -q
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import copilot  # noqa: E402
import firestore_cache as fc  # noqa: E402

# --------------------------------------------------------------------------
# Encoders (test-only)
# --------------------------------------------------------------------------


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pb(num: int, value) -> bytes:
    if isinstance(value, bool):
        return varint(num << 3) + varint(int(value))
    if isinstance(value, int):
        return varint(num << 3) + varint(value & ((1 << 64) - 1))
    if isinstance(value, float):
        return varint((num << 3) | 1) + struct.pack("<d", value)
    if isinstance(value, str):
        value = value.encode()
    return varint((num << 3) | 2) + varint(len(value)) + value


def fs_value(v) -> bytes:
    if v is None:
        return pb(11, 0)
    if isinstance(v, bool):
        return pb(1, v)
    if isinstance(v, int):
        return pb(2, v)
    if isinstance(v, float):
        return pb(3, v)
    if isinstance(v, str):
        return pb(17, v)
    if isinstance(v, list):
        return pb(9, b"".join(pb(1, fs_value(x)) for x in v))
    if isinstance(v, dict):
        return pb(6, b"".join(pb(1, pb(1, k) + pb(2, fs_value(x))) for k, x in v.items()))
    raise TypeError(v)


def fs_document(path: str, fields: dict) -> bytes:
    name = f"projects/p/databases/(default)/documents/{path}"
    body = pb(1, name) + b"".join(pb(2, pb(1, k) + pb(2, fs_value(v))) for k, v in fields.items())
    return pb(2, body)  # MaybeDocument.document


def remote_key(path: str) -> bytes:
    return b"\x85remote_document\x00\x01" + path.encode()


def snappy_literal(data: bytes) -> bytes:
    """Snappy stream with one literal element (valid, if not compressed)."""
    n = len(data) - 1
    if n < 60:
        return varint(len(data)) + bytes([n << 2]) + data
    return varint(len(data)) + bytes([61 << 2]) + n.to_bytes(2, "little") + data


def block(entries) -> bytes:
    """A LevelDB block with no prefix sharing and one restart point."""
    out = bytearray()
    for k, v in entries:
        out += varint(0) + varint(len(k)) + varint(len(v)) + k + v
    out += struct.pack("<I", 0) + struct.pack("<I", 1)
    return bytes(out)


def table(entries, compress: bool = False) -> bytes:
    """A minimal SSTable: one data block, an empty metaindex, an index, a footer."""
    data = block([(k + struct.pack("<Q", (seq << 8) | kind), v) for k, seq, kind, v in entries])
    payload = snappy_literal(data) if compress else data
    out = bytearray(payload + bytes([1 if compress else 0]) + b"\0\0\0\0")
    meta_off = len(out)
    meta = block([])
    out += meta + b"\0\0\0\0\0"
    idx_off = len(out)
    last = entries[-1][0] + b"\xff" * 8
    idx = block([(last, varint(0) + varint(len(payload)))])
    out += idx + b"\0\0\0\0\0"
    footer = varint(meta_off) + varint(len(meta)) + varint(idx_off) + varint(len(idx))
    footer += b"\0" * (40 - len(footer)) + b"\x57\xfb\x80\x8b\x24\x75\x47\xdb"
    return bytes(out + footer)


def log(batches) -> bytes:
    """A write-ahead log with one FULL record per batch."""
    out = bytearray()
    for seq, ops in batches:
        body = struct.pack("<QI", seq, len(ops))
        for kind, k, v in ops:
            body += bytes([kind]) + varint(len(k)) + k
            if kind == 1:
                body += varint(len(v)) + v
        out += b"\0\0\0\0" + struct.pack("<H", len(body)) + b"\x01" + body
    return bytes(out)


# --------------------------------------------------------------------------
# Snappy, LevelDB, protobuf
# --------------------------------------------------------------------------


def test_snappy_overlapping_copy():
    # literal "abc", then copy length 9 at offset 3 (repeats the pattern)
    stream = varint(12) + b"\x08abc" + bytes([((9 - 4) << 2) | 1, 3])
    assert fc.snappy_decompress(stream) == b"abc" * 4


def test_snappy_long_literal():
    data = bytes(range(256)) * 2
    assert fc.snappy_decompress(snappy_literal(data)) == data


@pytest.mark.parametrize("compress", [False, True])
def test_read_table_returns_entries(compress):
    entries = [(b"a", 5, 1, b"one"), (b"b", 6, 0, b"")]
    got = [(e.key, e.seq, e.kind, e.value) for e in fc.read_table(table(entries, compress))]
    assert got == entries


def test_read_db_newest_write_wins_and_delete_hides(tmp_path):
    (tmp_path / "000001.ldb").write_bytes(table([(b"k1", 1, 1, b"old"), (b"k2", 2, 1, b"keep")]))
    (tmp_path / "000002.log").write_bytes(log([(10, [(1, b"k1", b"new"), (0, b"k2", b"")])]))
    assert fc.read_db(tmp_path) == {b"k1": b"new"}


def test_read_db_skips_damaged_file(tmp_path):
    (tmp_path / "000001.ldb").write_bytes(b"not a table at all" * 4)
    (tmp_path / "000002.log").write_bytes(log([(1, [(1, b"k", b"v")])]))
    assert fc.read_db(tmp_path) == {b"k": b"v"}


def test_read_log_stops_at_truncated_record():
    data = log([(1, [(1, b"a", b"1")]), (2, [(1, b"b", b"2")])])
    got = [e.key for e in fc.read_log(data[:-3])]
    assert got == [b"a"]


def test_parse_document_decodes_all_value_types():
    raw = fs_document("users/U1/categories/C1", {
        "name": "Food", "excluded": False, "order": -3, "ratio": 0.5, "none": None,
        "ids": ["a", "b"], "nested": {"x": 1}})
    doc = fc.parse_maybe_document(raw)
    assert doc.collection == "categories" and doc.id == "C1" and doc.parent_ids == {"users": "U1"}
    assert doc.fields == {"name": "Food", "excluded": False, "order": -3, "ratio": 0.5, "none": None,
                          "ids": ["a", "b"], "nested": {"x": 1}}


def test_parse_maybe_document_ignores_deleted_marker():
    no_document = pb(1, pb(1, "projects/p/databases/(default)/documents/users/U/x/1"))
    assert fc.parse_maybe_document(no_document) is None


def test_iter_documents_reads_only_remote_documents():
    kv = {
        remote_key("items/I1"): fs_document("items/I1", {"institution_name": "Bank"}),
        b"\x85target_document\x00\x01x": fs_document("items/I2", {"institution_name": "Other"}),
        remote_key("items/bad"): b"\xff\xff\xff",
    }
    assert [d.path for d in fc.iter_documents(kv)] == ["items/I1"]


def test_find_cache_dir_prefers_the_folder_with_most_data(tmp_path):
    big = tmp_path / "proj.firestore" / "main"
    small = tmp_path / "proj" / "main"
    for d, size in ((big, 5000), (small, 10)):
        d.mkdir(parents=True)
        (d / "CURRENT").write_text("MANIFEST-1\n")
        (d / "000001.ldb").write_bytes(b"x" * size)
    # The stale folder is touched last, so a newest-mtime pick would choose it.
    (small / "000002.log").write_bytes(b"y")
    assert fc.find_cache_dir(tmp_path) == big


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def test_add_period_clamps_to_month_end():
    assert copilot.add_period(dt.date(2026, 10, 31), "monthly") == dt.date(2026, 11, 30)
    assert copilot.add_period(dt.date(2026, 1, 31), "monthly") == dt.date(2026, 2, 28)
    assert copilot.add_period(dt.date(2026, 3, 1), "weekly") == dt.date(2026, 3, 8)
    assert copilot.add_period(dt.date(2025, 11, 22), "annually") == dt.date(2026, 11, 22)
    assert copilot.add_period(dt.date(2026, 1, 1), "someday") is None


def test_due_dates_repeats_weekly_inside_window():
    got = copilot.due_dates(dt.date(2026, 11, 10), "weekly", dt.date(2026, 11, 14), dt.date(2026, 11, 30))
    assert got == [dt.date(2026, 11, 17), dt.date(2026, 11, 24)]


def test_due_dates_skips_a_missed_payment():
    # Last paid in September: October was missed, so nothing is projected for November.
    got = copilot.due_dates(dt.date(2026, 9, 23), "monthly", dt.date(2026, 11, 14), dt.date(2026, 11, 30))
    assert got == []


def test_months_back_crosses_year():
    assert copilot.months_back(dt.date(2026, 2, 15), 2) == "2025-12"
    assert copilot.months_back(dt.date(2026, 2, 15), 0) == "2026-02"


def test_redact_hides_tokens_at_any_depth():
    got = copilot.redact({"access_token": "s", "oauth": {"x": 1}, "a": [{"id_token": "t", "name": "ok"}]})
    assert got == {"access_token": "<redacted>", "oauth": "<redacted>", "a": [{"id_token": "<redacted>", "name": "ok"}]}


# --------------------------------------------------------------------------
# Enriched database
# --------------------------------------------------------------------------

TX_COLUMNS = ("id", "transaction_id", "account_id", "name", "name_override", "original_name", "amount",
              "pending", "pending_transaction_id", "recurring_id", "user_deleted", "plaid_deleted",
              "category_id", "date", "type", "user_note", "user_reviewed", "children_transaction_ids",
              "parent_transaction_id", "iso_currency_code", "is_amazon", "tag_ids")


def make_source(path: Path, txs):
    conn = sqlite3.connect(path)
    cols = ", ".join(f'"{c}"' for c in TX_COLUMNS)
    conn.execute(f'CREATE TABLE "Transactions" ({cols})')
    conn.execute('CREATE TABLE "accountDailyBalance" ("date" TEXT, "account_id" TEXT, "limit" DOUBLE, '
                 '"available_balance" DOUBLE, "current_balance" DOUBLE, "priceRangeLow" DOUBLE, '
                 '"priceRangeMedium" DOUBLE, "priceRangeHigh" DOUBLE, "source" TEXT)')
    for t in txs:
        row = {c: None for c in TX_COLUMNS}
        row.update({"type": "regular", "pending": 0, "user_deleted": 0, "plaid_deleted": 0, "account_id": "A1",
                    "children_transaction_ids": b"[]", "tag_ids": b"[]"})
        row.update(t)
        conn.execute(f"INSERT INTO Transactions VALUES ({','.join('?' * len(TX_COLUMNS))})",
                     [row[c] for c in TX_COLUMNS])
    conn.executemany("INSERT INTO accountDailyBalance (date, account_id, current_balance) VALUES (?, ?, ?)", [
        ("2026-11-01", "A1", 100.0), ("2026-11-01", "CARD", 40.0), ("2026-11-03", "A1", 150.0)])
    conn.commit()
    conn.close()


def doc(path: str, fields: dict) -> fc.Doc:
    return fc.Doc(path=path, fields=fields, segments=path.split("/"))


DOCS = [
    doc("users/U/categories/PARENT", {"name": "Food & Drink", "excluded": False, "order": 1}),
    doc("users/U/categories/FOOD", {"name": "Groceries", "emoji": "🥑", "excluded": False, "order": 2,
                                    "parent_category_id": "PARENT"}),
    doc("users/U/categories/EAT", {"name": "Restaurants", "excluded": False, "order": 3,
                                   "parent_category_id": "PARENT"}),
    doc("users/U/categories/BILLS", {"name": "Bills", "excluded": True, "order": 4}),
    doc("users/U/budgets/B1", {"category_id": "FOOD", "amount": 500.0, "amounts": {"2026-10": 300.0}}),
    doc("users/U/budgets/B2", {"category_id": "EAT", "amount": 200.0}),
    doc("users/U/budgets/B3", {"category_id": "BILLS", "amount": 999.0}),
    doc("users/U/recurring/R1", {"name": "Water", "state": "active", "frequency": "monthly", "amount": 50.0,
                                 "category_id": "BILLS", "latest_date": "2026-10-20"}),
    doc("users/U/recurring/R2", {"name": "Games", "state": "active", "frequency": "annually",
                                 "category_id": "EAT"}),
    doc("users/U/recurring/R3", {"name": "Old gym", "state": "archived", "frequency": "monthly", "amount": 9.0,
                                 "category_id": "EAT", "latest_date": "2026-10-25"}),
    doc("items/I1", {"institution_name": "Bank", "access_token": "secret"}),
    doc("items/I1/accounts/A1", {"name": "Checking", "type": "depository", "current_balance": 150.0,
                                 "access_token": "secret"}),
    doc("items/I1/accounts/CARD", {"name": "Card", "type": "credit", "current_balance": 40.0}),
    doc("items/I1/accounts/OLD", {"name": "Old", "type": "depository", "current_balance": 0.0,
                                  "user_hidden": True}),
    doc("items/I1/accounts/MEMBER", {"name": "Dup", "type": "depository", "current_balance": 999.0,
                                     "group_id": "G", "group_leader": False}),
    doc("items/I1/accounts/OLD/balance_history/2026-10-01", {"current_balance": 25.0}),
    doc("items/I1/accounts/INV", {"name": "Brokerage", "type": "investment", "current_balance": 1000.0,
                                  "holdings": [{"security_id": "S1", "quantity": 10.0, "institution_price": 90.0}]}),
    doc("securities/S1", {"ticker_symbol": "TEST", "name": "Test Fund", "current_price": 110.0}),
]

TXS = [
    {"id": "t1", "name": "Market", "amount": 40.0, "category_id": "FOOD", "date": "2026-11-02 05:00:00.000"},
    # pending row and the posted row that replaced it: counted once
    {"id": "p1", "transaction_id": "p1", "name": "Cafe", "amount": 12.0, "category_id": "EAT", "pending": 1,
     "date": "2026-11-03 05:00:00.000"},
    {"id": "t2", "name": "Cafe", "amount": 15.0, "category_id": "EAT", "pending_transaction_id": "p1",
     "date": "2026-11-03 05:00:00.000"},
    # pending with no posted row yet: counted
    {"id": "p2", "name": "Bistro", "amount": 8.0, "category_id": "EAT", "pending": 1,
     "date": "2026-11-04 05:00:00.000"},
    # split parent and its children: only the children count
    {"id": "sp", "name": "Big shop", "amount": 100.0, "category_id": "FOOD",
     "children_transaction_ids": b'["c1","c2"]', "date": "2026-11-05 05:00:00.000"},
    {"id": "c1", "name": "Big shop", "amount": 60.0, "category_id": "FOOD", "parent_transaction_id": "sp",
     "date": "2026-11-05 05:00:00.000"},
    {"id": "c2", "name": "Big shop", "amount": 40.0, "category_id": "EAT", "parent_transaction_id": "sp",
     "date": "2026-11-05 05:00:00.000"},
    # refund subtracts
    {"id": "t3", "name": "Market refund", "amount": -10.0, "category_id": "FOOD", "date": "2026-11-06 05:00:00.000"},
    # deleted, transfer, income: not spending
    {"id": "d1", "name": "Gone", "amount": 500.0, "category_id": "FOOD", "user_deleted": 1,
     "date": "2026-11-06 05:00:00.000"},
    {"id": "x1", "name": "To savings", "amount": 300.0, "category_id": "FOOD", "type": "internal_transfer",
     "date": "2026-11-06 05:00:00.000"},
    {"id": "i1", "name": "Payroll", "amount": -2000.0, "type": "income", "date": "2026-11-01 05:00:00.000"},
    # excluded category
    {"id": "b1", "name": "Water", "amount": 50.0, "category_id": "BILLS", "recurring_id": "R1",
     "date": "2026-10-20 04:00:00.000"},
    # the recurring without an amount takes its newest linked transaction's amount
    {"id": "g1", "name": "Games", "amount": 43.54, "category_id": "EAT", "recurring_id": "R2",
     "date": "2025-11-22 05:00:00.000"},
]


@pytest.fixture()
def conn(tmp_path):
    src = tmp_path / "Copilot DB.sqlite"  # a space in the path, like the real one
    make_source(src, TXS)
    out = tmp_path / "out dir" / "copilot.db"
    copilot.build_db(out, src, DOCS)
    c = sqlite3.connect(out)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_build_db_never_stores_tokens(conn, tmp_path):
    assert b"secret" not in (tmp_path / "out dir" / "copilot.db").read_bytes()


def test_spend_view_applies_app_rules(conn):
    ids = sorted(r["id"] for r in conn.execute("SELECT id FROM spend WHERE month = '2026-11'"))
    assert ids == ["c1", "c2", "p2", "t1", "t2", "t3"]


def test_tx_view_joins_names(conn):
    r = conn.execute("SELECT * FROM tx WHERE id = 't1'").fetchone()
    assert (r["category"], r["parent_category"], r["account"], r["day"]) == (
        "Groceries", "Food & Drink", "Checking", "2026-11-02")


def test_budget_report_matches_app_rules(conn):
    rows = {r["name"]: r for r in copilot.budget_report(conn, "2026-11", today=dt.date(2026, 11, 14))}
    # Groceries: 40 + 60 - 10 = 90 against 500
    assert (rows["Groceries"]["spent"], rows["Groceries"]["budget"], rows["Groceries"]["left"]) == (90.0, 500.0, 410.0)
    # Restaurants: 15 + 8 + 40 = 63 actual, plus Games 43.54 due 2026-11-22
    assert rows["Restaurants"]["spent_actual"] == 63.0
    assert rows["Restaurants"]["upcoming"] == 43.54
    # Bills is excluded: budget 0, but the Water bill due 2026-11-20 still shows as spent
    assert (rows["Bills"]["budget"], rows["Bills"]["spent"], rows["Bills"]["left"]) == (0.0, 50.0, -50.0)
    # The parent sums its children
    assert rows["Food & Drink"]["budget"] == 700.0
    assert rows["Food & Drink"]["spent"] == round(90.0 + 63.0 + 43.54, 2)
    assert rows["Food & Drink"]["is_parent"] is True


def test_budget_report_uses_month_override_and_no_projection_for_past(conn):
    rows = {r["name"]: r for r in copilot.budget_report(conn, "2026-10", today=dt.date(2026, 11, 14))}
    assert rows["Groceries"]["budget"] == 300.0
    assert rows["Bills"]["upcoming"] == 0.0


def test_upcoming_recurrings_ignores_archived(conn):
    got = copilot.upcoming_recurrings(conn, dt.date(2026, 11, 14), dt.date(2026, 11, 30))
    assert [(u["name"], u["due"], u["amount"]) for u in got] == [
        ("Water", "2026-11-20", 50.0), ("Games", "2026-11-22", 43.54)]


def test_current_networth_uses_live_prices_and_skips_group_members(conn):
    got = copilot.current_networth(conn)
    # 150 checking + 10 x 110 live brokerage - 40 card; the group member is left out
    assert got == {"net_worth": 1210.0, "assets": 1250.0, "debts": 40.0, "accounts": 4}


def test_networth_history_carries_balances_forward(conn):
    rows = {r["day"]: r["net_worth"] for r in copilot.networth_history(conn, dt.date(2026, 10, 1), dt.date(2026, 11, 4))}
    assert rows["2026-10-01"] == 25.0  # the hidden account's past balance still counts
    assert rows["2026-11-02"] == 25.0 + 100.0 - 40.0
    assert rows["2026-11-04"] == 25.0 + 150.0 - 40.0


def test_doctor_checks_compares_with_widget_numbers(conn, tmp_path):
    w = tmp_path / "widgets"
    w.mkdir()
    (w / "widgets-category-category_FOOD.json").write_text(json.dumps({"id": "FOOD", "name": "Groceries",
                                                                       "spent": 90.0, "left": 410.0}))
    (w / "widgets-category-category_EAT.json").write_text(json.dumps({"id": "EAT", "name": "Restaurants",
                                                                      "spent": 1.0, "left": 199.0}))
    got = {c["category"]: c["ok"] for c in copilot.doctor_checks(conn, w, dt.date(2026, 11, 14))}
    assert got == {"Groceries": True, "Restaurants": False}
