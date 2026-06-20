"""Resolve and decode WeChat 4.x media: avatars, voice audio, images.

These live in *auxiliary* SQLCipher databases next to the message DBs (decrypted
on demand with the same per-DB keys), or as encrypted ``.dat`` files on disk:

* **avatars**  ``db_storage/head_image/head_image.db`` -> table ``head_image``
  ``(username, md5, image_buffer)`` — ``image_buffer`` is a plain JPEG blob.
* **voice**    ``db_storage/message/media_*.db`` -> table ``VoiceInfo``
  ``(chat_name_id, local_id, svr_id, voice_data)`` — ``voice_data`` is a Tencent
  SILK blob (``\\x02#!SILK_V3``). Matched to a message by ``svr_id`` (the message
  server id), falling back to ``(chat_name_id, local_id)``.
* **images**   encrypted ``.dat`` files under ``msg/attach/<md5(chat)>/<YYYY-MM>/Img``
  in WeChat's V2 format (see :mod:`core.image_dat`).

Everything is read-only; decrypted aux copies live in a temp dir cleaned up by
:meth:`MediaResolver.close`.
"""
from __future__ import annotations

import glob
import hashlib
import io
import os
import sqlite3
import tempfile
from typing import Dict, List, Optional

from . import constants, db_decryptor
from .models import Layout, Message


