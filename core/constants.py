"""Central, version-aware knowledge base for WeChat-on-macOS internals.

Everything we learned about WeChat's on-disk layout, encryption parameters and
message schema lives here so the rest of the code stays declarative. Values are
documented with their source/confidence because WeChat changes them between
releases.

Two eras matter on macOS:

* **v3.x**  ("old", <= 3.8.x): SQLCipher *3* databases under
  ``Application Support/com.tencent.xinWeChat/<dataver>/<account-hash>/`` with a
  separate ``Chat_<md5>`` table per conversation, sharded across ``msg_N.db``.
* **v4.x**  ("new", 4.0+, the 2024/2025 rewrite — what most users have today):
  SQLCipher *4* databases under ``Documents/xwechat_files/<wxid>/db_storage/``
  with ``Msg_<md5>`` tables, snake_case columns and zstd-compressed content.

The macOS container bundle id is ``com.tencent.xinWeChat`` for BOTH eras
(the spec's ``com.tencent.xinMac`` is wrong and does not exist).
"""
from __future__ import annotations

import os
from typing import Dict

# --------------------------------------------------------------------------- #
# Container / filesystem roots
# --------------------------------------------------------------------------- #
BUNDLE_ID = "com.tencent.xinWeChat"


def real_home() -> str:
    """The invoking user's home directory, resilient to ``sudo``.

    Key extraction needs root (``sudo``), but under sudo ``$HOME`` /
    ``expanduser('~')`` point at ``/var/root`` — while the WeChat data AND our
    own cache live in the *real* user's home. We honor ``SUDO_USER`` so every
    path resolves to the same place whether or not the tool runs as root; this
    is the difference between finding your account and silently finding nothing.
    """
    sudo_user = os.environ.get("SUDO_USER")
    try:
        if sudo_user and hasattr(os, "geteuid") and os.geteuid() == 0:
            import pwd
            return pwd.getpwnam(sudo_user).pw_dir
    except (KeyError, ImportError, OSError):
        pass
    return os.path.expanduser("~")


#: Resolved once at import (env is already set by the time we run).
HOME = real_home()

#: Sandbox container root.
CONTAINER = os.path.join(HOME, "Library", "Containers", BUNDLE_ID, "Data")

#: WeChat 3.x stores DBs under Application Support/<bundle>/<dataver>/<hash>/
V3_ROOT = os.path.join(
    CONTAINER, "Library", "Application Support", BUNDLE_ID
)
#: WeChat 4.x stores everything under Documents/xwechat_files/<wxid>/
V4_ROOT = os.path.join(CONTAINER, "Documents", "xwechat_files")


# --------------------------------------------------------------------------- #
# Application paths used by the tool itself (resolved against the real home).
# --------------------------------------------------------------------------- #
APP_DIR = os.path.join(HOME, ".wechat_exporter")
KEY_CACHE = os.path.join(APP_DIR, "keys.json")          # map: salt/db -> key
DECRYPTED_DIR = os.path.join(APP_DIR, "decrypted")      # plaintext DB copies
VOICE_CACHE_DIR = os.path.join(APP_DIR, "voice_cache")  # transcription cache


# --------------------------------------------------------------------------- #
# SQLCipher parameters (drive a *real* SQLCipher build with these PRAGMAs;
# do NOT hand-roll AES/HMAC — the per-page reserve size is ambiguous in the wild).
# Key MUST be supplied as a raw key:  PRAGMA key = "x'<64-hex-key><32-hex-salt>'"
# Supplying the 96-hex form (key + the file's own 16-byte salt) makes SQLCipher
# skip KDF derivation and use the bytes directly.
# --------------------------------------------------------------------------- #
SQLCIPHER_PRAGMAS: Dict[str, Dict[str, object]] = {
    "v3": {
        "cipher_compatibility": 3,
        "cipher_page_size": 1024,
        "kdf_iter": 64000,
        "cipher_hmac_algorithm": "HMAC_SHA1",
        "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA1",
    },
    "v4": {
        "cipher_compatibility": 4,
        "cipher_page_size": 4096,
        "kdf_iter": 256000,
        "cipher_hmac_algorithm": "HMAC_SHA512",
        "cipher_kdf_algorithm": "PBKDF2_HMAC_SHA512",
    },
}

#: SQLCipher writes a unique 16-byte salt as the first bytes of every DB file.
SALT_LEN = 16
#: Raw 256-bit page key.
KEY_LEN = 32


# --------------------------------------------------------------------------- #
# In-memory key signature (used by the memory-scan key extractor).
# WCDB caches the key as the ASCII SQLite blob literal  x'<64hex><32hex>'  =
# 2 + 96 + 1 = 99 bytes.  The 64 hex chars are the 32-byte key, the trailing 32
# hex chars are the per-file 16-byte salt.
# --------------------------------------------------------------------------- #
KEY_LITERAL_RE = rb"x'[0-9a-fA-F]{96}'"        # full key+salt literal
KEY_RAW_HEX_RE = rb"[0-9a-fA-F]{64}"           # bare key (fallback heuristic)


