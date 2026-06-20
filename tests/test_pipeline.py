#!/usr/bin/env python3
"""End-to-end pipeline test against SYNTHETIC encrypted SQLCipher databases.

There is no real WeChat data (or key extraction) involved here — this test
builds throw-away SQLCipher databases that mimic WeChat's v3 and v4 schemas,
encrypts them with a known key, then exercises the real pipeline:

    db_decryptor -> db_reader -> message_parser -> exporters

It validates the parts we can verify without a live WeChat: the SQLCipher
PRAGMA handling, schema introspection, sharded-table lookup, v4 Name2Id sender
resolution, zstd content decompression, message-type classification and all
three exporters. Run:  python tests/test_pipeline.py
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import constants, db_decryptor, db_reader, message_parser  # noqa: E402
from core.models import Contact, ExportBundle, Layout  # noqa: E402

KEY = "a" * 64  # 32-byte raw key, hex
FRIEND = "wxid_friend0001"
GROUP = "9988776655@chatroom"
MEMBER = "wxid_member0007"
OWNER_V4 = "wxid_myself0000"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _connect_encrypted(path, version):
    import sqlcipher3.dbapi2 as sq
    conn = sq.connect(path)
    cur = conn.cursor()
    cur.execute('PRAGMA key = "x\'%s\'"' % KEY)
    for k, v in constants.SQLCIPHER_PRAGMAS[version].items():
        if isinstance(v, int):
            cur.execute("PRAGMA %s = %d" % (k, v))
        else:
            cur.execute("PRAGMA %s = %s" % (k, v))
    return conn, cur


# --------------------------------------------------------------------------- #
# v4 synthetic build
# --------------------------------------------------------------------------- #
def build_v4(base):
    acct = os.path.join(base, "xwechat_files", OWNER_V4)
    msg_dir = os.path.join(acct, "db_storage", "message")
    contact_dir = os.path.join(acct, "db_storage", "contact")
    os.makedirs(msg_dir)
    os.makedirs(contact_dir)

    # --- message_0.db ---
    msg_db = os.path.join(msg_dir, "message_0.db")
    conn, cur = _connect_encrypted(msg_db, "v4")
    tbl = "Msg_" + _md5(FRIEND)
    gtbl = "Msg_" + _md5(GROUP)
    for t in (tbl, gtbl):
        cur.execute(
            'CREATE TABLE "%s" (local_id INTEGER PRIMARY KEY, server_id TEXT, '
            "local_type INTEGER, create_time INTEGER, real_sender_id INTEGER, "
            "message_content BLOB, WCDB_CT_message_content INTEGER, status INTEGER)" % t
        )
    cur.execute("CREATE TABLE Name2Id (user_name TEXT)")
    # Name2Id rowid (1-based) == real_sender_id
    cur.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (?,?)", (1, OWNER_V4))
    cur.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (?,?)", (2, FRIEND))
    cur.execute("INSERT INTO Name2Id (rowid, user_name) VALUES (?,?)", (3, MEMBER))

    import zstandard
    zc = zstandard.ZstdCompressor()
    link_xml = ('<msg><appmsg><title>一篇文章</title><type>5</type>'
                '<des>描述</des><url>https://example.com/a</url></appmsg></msg>')
    rows_1to1 = [
        (1, "s1", 1, 1_700_000_000, 2, "你好呀".encode(), 0, 0),          # recv text
        (2, "s2", 1, 1_700_000_100, 0, "在的，有事？".encode(), 0, 0),     # sent text (sender 0 => self)
        (3, "s3", 1, 1_700_000_200, 2, zc.compress("这是zstd压缩的消息".encode()), 4, 0),  # recv, zstd
        (4, "s4", 3, 1_700_000_300, 2,
         b'<msg><img md5="abc" aeskey="k" cdnurl="https://cdn/x.jpg"/></msg>', 0, 0),  # image
        (5, "s5", 34, 1_700_000_400, 2,
         b'<msg><voicemsg voicelength="2200" clientmsgid="cm99"/></msg>', 0, 0),       # voice
        (6, "s6", 49, 1_700_000_500, 0, link_xml.encode(), 0, 0),         # link (sent)
        (7, "s7", 10000, 1_700_000_600, 2, b"\xe6\x92\xa4\xe5\x9b\x9e\xe4\xba\x86\xe4\xb8\x80\xe6\x9d\xa1\xe6\xb6\x88\xe6\x81\xaf", 0, 0),  # system "撤回了一条消息"
    ]
    cur.executemany(
        'INSERT INTO "%s" VALUES (?,?,?,?,?,?,?,?)' % tbl, rows_1to1)
    # group: member sends; content carries "wxid:\n" prefix as a fallback too
    grp = [
        (1, "g1", 1, 1_700_001_000, 3, b"%s:\n\xe5\xa4\xa7\xe5\xae\xb6\xe5\xa5\xbd" % MEMBER.encode(), 0, 0),  # "大家好"
        (2, "g2", 1, 1_700_001_100, 0, b"%s:\n\xe6\x94\xb6\xe5\x88\xb0" % OWNER_V4.encode(), 0, 0),            # "收到" (self)
    ]
    cur.executemany('INSERT INTO "%s" VALUES (?,?,?,?,?,?,?,?)' % gtbl, grp)
    conn.commit()
    conn.close()

    # --- contact.db ---
    cdb = os.path.join(contact_dir, "contact.db")
    conn, cur = _connect_encrypted(cdb, "v4")
    cur.execute("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, alias TEXT)")
    cur.execute("CREATE TABLE stranger (username TEXT, nick_name TEXT, remark TEXT, alias TEXT)")
    cur.execute("INSERT INTO contact VALUES (?,?,?,?)",
                (FRIEND, "小明", "明哥", "mingge88"))
    cur.execute("INSERT INTO contact VALUES (?,?,?,?)",
                (GROUP, "技术交流群", "", ""))
    cur.execute("INSERT INTO contact VALUES (?,?,?,?)",
                (MEMBER, "小七", "", "qiqi"))
    conn.commit()
    conn.close()

    return Layout(
        version="v4", account_dir=acct, account_id=OWNER_V4,
        message_dbs=[msg_db], contact_db=cdb,
    )


# --------------------------------------------------------------------------- #
# v3 synthetic build
# --------------------------------------------------------------------------- #
def build_v3(base):
    acct = os.path.join(base, "AppSupport", constants.BUNDLE_ID, "2.0b4.0.9", _md5("acct"))
    msg_dir = os.path.join(acct, "Message")
    contact_dir = os.path.join(acct, "Contact")
    os.makedirs(msg_dir)
    os.makedirs(contact_dir)

    msg_db = os.path.join(msg_dir, "msg_0.db")
    conn, cur = _connect_encrypted(msg_db, "v3")
    tbl = "Chat_" + _md5(FRIEND)
    cur.execute(
        'CREATE TABLE "%s" (mesLocalID INTEGER PRIMARY KEY, msgCreateTime INTEGER, '
        "msgContent TEXT, messageType INTEGER, msgStatus INTEGER, mesDes INTEGER, "
        "mesSvrID TEXT)" % tbl
    )
    rows = [
        (1, 1_600_000_000, "早上好", 1, 4, 0, "v1"),   # received (mesDes 0)
        (2, 1_600_000_100, "早，吃了吗", 1, 3, 1, "v2"),  # sent (mesDes 1)
        (3, 1_600_000_200, "[这是表情]", 47, 4, 0, "v3"),
    ]
    cur.executemany('INSERT INTO "%s" VALUES (?,?,?,?,?,?,?)' % tbl, rows)
    conn.commit()
    conn.close()

    cdb = os.path.join(contact_dir, "wccontact_new2.db")
    conn, cur = _connect_encrypted(cdb, "v3")
    cur.execute("CREATE TABLE WCContact (m_nsUsrName TEXT, nickname TEXT, "
                "m_nsRemark TEXT, m_nsAliasName TEXT)")
    cur.execute("INSERT INTO WCContact VALUES (?,?,?,?)",
                (FRIEND, "小明", "明哥", "mingge88"))
    conn.commit()
    conn.close()

    return Layout(
        version="v3", account_dir=acct, account_id=os.path.basename(acct),
        message_dbs=[msg_db], contact_db=cdb,
    )


# --------------------------------------------------------------------------- #
# run + assert
# --------------------------------------------------------------------------- #
def run(layout, out_dir, owner_username):
    keys = {"*": KEY}
    decrypted = db_decryptor.decrypt_layout(layout, keys)
    assert decrypted, "decrypt_layout returned nothing"

    contacts = db_reader.read_contacts(decrypted, layout)
    assert FRIEND in contacts, "friend contact missing: %s" % list(contacts)
    assert contacts[FRIEND].display_name == "明哥", contacts[FRIEND].display_name

    index = db_reader.build_table_index([decrypted[db] for db in layout.message_dbs])
    hit = db_reader.find_chat_table(index, contacts[FRIEND], layout.version)
    assert hit, "chat table not found"

    msgs = db_reader.read_messages(hit[0], hit[1], layout, contacts[FRIEND],
                                   owner_username)
    assert msgs, "no messages read"
    for m in msgs:
        message_parser.parse(m, is_group=contacts[FRIEND].is_group)

    kinds = [m.kind for m in msgs]
    texts = [m.display_text for m in msgs]
    return decrypted, contacts, msgs, kinds, texts


def check_v4(layout):
    out = tempfile.mkdtemp()
    decrypted, contacts, msgs, kinds, texts = run(layout, out, OWNER_V4)

    # zstd message decoded
    assert "这是zstd压缩的消息" in texts, "zstd content not decoded: %s" % texts
    # classification
    assert "text" in kinds and "image" in kinds and "voice" in kinds and "link" in kinds
    # sender resolution via Name2Id: msg #2 (real_sender_id 0) is self
    sent = [m for m in msgs if m.is_sender]
    recv = [m for m in msgs if not m.is_sender]
    assert sent and recv, "sender resolution failed (sent=%d recv=%d)" % (len(sent), len(recv))
    assert any(m.display_text == "在的，有事？" and m.is_sender for m in msgs), "self text mis-tagged"
    # voice flagged
    assert any(m.needs_voice for m in msgs), "voice not flagged"

    # group: Name2Id resolves member, prefix stripped
    gcontact = contacts[GROUP]
    gidx = db_reader.build_table_index([decrypted[db] for db in layout.message_dbs])
    ghit = db_reader.find_chat_table(gidx, gcontact, layout.version)
    gmsgs = db_reader.read_messages(ghit[0], ghit[1], layout, gcontact, OWNER_V4)
    for m in gmsgs:
        message_parser.parse(m, is_group=True)
    assert any(m.sender_username == MEMBER and m.display_text == "大家好" for m in gmsgs), \
        "group sender/prefix failed: %s" % [(m.sender_username, m.display_text) for m in gmsgs]

    # exporters
    _export_all(layout, contacts[FRIEND], msgs, out, contacts)
    print("  [v4] OK — %d msgs, kinds=%s" % (len(msgs), sorted(set(kinds))))


def check_v3(layout):
    out = tempfile.mkdtemp()
    decrypted, contacts, msgs, kinds, texts = run(layout, out, "")
    assert "早上好" in texts and "早，吃了吗" in texts, texts
    # v3 direction: mesDes 1 => sent
    assert any(m.display_text == "早，吃了吗" and m.is_sender for m in msgs), "v3 sent flag"
    assert any(m.display_text == "早上好" and not m.is_sender for m in msgs), "v3 recv flag"
    assert "sticker" in kinds, kinds
    _export_all(layout, contacts[FRIEND], msgs, out, contacts)
    print("  [v3] OK — %d msgs, kinds=%s" % (len(msgs), sorted(set(kinds))))


def _export_all(layout, contact, msgs, out, contacts):
    from export import html_exporter, csv_exporter, txt_exporter
    owner = Contact(username="me", nickname="我")
    members = {c.username: c for c in contacts.values()}
    bundle = ExportBundle(contact=contact, owner=owner, messages=msgs,
                          layout=layout, members=members,
                          generated_at="2026-06-18 00:00:00")
    h = html_exporter.export_html(bundle, os.path.join(out, "chat.html"))
    c = csv_exporter.export_csv(bundle, os.path.join(out, "chat.csv"))
    t = txt_exporter.export_txt(bundle, os.path.join(out, "chat.txt"))
    for f in (h, c, t):
        assert os.path.exists(f) and os.path.getsize(f) > 0, "empty export: %s" % f
    html = open(h, encoding="utf-8").read()
    assert contact.display_name in html, "contact name missing from html"


def main():
    with tempfile.TemporaryDirectory() as base:
        print("building + testing v4 (current WeChat)...")
        check_v4(build_v4(base))
        print("building + testing v3 (legacy WeChat)...")
        check_v3(build_v3(base))
    db_decryptor.cleanup_decrypted()
    print("\nALL PIPELINE TESTS PASSED ✓")


if __name__ == "__main__":
    main()
