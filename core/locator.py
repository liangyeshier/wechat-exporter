"""Locate WeChat's data directory and detect which storage era it uses.

This is the first thing the tool runs. It returns one :class:`Layout` per
logged-in account it can find, version-resolved so every downstream module
knows exactly which paths/PRAGMAs/schema to use.

Detection strategy: prefer v4 (the current rewrite, what most users have), then
fall back to v3. We never assume a single hard-coded path — we glob for the
actual on-disk shape, because the ``<dataver>`` and ``<account>`` folder names
are install-specific.
"""
from __future__ import annotations

import glob
import os
from typing import List, Optional

from . import constants
from .models import Layout


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def container_exists() -> bool:
    """True if the WeChat sandbox container is present at all."""
    return os.path.isdir(_expand(constants.CONTAINER))


# --------------------------------------------------------------------------- #
# v4 discovery: ~/.../Documents/xwechat_files/<wxid>/db_storage/...
# --------------------------------------------------------------------------- #
def _discover_v4() -> List[Layout]:
    root = _expand(constants.V4_ROOT)
    if not os.path.isdir(root):
        return []

    layouts: List[Layout] = []
    for entry in sorted(os.listdir(root)):
        acct_dir = os.path.join(root, entry)
        db_storage = os.path.join(acct_dir, "db_storage")
        msg_dir = os.path.join(db_storage, "message")
        if not os.path.isdir(msg_dir):
            continue

        message_dbs = sorted(glob.glob(os.path.join(msg_dir, "message_*.db")))
        if not message_dbs:
            continue

        contact_db = os.path.join(acct_dir, constants.SCHEMA["v4"]["contact_db"])
        session_db = os.path.join(acct_dir, constants.SCHEMA["v4"]["session_db"])
        media_root = os.path.join(acct_dir, "msg")

        layouts.append(
            Layout(
                version="v4",
                account_dir=acct_dir,
                account_id=entry,
                message_dbs=message_dbs,
                contact_db=contact_db if os.path.exists(contact_db) else None,
                session_db=session_db if os.path.exists(session_db) else None,
                media_root=media_root if os.path.isdir(media_root) else None,
            )
        )
    return layouts


# --------------------------------------------------------------------------- #
# v3 discovery: ~/.../Application Support/<bundle>/<dataver>/<hash>/Message/...
# --------------------------------------------------------------------------- #
def _discover_v3() -> List[Layout]:
    root = _expand(constants.V3_ROOT)
    if not os.path.isdir(root):
        return []

    layouts: List[Layout] = []
    # <dataver>/<account-hash>/Message/msg_*.db  — glob two levels deep.
    pattern = os.path.join(root, "*", "*", "Message", "msg_*.db")
    by_account = {}
    for db in glob.glob(pattern):
        acct_dir = os.path.dirname(os.path.dirname(db))  # the <hash> folder
        by_account.setdefault(acct_dir, []).append(db)

    for acct_dir, dbs in sorted(by_account.items()):
        contact_db = os.path.join(
            acct_dir, constants.SCHEMA["v3"]["contact_subdir"],
            constants.SCHEMA["v3"]["contact_db"],
        )
        # Some builds keep wccontact at the account root rather than Contact/.
        if not os.path.exists(contact_db):
            alt = os.path.join(acct_dir, constants.SCHEMA["v3"]["contact_db"])
            contact_db = alt if os.path.exists(alt) else None

        group_db = os.path.join(acct_dir, constants.SCHEMA["v3"]["group_db"])
        media_root = acct_dir  # 3.x scatters media under the account dir

        layouts.append(
            Layout(
                version="v3",
                account_dir=acct_dir,
                account_id=os.path.basename(acct_dir),
                message_dbs=sorted(dbs),
                contact_db=contact_db,
                group_db=group_db if os.path.exists(group_db) else None,
                media_root=media_root,
            )
        )
    return layouts


def discover_accounts() -> List[Layout]:
    """Return every WeChat account layout found on this machine (v4 first)."""
    return _discover_v4() + _discover_v3()


def select_account(preferred_id: Optional[str] = None) -> Layout:
    """Pick a single account, raising a helpful error if none/ambiguous.

    :param preferred_id: substring match against account_id to disambiguate.
    """
    accounts = discover_accounts()
    if not accounts:
        if not container_exists():
            raise FileNotFoundError(
                "找不到微信数据目录。\n"
                f"  期望容器路径: {_expand(constants.CONTAINER)}\n"
                "  请确认: (1) 这台 Mac 安装并登录过微信; "
                "(2) 终端已获得『完全磁盘访问权限』(系统设置 › 隐私与安全性 › 完全磁盘访问)。"
            )
        raise FileNotFoundError(
            "微信容器存在，但未找到消息数据库。可能是尚未登录，或微信版本的目录结构有变化。\n"
            f"  v4 路径: {_expand(constants.V4_ROOT)}\n"
            f"  v3 路径: {_expand(constants.V3_ROOT)}"
        )

    if preferred_id:
        matches = [a for a in accounts if preferred_id in a.account_id]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"没有账号 id 匹配 {preferred_id!r}。可用: "
                             + ", ".join(a.account_id for a in accounts))
        # multiple matches -> fall through to ambiguity handling below
        accounts = matches

    if len(accounts) == 1:
        return accounts[0]

    listing = "\n".join(
        f"  - {a.account_id}  ({a.version}, {len(a.message_dbs)} 个消息库)"
        for a in accounts
    )
    raise ValueError(
        "发现多个微信账号，请用 --account <id 片段> 指定其中一个:\n" + listing
    )