# --------------------------------------------------------------------------- #
# Message types.  WeChat packs a *base* type in the low 32 bits and an optional
# app sub-type in the high 32 bits, so always mask:  base = t & 0xFFFFFFFF.
# --------------------------------------------------------------------------- #
BASE_MSG_TYPES: Dict[int, str] = {
    1: "text",
    3: "image",
    34: "voice",
    42: "card",          # 名片 / contact card
    43: "video",
    47: "sticker",       # 表情 / emoji-gif
    48: "location",
    49: "app",           # link/file/miniprogram/quote/transfer... -> parse <appmsg><type>
    50: "voip",          # 语音/视频通话
    10000: "system",
    10002: "recall",     # 撤回
}

#: For type 49 the real meaning is the ``<appmsg><type>`` value in the XML body.
APPMSG_SUBTYPES: Dict[int, str] = {
    1: "text",
    3: "music",
    5: "link",
    6: "file",
    8: "sticker",
    17: "location_share",   # realtime location
    19: "forward",          # 合并转发
    33: "miniprogram",
    36: "miniprogram",
    44: "miniprogram",
    51: "channel",          # 视频号
    57: "quote",            # refermsg / 引用
    63: "channel_live",
    2000: "transfer",       # 转账
    2001: "redpacket",      # 红包
}

#: Human-readable Chinese labels for the rendered "kind".
KIND_LABELS: Dict[str, str] = {
    "text": "文本",
    "image": "图片",
    "voice": "语音",
    "card": "名片",
    "video": "视频",
    "sticker": "表情",
    "location": "位置",
    "location_share": "实时位置",
    "link": "链接",
    "file": "文件",
    "music": "音乐",
    "miniprogram": "小程序",
    "channel": "视频号",
    "channel_live": "视频号直播",
    "quote": "引用",
    "forward": "合并转发",
    "transfer": "转账",
    "redpacket": "红包",
    "voip": "通话",
    "system": "系统消息",
    "recall": "撤回",
    "app": "应用消息",
    "unknown": "未知",
}


# --------------------------------------------------------------------------- #
# Schema descriptors per era.  Column *casing* drifts between WeChat builds, so
# db_reader treats these as PREFERRED names and falls back to PRAGMA table_info
# introspection.  Order in each list = priority (first match wins).
# --------------------------------------------------------------------------- #
SCHEMA = {
    "v3": {
        # message databases
        "msg_glob": "msg_*.db",
        "msg_subdir": "Message",
        "chat_table_prefix": "Chat_",          # Chat_<md5(username)>
        "cols": {
            "local_id": ["mesLocalID", "MesLocalID"],
            "server_id": ["mesSvrID", "MesSvrID", "mesMesSvrID"],
            "create_time": ["msgCreateTime", "CreateTime"],
            "content": ["msgContent", "Message"],
            "type": ["messageType", "Type", "messgaeType"],
            "status": ["msgStatus", "Status"],
            "des": ["mesDes", "Des"],          # 0 = received, 1 = sent (varies; verify)
            "source": ["msgSource"],
        },
        # contacts
        "contact_db": "wccontact_new2.db",
        "contact_subdir": "Contact",
        "contact_table": "WCContact",
        "contact_cols": {
            "username": ["m_nsUsrName"],
            "nickname": ["nickname"],
            "remark": ["m_nsRemark"],
            "alias": ["m_nsAliasName"],
        },
        "group_db": "group_new.db",
    },
    "v4": {
        "msg_glob": "message_*.db",
        "msg_subdir": os.path.join("db_storage", "message"),
        "chat_table_prefix": "Msg_",           # Msg_<md5(username)>
        "name2id_table": "Name2Id",            # rowid (1-based) == real_sender_id
        "cols": {
            "local_id": ["local_id"],
            "server_id": ["server_id"],
            "create_time": ["create_time"],
            "content": ["message_content"],
            "content_ct": ["WCDB_CT_message_content"],   # ==4 => zstd-compressed
            "type": ["local_type"],
            "status": ["status"],
            "sender_id": ["real_sender_id"],
            "source": ["source"],
        },
        "contact_db": os.path.join("db_storage", "contact", "contact.db"),
        "contact_tables": ["contact", "stranger"],
        "contact_cols": {
            "username": ["username"],
            "nickname": ["nick_name"],
            "remark": ["remark"],
            "alias": ["alias"],
        },
        "session_db": os.path.join("db_storage", "session", "session.db"),
        "session_table": "SessionTable",
    },
}

#: WCDB content-type flag value meaning "zstd compressed" (v4 message_content).
WCDB_CT_ZSTD = 4

#: ``WeChat must be running`` while the key is extracted (key lives in process
#: memory only). Used for user-facing messages.
WECHAT_PROCESS_NAMES = ("WeChat", "微信", "Weixin")


def group_marker(username: str) -> bool:
    """True if a username denotes a group chat (``...@chatroom``)."""
    return username.endswith("@chatroom")


def is_official(username: str) -> bool:
    """True for official accounts (公众号), prefix ``gh_``."""
    return username.startswith("gh_")