class MediaResolver:
    """Lazily decrypts the auxiliary media DBs and serves avatars / voice / images."""

    def __init__(self, layout: Layout, keys) -> None:
        self.layout = layout
        self.keys = keys
        self._tmp = tempfile.mkdtemp(prefix="wechat_media_")
        # decrypted aux DB connections (opened on first use)
        self._avatar_conn: Optional[sqlite3.Connection] = None
        self._avatar_ready = False
        self._voice_conns: Optional[List[sqlite3.Connection]] = None
        self._image_key_tried = False
        self._image_key: Optional[bytes] = None

    # ------------------------------------------------------------------ #
    # decryption / connection helpers
    # ------------------------------------------------------------------ #
    def _decrypt_aux(self, src: str) -> Optional[str]:
        """Decrypt one auxiliary DB into the temp dir; return the plaintext path."""
        src = os.path.expanduser(src)
        if not os.path.exists(src):
            return None
        try:
            salt = db_decryptor.read_salt(src).hex()
        except db_decryptor.DecryptError:
            return None
        key = db_decryptor.resolve_key(src, salt, self.keys)
        if not key:
            return None
        # unique output name (different dirs may share a basename)
        tag = hashlib.md5(src.encode()).hexdigest()[:8]
        out = os.path.join(self._tmp, tag + "_" + os.path.basename(src))
        try:
            return db_decryptor.decrypt_database(src, key, self.layout.version, out)
        except db_decryptor.DecryptError:
            return None

    @staticmethod
    def _ro(path: str) -> sqlite3.Connection:
        from urllib.parse import quote
        return sqlite3.connect("file:" + quote(path) + "?mode=ro", uri=True)

    # ------------------------------------------------------------------ #
    # avatars
    # ------------------------------------------------------------------ #
    def _ensure_avatars(self) -> None:
        if self._avatar_ready:
            return
        self._avatar_ready = True
        if not self.layout.is_v4:
            return
        src = os.path.join(self.layout.account_dir, "db_storage",
                           "head_image", "head_image.db")
        plain = self._decrypt_aux(src)
        if plain:
            try:
                self._avatar_conn = self._ro(plain)
            except sqlite3.Error:
                self._avatar_conn = None

    def avatar(self, username: str) -> Optional[bytes]:
        """Return the JPEG bytes of *username*'s avatar, or None."""
        if not username:
            return None
        self._ensure_avatars()
        if self._avatar_conn is None:
            return None
        try:
            row = self._avatar_conn.execute(
                "SELECT image_buffer FROM head_image WHERE username=?", (username,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if row and row[0]:
            return bytes(row[0])
        return None

    # ------------------------------------------------------------------ #
    # voice
    # ------------------------------------------------------------------ #
    def _ensure_voice(self) -> None:
        if self._voice_conns is not None:
            return
        self._voice_conns = []
        if not self.layout.is_v4:
            return
        msg_dir = os.path.join(self.layout.account_dir, "db_storage", "message")
        for src in sorted(glob.glob(os.path.join(msg_dir, "media_*.db"))):
            plain = self._decrypt_aux(src)
            if not plain:
                continue
            try:
                self._voice_conns.append(self._ro(plain))
            except sqlite3.Error:
                pass

    def voice_silk(self, msg: Message, chat_username: str) -> Optional[bytes]:
        """Return the raw SILK bytes for a voice *msg*, or None.

        Matches by the message server id first (most reliable), then by
        ``(chat_name_id, local_id)`` where ``chat_name_id`` is resolved from the
        media DB's own ``Name2Id`` table via *chat_username*.
        """
        self._ensure_voice()
        if not self._voice_conns:
            return None
        try:
            svr = int(msg.server_id) if msg.server_id not in (None, "", "0") else 0
        except (TypeError, ValueError):
            svr = 0
        for conn in self._voice_conns:
            try:
                if svr:
                    row = conn.execute(
                        "SELECT voice_data FROM VoiceInfo WHERE svr_id=?", (svr,)
                    ).fetchone()
                    if row and row[0]:
                        return bytes(row[0])
                # fallback: chat_name_id (from this DB's Name2Id) + local_id
                cnid = conn.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name=?", (chat_username,)
                ).fetchone()
                if cnid:
                    row = conn.execute(
                        "SELECT voice_data FROM VoiceInfo WHERE chat_name_id=? AND local_id=?",
                        (cnid[0], msg.local_id),
                    ).fetchone()
                    if row and row[0]:
                        return bytes(row[0])
            except sqlite3.Error:
                continue
        return None

    def voice_wav(self, msg: Message, chat_username: str) -> Optional[bytes]:
        """Decode a voice message's SILK to a 24 kHz mono WAV (bytes), or None."""
        silk = self.voice_silk(msg, chat_username)
        if not silk:
            return None
        return silk_to_wav(silk)

    # ------------------------------------------------------------------ #
    # images  (V2 .dat decode; needs the memory-extracted image key)
    # ------------------------------------------------------------------ #
    def prepare_images(self, image_messages, chat_username: str) -> int:
        """Load/derive the image key+xor and build the message->.dat map.

        The macOS V2 key is DERIVED offline from uin+wxid (no memory scan / sudo);
        we fall back to a cached key if present. Returns the number of image
        messages mapped to a file (0 if no key available)."""
        from . import image_dat, key_extractor
        # Derive the key for THIS account (correct when several WeChat accounts
        # coexist — each has its own uin/wxid → its own image key). Only fall
        # back to a cached key if derivation fails. Do NOT write a global cache
        # (it would be wrong for a different account).
        k, xb = image_dat.derive_image_key(self.layout)
        if not k:
            k = key_extractor.load_image_key()
            if xb is None:
                xb = key_extractor.load_image_xor()
        self._image_key = k
        self._image_xor = xb
        if self._image_xor is None:
            self._image_xor = image_dat.recover_xor_byte(self.layout, chat_username)
        if not self._image_key:
            self._image_map = {}
            return 0
        self._image_map = image_dat.map_images(self.layout, image_messages, chat_username)
        return len(self._image_map)

    def image(self, msg: Message):
        """Return (displayable_bytes, ext) for a prepared image *msg*, or None.
        WeChat's wxgf (HEVC) output is converted to JPEG."""
        from . import image_dat
        dat = getattr(self, "_image_map", {}).get(msg.local_id)
        if not dat or not getattr(self, "_image_key", None):
            return None
        try:
            with open(dat, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        return image_dat.decode_to_displayable(data, self._image_key, self._image_xor)

    # ------------------------------------------------------------------ #
    # video  (plaintext .mp4 + thumbnail, mapped via message_resource.db)
    # ------------------------------------------------------------------ #
    def _ensure_resource(self) -> None:
        if hasattr(self, "_resource_conn"):
            return
        self._resource_conn = None
        if not self.layout.is_v4:
            return
        src = os.path.join(self.layout.account_dir, "db_storage", "message",
                           "message_resource.db")
        plain = self._decrypt_aux(src)
        if plain:
            try:
                self._resource_conn = self._ro(plain)
            except sqlite3.Error:
                self._resource_conn = None

    def _resource_md5(self, local_id: int, local_type: int):
        """The plaintext-file md5 for a message, from MessageResourceInfo.packed_info."""
        self._ensure_resource()
        if self._resource_conn is None:
            return None
        import re
        try:
            row = self._resource_conn.execute(
                "SELECT packed_info FROM MessageResourceInfo "
                "WHERE message_local_id=? AND message_local_type=? AND length(packed_info)>0",
                (local_id, local_type),
            ).fetchone()
        except sqlite3.Error:
            return None
        if not row or not row[0]:
            return None
        m = re.search(rb"[0-9a-f]{32}", bytes(row[0]))
        return m.group(0).decode() if m else None

    def video(self, msg: Message):
        """Return ``(mp4_path, thumbnail_path|None)`` for a video msg, or (None, None).

        Videos are stored UNENCRYPTED as ``msg/video/<YYYY-MM>/<md5>.mp4`` with a
        ``<md5>.jpg`` thumbnail; the md5 comes from message_resource.db.
        """
        if not self.layout.media_root:
            return None, None
        md5 = self._resource_md5(msg.local_id, 43)
        if not md5:
            return None, None
        vroot = os.path.join(self.layout.media_root, "video")
        mp4 = next(iter(glob.glob(os.path.join(vroot, "*", md5 + ".mp4"))), None)
        thumb = (next(iter(glob.glob(os.path.join(vroot, "*", md5 + ".jpg"))), None)
                 or next(iter(glob.glob(os.path.join(vroot, "*", md5 + "_*.jpg"))), None))
        return mp4, thumb

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        for conn in ([self._avatar_conn, getattr(self, "_resource_conn", None)]
                     + (self._voice_conns or [])):
            try:
                if conn:
                    conn.close()
            except Exception:
                pass
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# SILK -> WAV (shared with voice_converter's pipeline, but produces a real WAV
# file so the HTML can embed a playable <audio>).
# --------------------------------------------------------------------------- #
def silk_to_wav(silk: bytes, rate: int = 24000) -> Optional[bytes]:
    """Decode Tencent SILK bytes to a mono 16-bit PCM WAV (bytes)."""
    if not silk:
        return None
    try:
        pysilk = None
        for name in ("pysilk", "silk"):
            try:
                pysilk = __import__(name)
                break
            except ImportError:
                continue
        if pysilk is None:
            return None
    except Exception:
        return None
    data = silk[1:] if silk[:1] == b"\x02" else silk
    if not data.startswith(b"#!SILK_V3"):
        return None
    try:
        out = io.BytesIO()
        pysilk.decode(io.BytesIO(data), out, rate)
        pcm = out.getvalue()
    except Exception:
        return None
    if not pcm:
        return None
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return buf.getvalue()
