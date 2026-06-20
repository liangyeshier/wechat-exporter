"""Read contacts and messages from the DECRYPTED (plaintext) databases.

Operates only on the plaintext copies produced by :mod:`core.db_decryptor`, so
it uses the stdlib ``sqlite3`` (no SQLCipher needed here) and opens everything
read-only.

Two schemas are handled (see :data:`core.constants.SCHEMA`):

* **v3**: one ``Chat_<md5(username)>`` table per conversation, sharded across
  ``msg_N.db`` with no global index — we build a table→file index by scanning
  ``sqlite_master`` in every shard. Columns are CamelCase-ish and drift between
  builds, so we introspect ``PRAGMA table_info`` and map by candidate names.
* **v4**: ``Msg_<md5(username)>`` tables, snake_case columns, sender resolved via
  the per-DB ``Name2Id`` table (rowid == ``real_sender_id``), and
  ``message_content`` is zstd-compressed when ``WCDB_CT_message_content == 4``.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from . import constants
from .models import Contact, Layout, Message


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #
def _connect_ro(path: str) -> sqlite3.Connection:
    """Open a plaintext DB strictly read-only."""
    # Proper URI quoting so '#', '%', spaces etc. in the path can't truncate or
    # mis-decode the filename (a bare '#' would otherwise be read as a fragment).
    uri = "file:" + quote(os.path.abspath(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.text_factory = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else b
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    cur = conn.execute('PRAGMA table_info("%s")' % table.replace('"', ""))
    return [row[1] for row in cur.fetchall()]


def _pick(actual: List[str], candidates: List[str]) -> Optional[str]:
    """Return the first candidate present in ``actual`` (case-insensitive)."""
    lower = {c.lower(): c for c in actual}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    return None


def _col_map(actual: List[str], spec: Dict[str, List[str]]) -> Dict[str, Optional[str]]:
    return {logical: _pick(actual, cands) for logical, cands in spec.items()}


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
def read_contacts(decrypted: Dict[str, str], layout: Layout) -> Dict[str, Contact]:
    """Build ``username -> Contact`` from the decrypted contact DB."""
    contacts: Dict[str, Contact] = {}
    if not layout.contact_db:
        return contacts
    plain = decrypted.get(layout.contact_db) or decrypted.get(os.path.abspath(layout.contact_db))
    if not plain or not os.path.exists(plain):
        return contacts

    schema = constants.SCHEMA[layout.version]
    tables = (schema["contact_tables"] if layout.is_v4 else [schema["contact_table"]])
    conn = _connect_ro(plain)
    try:
        for table in tables:
            try:
                cols = _table_columns(conn, table)
            except sqlite3.Error:
                continue
            if not cols:
                continue
            cm = _col_map(cols, schema["contact_cols"])
            if not cm.get("username"):
                continue
            select_cols = [cm[k] for k in ("username", "nickname", "remark", "alias")]
            sel = ", ".join('"%s"' % c if c else "NULL" for c in select_cols)
            try:
                rows = conn.execute('SELECT %s FROM "%s"' % (sel, table)).fetchall()
            except sqlite3.Error:
                continue
            for username, nickname, remark, alias in rows:
                if not username:
                    continue
                contacts.setdefault(username, Contact(
                    username=username,
                    nickname=nickname or "",
                    remark=remark or "",
                    alias=alias or "",
                ))
    finally:
        conn.close()
    return contacts


# --------------------------------------------------------------------------- #
# Chat-table discovery across shards
# --------------------------------------------------------------------------- #
def build_table_index(decrypted_msg_dbs: List[str]) -> Dict[str, str]:
    """Map lowercased message-table name -> decrypted DB path (across all shards)."""
    index: Dict[str, str] = {}
    for db in decrypted_msg_dbs:
        if not os.path.exists(db):
            continue
        try:
            conn = _connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for (name,) in rows:
            low = name.lower()
            if low.startswith("chat_") or low.startswith("msg_"):
                # 'Chat_'/'Msg_' tables only (skip ChatExt2_/ChatCrypt_/etc.)
                if low.startswith("chatext") or low.startswith("chatcrypt"):
                    continue
                index.setdefault(low, db)
    return index


def find_chat_table(index: Dict[str, str], contact: Contact, version: str
                    ) -> Optional[Tuple[str, str]]:
    """Return ``(db_path, real_table_name)`` for a contact, or None if not found."""
    target = contact.chat_table(version).lower()
    db = index.get(target)
    if not db:
        return None
    # recover the real (original-case) table name
    conn = _connect_ro(db)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND lower(name)=?",
            (target,),
        ).fetchone()
    finally:
        conn.close()
    return (db, row[0]) if row else (db, contact.chat_table(version))


# --------------------------------------------------------------------------- #
# Name2Id (v4 sender resolution)
# --------------------------------------------------------------------------- #
def resolve_owner_wxid(layout: Layout, decrypted: Dict[str, str]) -> str:
    """Best-effort: the account owner's real wxid (needed for group direction).

    The v4 account FOLDER is ``<wxid>_<suffix>`` (e.g. ``wxid_abc_759c``), so the
    folder name is NOT the real wxid. We try the folder id and the folder id
    minus a trailing ``_<suffix>``, and return whichever actually appears as a
    ``Name2Id`` user_name. (1:1 chats don't need this — see ``read_messages``.)
    """
    if not layout.is_v4:
        return ""
    aid = layout.account_id
    candidates: List[str] = []
    if "_" in aid:
        candidates.append(aid.rsplit("_", 1)[0])  # strip the folder suffix
    candidates.append(aid)
    for db in layout.message_dbs:
        plain = decrypted.get(db) or decrypted.get(os.path.abspath(db))
        if not plain or not os.path.exists(plain):
            continue
        try:
            conn = _connect_ro(plain)
        except sqlite3.Error:
            continue
        try:
            for cand in candidates:
                row = conn.execute(
                    "SELECT 1 FROM Name2Id WHERE user_name=? LIMIT 1", (cand,)
                ).fetchone()
                if row:
                    return cand
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return candidates[0]  # best guess (suffix-stripped form)


def _load_name2id(conn: sqlite3.Connection) -> Dict[int, str]:
    try:
        rows = conn.execute("SELECT rowid, user_name FROM Name2Id").fetchall()
    except sqlite3.Error:
        return {}
    return {int(rid): (uname or "") for rid, uname in rows}


# --------------------------------------------------------------------------- #
# zstd
# --------------------------------------------------------------------------- #
def _maybe_decompress(value, ct_flag) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        if ct_flag == constants.WCDB_CT_ZSTD or data[:4] == b"\x28\xb5\x2f\xfd":
            try:
                import zstandard
                data = zstandard.ZstdDecompressor().decompress(data)
            except Exception:
                pass
        return data.decode("utf-8", "replace")
    return "" if value is None else str(value)


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
def count_messages(db: str, table: str, start_ts: Optional[int] = None,
                   end_ts: Optional[int] = None, version: str = "v4") -> int:
    conn = _connect_ro(db)
    try:
        cols = _table_columns(conn, table)
        time_col = _pick(cols, constants.SCHEMA[version]["cols"]["create_time"])
        where, params = _time_clause(time_col, start_ts, end_ts)
        sql = 'SELECT COUNT(*) FROM "%s"%s' % (table, where)
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def _time_clause(time_col: Optional[str], start_ts, end_ts):
    if not time_col or (start_ts is None and end_ts is None):
        return "", ()
    clauses, params = [], []
    if start_ts is not None:
        clauses.append('"%s" >= ?' % time_col)
        params.append(start_ts)
    if end_ts is not None:
        clauses.append('"%s" <= ?' % time_col)
        params.append(end_ts)
    return " WHERE " + " AND ".join(clauses), tuple(params)


def read_messages(db: str, table: str, layout: Layout, contact: Contact,
                  owner_username: str, start_ts: Optional[int] = None,
                  end_ts: Optional[int] = None) -> List[Message]:
    """Read and lightly-normalize messages for one conversation (raw, unparsed)."""
    version = layout.version
    cols_spec = constants.SCHEMA[version]["cols"]
    conn = _connect_ro(db)
    out: List[Message] = []
    try:
        actual = _table_columns(conn, table)
        cm = _col_map(actual, cols_spec)
        time_col = cm.get("create_time")
        name2id = _load_name2id(conn) if version == "v4" else {}

        wanted = ["local_id", "create_time", "type", "content", "status", "server_id"]
        if version == "v4":
            wanted += ["sender_id", "content_ct"]
        else:
            wanted += ["des"]
        present = [(k, cm.get(k)) for k in wanted]
        sel = ", ".join('"%s"' % c if c else "NULL" for _, c in present)

        where, params = _time_clause(time_col, start_ts, end_ts)
        order = ' ORDER BY "%s" ASC' % time_col if time_col else ""
        sql = "SELECT %s FROM \"%s\"%s%s" % (sel, table, where, order)
        cur = conn.execute(sql, params)

        idx = {k: i for i, (k, _) in enumerate(present)}
        is_group = contact.is_group
        for row in cur:
            def g(key):
                j = idx.get(key)
                return row[j] if j is not None else None

            content = _maybe_decompress(g("content"), g("content_ct"))
            try:
                mtype = int(g("type") or 0)
            except (TypeError, ValueError):
                mtype = 0
            msg = Message(
                local_id=int(g("local_id") or 0),
                timestamp=int(g("create_time") or 0),
                msg_type=mtype,
                raw_content=content,
                server_id=str(g("server_id") or ""),
                status=int(g("status") or 0) if g("status") is not None else 0,
            )

            if version == "v4":
                sid = g("sender_id")
                sid_int = None
                if sid not in (None, ""):
                    try:
                        sid_int = int(sid)
                    except (TypeError, ValueError):
                        sid_int = None
                sender = name2id.get(sid_int) if sid_int is not None else ""
                if sender:
                    msg.sender_username = sender
                # Direction. real_sender_id is a Name2Id rowid (NOT necessarily 0
                # for self — observed values are arbitrary). For a 1:1 chat the
                # only "other" party is the contact, so anyone who is NOT the
                # contact is me. For a group we compare against the owner's wxid.
                if contact.is_group:
                    if sender:
                        msg.is_sender = (sender == owner_username)
                    else:
                        msg.is_sender = sid_int in (None, 0)
                else:
                    if sender:
                        msg.is_sender = (sender != contact.username)
                    else:
                        # unresolved sender (e.g. system rows) -> treat as not-me
                        msg.is_sender = sid_int in (None, 0)
            else:
                des = g("des")
                # v3 direction flag (Des/mesDes). Convention used here: 1 => sent
                # by me, 0 => received. Use --flip-sender in the CLI if reversed
                # for your WeChat build.
                msg.is_sender = bool(des) if des is not None else False

            if is_group and not msg.is_sender and not msg.sender_username:
                # parser will pull the wxid:\n prefix
                pass
            out.append(msg)
    finally:
        conn.close()
    return out
