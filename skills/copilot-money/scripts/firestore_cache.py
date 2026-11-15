"""Read the Firestore offline cache that the Copilot Money Mac app keeps on disk.

Copilot stores only transactions and daily balances in SQLite. Every name a
person sees (categories, accounts, tags, recurrings, budgets, goals) lives in
the Firestore SDK's LevelDB cache. The earlier skill scraped that cache with
`strings`, which loses structure, mixes old and current versions of a document,
and cannot tell a deleted document from a live one.

This module decodes the cache properly with the Python standard library only,
so the skill works on any Mac without `pip install` (plyvel and python-snappy
both need native libraries). It has three layers:

1. LevelDB: SSTable (`.ldb`) and write-ahead log (`.log`) readers, plus Snappy
   decompression. Entries are merged by sequence number, so the newest write
   for a key wins and a deletion hides older values, the same as LevelDB.
2. Protobuf: a schema-less wire decoder, then the two Firestore messages that
   matter (`firestore.client.MaybeDocument` and `google.firestore.v1.Value`).
3. Documents: each live remote document becomes a `Doc` with its path split
   into collection and id, and its fields turned into plain Python values.

The app writes to LevelDB while we read, and compaction deletes and renames
files. `snapshot()` copies the folder first (APFS clones make this nearly free)
so one read sees a consistent set of files.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

FIRESTORE_ROOT = (
    Path.home()
    / "Library/Containers/com.copilot.production/Data/Library/Application Support/firestore/__FIRAPP_DEFAULT"
)


def find_cache_dir(root: Path = FIRESTORE_ROOT) -> Path:
    """Return the LevelDB folder that holds the real cache.

    The folder name changed between app versions: older builds use
    `copilot-production-22904/main`, and newer builds use
    `copilot-production-22904.copilot-production-firestore/main`. Both can exist
    at the same time (for example, when an old `Copilot.app` and a new
    `Copilot 2.app` are both installed), and the stale one can have the newest
    modification time. So we pick the folder with the most data, not the most
    recent one.
    """
    candidates = [p.parent for p in root.glob("*/main/CURRENT")]
    if not candidates:
        raise FileNotFoundError(f"No Firestore LevelDB cache under {root}")

    def size(p: Path) -> int:
        return sum(f.stat().st_size for f in p.iterdir() if f.suffix in (".ldb", ".sst", ".log"))

    return max(candidates, key=size)


# --------------------------------------------------------------------------
# Snappy
# --------------------------------------------------------------------------


def _uvarint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def snappy_decompress(buf: bytes) -> bytes:
    length, pos = _uvarint(buf, 0)
    out = bytearray()
    n = len(buf)
    while pos < n:
        tag = buf[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            ln = tag >> 2
            if ln >= 60:
                extra = ln - 59
                ln = int.from_bytes(buf[pos : pos + extra], "little")
                pos += extra
            ln += 1
            out += buf[pos : pos + ln]
            pos += ln
            continue
        if kind == 1:
            ln = ((tag >> 2) & 7) + 4
            off = ((tag >> 5) << 8) | buf[pos]
            pos += 1
        elif kind == 2:
            ln = (tag >> 2) + 1
            off = int.from_bytes(buf[pos : pos + 2], "little")
            pos += 2
        else:
            ln = (tag >> 2) + 1
            off = int.from_bytes(buf[pos : pos + 4], "little")
            pos += 4
        if off == 0 or off > len(out):
            raise ValueError("bad snappy offset")
        start = len(out) - off
        if off >= ln:
            out += out[start : start + ln]
        else:  # overlapping copy repeats the pattern
            for i in range(ln):
                out.append(out[start + i])
    if len(out) != length:
        raise ValueError(f"snappy length mismatch {len(out)} != {length}")
    return bytes(out)


# --------------------------------------------------------------------------
# LevelDB
# --------------------------------------------------------------------------

TYPE_DELETE = 0
TYPE_VALUE = 1


@dataclass
class Entry:
    key: bytes
    seq: int
    kind: int
    value: bytes


def _read_block(data: bytes, offset: int, size: int) -> bytes:
    raw = data[offset : offset + size]
    ctype = data[offset + size]
    if ctype == 0:
        return raw
    if ctype == 1:
        return snappy_decompress(raw)
    raise ValueError(f"unsupported block compression {ctype}")


def _block_entries(block: bytes) -> Iterator[Tuple[bytes, bytes]]:
    num_restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * num_restarts
    pos = 0
    key = b""
    while pos < end:
        shared, pos = _uvarint(block, pos)
        non_shared, pos = _uvarint(block, pos)
        vlen, pos = _uvarint(block, pos)
        key = key[:shared] + block[pos : pos + non_shared]
        pos += non_shared
        yield key, block[pos : pos + vlen]
        pos += vlen


def read_table(data: bytes) -> Iterator[Entry]:
    """Yield every entry of one SSTable (`.ldb`)."""
    footer = data[-48:]
    if footer[-8:] != b"\x57\xfb\x80\x8b\x24\x75\x47\xdb":
        raise ValueError("not a LevelDB table")
    _, p = _uvarint(footer, 0)
    _, p = _uvarint(footer, p)
    idx_off, p = _uvarint(footer, p)
    idx_size, p = _uvarint(footer, p)
    index = _read_block(data, idx_off, idx_size)
    for _, handle in _block_entries(index):
        off, q = _uvarint(handle, 0)
        size, _ = _uvarint(handle, q)
        for ikey, value in _block_entries(_read_block(data, off, size)):
            tail = int.from_bytes(ikey[-8:], "little")
            yield Entry(ikey[:-8], tail >> 8, tail & 0xFF, value)


def read_log(data: bytes) -> Iterator[Entry]:
    """Yield every entry of a write-ahead log (`.log`).

    A truncated record at the end is normal (the app may be mid-write), so the
    reader stops there instead of failing.
    """
    block_size = 32768
    pending = bytearray()
    pos = 0
    n = len(data)
    while pos + 7 <= n:
        left = block_size - (pos % block_size)
        if left < 7:
            pos += left
            continue
        length = struct.unpack_from("<H", data, pos + 4)[0]
        rtype = data[pos + 6]
        payload = data[pos + 7 : pos + 7 + length]
        pos += 7 + length
        if rtype == 0 or len(payload) < length:
            break
        if rtype == 1:  # FULL
            yield from _write_batch(bytes(payload))
        elif rtype == 2:  # FIRST
            pending = bytearray(payload)
        elif rtype == 3:  # MIDDLE
            pending += payload
        elif rtype == 4:  # LAST
            pending += payload
            yield from _write_batch(bytes(pending))
            pending = bytearray()


def _write_batch(batch: bytes) -> Iterator[Entry]:
    seq, count = struct.unpack_from("<QI", batch, 0)
    pos = 12
    for i in range(count):
        kind = batch[pos]
        pos += 1
        klen, pos = _uvarint(batch, pos)
        key = batch[pos : pos + klen]
        pos += klen
        value = b""
        if kind == TYPE_VALUE:
            vlen, pos = _uvarint(batch, pos)
            value = batch[pos : pos + vlen]
            pos += vlen
        yield Entry(key, seq + i, kind, value)


def read_db(path: Path) -> Dict[bytes, bytes]:
    """Return the live key/value pairs of a LevelDB folder (newest write wins)."""
    latest: Dict[bytes, Entry] = {}
    files = sorted(path.glob("*.ldb")) + sorted(path.glob("*.sst")) + sorted(path.glob("*.log"))
    for f in files:
        data = f.read_bytes()
        if not data:
            continue
        reader = read_log if f.suffix == ".log" else read_table
        try:
            for e in reader(data):
                cur = latest.get(e.key)
                if cur is None or e.seq >= cur.seq:
                    latest[e.key] = e
        except (ValueError, IndexError, struct.error):
            # A damaged or half-written file must not hide the rest of the cache.
            continue
    return {k: e.value for k, e in latest.items() if e.kind == TYPE_VALUE}


def snapshot(src: Path) -> Path:
    """Copy the LevelDB folder to a temp folder so compaction cannot move files under us."""
    dst = Path(tempfile.mkdtemp(prefix="copilot-firestore-"))
    target = dst / "main"
    try:
        # `cp -c` makes APFS clones: instant and no extra disk space.
        subprocess.run(["cp", "-c", "-R", str(src), str(target)], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        shutil.copytree(src, target, ignore=shutil.ignore_patterns("LOCK"))
    return target


# --------------------------------------------------------------------------
# Protobuf
# --------------------------------------------------------------------------


def pb_fields(buf: bytes) -> List[Tuple[int, int, Any]]:
    """Decode one protobuf message into (field_number, wire_type, raw) tuples."""
    out = []
    pos = 0
    n = len(buf)
    while pos < n:
        tag, pos = _uvarint(buf, pos)
        num, wt = tag >> 3, tag & 7
        if num == 0:
            raise ValueError("field 0")
        if wt == 0:
            v, pos = _uvarint(buf, pos)
        elif wt == 1:
            v = buf[pos : pos + 8]
            pos += 8
        elif wt == 2:
            ln, pos = _uvarint(buf, pos)
            v = buf[pos : pos + ln]
            if len(v) != ln:
                raise ValueError("truncated")
            pos += ln
        elif wt == 5:
            v = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"wire type {wt}")
        out.append((num, wt, v))
    if pos != n:
        raise ValueError("trailing bytes")
    return out


def _signed64(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _timestamp(buf: bytes) -> _dt.datetime:
    secs = nanos = 0
    for num, _, v in pb_fields(buf):
        if num == 1:
            secs = _signed64(v)
        elif num == 2:
            nanos = v
    return _dt.datetime.fromtimestamp(secs + nanos / 1e9, tz=_dt.timezone.utc)


def decode_value(buf: bytes) -> Any:
    """Decode a `google.firestore.v1.Value` into a plain Python value."""
    for num, _, v in pb_fields(buf):
        if num == 11:
            return None
        if num == 1:
            return bool(v)
        if num == 2:
            return _signed64(v)
        if num == 3:
            return struct.unpack("<d", v)[0]
        if num == 10:
            return _timestamp(v)
        if num == 17:
            return v.decode("utf-8", "replace")
        if num == 18:
            return v.hex()
        if num == 5:
            return v.decode("utf-8", "replace").split("/documents/", 1)[-1]
        if num == 8:
            lat = lng = 0.0
            for n2, _, v2 in pb_fields(v):
                if n2 == 1:
                    lat = struct.unpack("<d", v2)[0]
                elif n2 == 2:
                    lng = struct.unpack("<d", v2)[0]
            return {"latitude": lat, "longitude": lng}
        if num == 9:
            return [decode_value(v2) for n2, _, v2 in pb_fields(v) if n2 == 1]
        if num == 6:
            return decode_map(v)
    return None


def _map_entry(buf: bytes) -> Tuple[str, Any]:
    key, val = "", None
    for num, _, v in pb_fields(buf):
        if num == 1:
            key = v.decode("utf-8", "replace")
        elif num == 2:
            val = decode_value(v)
    return key, val


def decode_map(buf: bytes) -> Dict[str, Any]:
    """Decode a `MapValue` (field 1 holds repeated key/value entries)."""
    return dict(_map_entry(v) for num, _, v in pb_fields(buf) if num == 1)


# --------------------------------------------------------------------------
# Firestore documents
# --------------------------------------------------------------------------

_NAME_RE = re.compile(r"^projects/[^/]+/databases/[^/]+/documents/(.+)$")


@dataclass
class Doc:
    path: str
    fields: Dict[str, Any]
    update_time: Optional[_dt.datetime] = None
    segments: List[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.segments[-1]

    @property
    def collection(self) -> str:
        """The collection name without parent ids, e.g. `categories` for users/U/categories/C."""
        return self.segments[-2]

    @property
    def parent_ids(self) -> Dict[str, str]:
        """Ids of parent documents keyed by their collection, e.g. {"users": "U"}."""
        s = self.segments[:-2]
        return {s[i]: s[i + 1] for i in range(0, len(s) - 1, 2)}


def parse_document(buf: bytes) -> Optional[Doc]:
    """Decode a `google.firestore.v1.Document`."""
    name = None
    fields: Dict[str, Any] = {}
    update_time = None
    for num, wt, v in pb_fields(buf):
        if num == 1 and wt == 2:
            name = v.decode("utf-8", "replace")
        elif num == 2 and wt == 2:
            k, val = _map_entry(v)
            fields[k] = val
        elif num == 4 and wt == 2:
            update_time = _timestamp(v)
    if not name:
        return None
    m = _NAME_RE.match(name)
    if not m:
        return None
    path = m.group(1)
    return Doc(path=path, fields=fields, update_time=update_time, segments=path.split("/"))


def parse_maybe_document(buf: bytes) -> Optional[Doc]:
    """Decode a `firestore.client.MaybeDocument` and return the live document, if any.

    Field 1 (`no_document`) marks a document the server says is gone, and field 3
    (`unknown_document`) has no data; both return None.
    """
    for num, wt, v in pb_fields(buf):
        if num == 2 and wt == 2:
            return parse_document(v)
    return None


_REMOTE_DOC_PREFIXES = (b"\x85remote_document\x00", b"\x85remote_documents\x00")


def iter_documents(kv: Dict[bytes, bytes]) -> Iterator[Doc]:
    """Yield every live document in the remote document cache.

    The key table name is encoded with Firestore's OrderedCode (`0x85` + name +
    `0x00 0x01`). Two table names exist across SDK versions, so we accept both.
    Values that fail to parse are skipped: other tables share no prefix with
    these, but an unknown future message layout should not stop the read.
    """
    for key, value in kv.items():
        if not key.startswith(_REMOTE_DOC_PREFIXES):
            continue
        try:
            doc = parse_maybe_document(value)
        except (ValueError, IndexError, struct.error):
            continue
        if doc is not None:
            yield doc


def load_documents(cache_dir: Optional[Path] = None, use_snapshot: bool = True) -> List[Doc]:
    src = cache_dir or find_cache_dir()
    path = snapshot(src) if use_snapshot else src
    try:
        return list(iter_documents(read_db(path)))
    finally:
        if use_snapshot:
            shutil.rmtree(path.parent, ignore_errors=True)


def jsonable(v: Any) -> Any:
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, list):
        return [jsonable(x) for x in v]
    return v


if __name__ == "__main__":  # quick manual probe
    import collections

    docs = load_documents()
    print(collections.Counter(d.collection for d in docs).most_common())
