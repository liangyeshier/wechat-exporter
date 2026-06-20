"""Shared data model. This is the contract every module agrees on.

Read order:  locator -> Layout ; decryptor -> plaintext DB ; reader -> Contact /
Message ; message_parser enriches each Message in place ; exporters consume them.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import constants


# --------------------------------------------------------------------------- #
# Where WeChat's data lives, resolved for one logged-in account.
# --------------------------------------------------------------------------- #
@dataclass
class Layout:
    """A concrete, version-resolved view of one account's data directory."""

    version: str                       # "v3" or "v4"
    account_dir: str                   # absolute path to the per-account folder
    account_id: str                    # wxid (v4) or 32-hex hash (v3)
    message_dbs: List[str] = field(default_factory=list)   # abs paths, sharded
    contact_db: Optional[str] = None
    session_db: Optional[str] = None
    group_db: Optional[str] = None
    media_root: Optional[str] = None   # base dir for attachments (images/voice/video)

    @property
    def is_v4(self) -> bool:
        return self.version == "v4"

    @property
    def pragmas(self) -> Dict[str, object]:
        return constants.SQLCIPHER_PRAGMAS[self.version]


# --------------------------------------------------------------------------- #
# Contacts
# --------------------------------------------------------------------------- #
@dataclass
class Contact:
    username: str                      # raw id: wxid_xxx / gh_xxx / xxx@chatroom
    nickname: str = ""
    remark: str = ""                   # 备注 / user-set alias
    alias: str = ""                    # 微信号 (human-typeable id)
    avatar_path: Optional[str] = None
    message_count: int = 0

    @property
    def is_group(self) -> bool:
        return constants.group_marker(self.username)

    @property
    def is_official(self) -> bool:
        return constants.is_official(self.username)

    @property
    def display_name(self) -> str:
        return self.remark or self.nickname or self.alias or self.username

    def chat_table(self, version: str) -> str:
        """The per-conversation table name for this contact in the given era."""
        prefix = constants.SCHEMA[version]["chat_table_prefix"]
        md5 = hashlib.md5(self.username.encode("utf-8")).hexdigest()
        return f"{prefix}{md5}"

    def search_blob(self) -> str:
        return " ".join([self.username, self.nickname, self.remark, self.alias]).lower()


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
@dataclass
class Message:
    # ---- raw, straight from the decrypted DB -------------------------------
    local_id: int
    timestamp: int                     # unix seconds
    msg_type: int                      # base type (low 32 bits already masked)
    raw_content: str = ""              # decompressed text or XML body
    is_sender: bool = False            # True => sent by the account owner ("me")
    server_id: str = ""
    sender_username: str = ""          # actual sender wxid (group chats)
    status: int = 0
    sub_type: int = 0                  # appmsg <type> or high-32-bit subtype

    # ---- enriched by message_parser ---------------------------------------
    kind: str = "unknown"              # text/image/voice/video/link/file/...
    display_text: str = ""             # human-readable rendering
    media_src: Optional[str] = None    # source file on disk (.dat/.silk/.mp4)
    media_out: Optional[str] = None    # path inside the export folder (relative)
    extra: Dict[str, Any] = field(default_factory=dict)

    # ---- voice -------------------------------------------------------------
    needs_voice: bool = False          # True for type-34 awaiting transcription
    voice_text: Optional[str] = None   # filled by voice_converter

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).astimezone()

    @property
    def date_key(self) -> str:
        return self.dt.strftime("%Y-%m-%d")

    @property
    def time_str(self) -> str:
        return self.dt.strftime("%H:%M:%S")

    @property
    def kind_label(self) -> str:
        return constants.KIND_LABELS.get(self.kind, self.kind)


# --------------------------------------------------------------------------- #
# Everything an exporter needs to render a conversation.
# --------------------------------------------------------------------------- #
@dataclass
class ExportBundle:
    contact: Contact                   # the conversation partner / group
    owner: Contact                     # the account owner ("me")
    messages: List[Message]
    layout: Layout
    members: Dict[str, Contact] = field(default_factory=dict)  # wxid -> Contact (groups)
    avatars: Dict[str, str] = field(default_factory=dict)      # username -> avatar src path
    start_date: Optional[str] = None   # "YYYY-MM-DD" filter, inclusive
    end_date: Optional[str] = None
    generated_at: Optional[str] = None # stamped by the caller (no clock in pure code)

    def sender_key(self, msg: Message) -> str:
        """The username that identifies a message's sender (for avatar lookup)."""
        if msg.is_sender:
            return self.owner.username
        if self.contact.is_group and msg.sender_username:
            return msg.sender_username
        return self.contact.username

    def sender_name(self, msg: Message) -> str:
        """Display name to show next to a message bubble."""
        if msg.is_sender:
            return self.owner.display_name or "我"
        if self.contact.is_group and msg.sender_username:
            m = self.members.get(msg.sender_username)
            if m:
                return m.display_name
            return msg.sender_username
        return self.contact.display_name

    def avatar_src(self, msg: Message) -> Optional[str]:
        """Source path of the sender's avatar image, if resolved."""
        return self.avatars.get(self.sender_key(msg))
