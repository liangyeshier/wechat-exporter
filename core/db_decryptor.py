"""Decrypt WeChat SQLCipher databases to plaintext copies.

We never touch WeChat's data except to *read* it: the originals stay untouched
and every decrypted copy is written under ``~/.wechat_exporter/decrypted/`` (see
:data:`constants.DECRYPTED_DIR`). Clean those up with :func:`cleanup_decrypted`.

Decryption is delegated to a **real** SQLCipher build via the ``sqlcipher3``
package — we drive it with the version-specific PRAGMAs from
:data:`constants.SQLCIPHER_PRAGMAS` and the file's own 16-byte salt, so SQLCipher
uses the raw page key/salt directly with no KDF surprises. We do NOT hand-roll
AES/HMAC: the per-page reserve size is ambiguous in the wild and a real build is
the only reliable decoder.

Key shapes by WeChat era:

* **v3.x / v4.0**: one master key (64 hex chars) decrypts every DB.
* **v4.1+**: per-database keys — pass a ``dict`` mapping ``salt_hex`` (or the DB
  basename) to the matching 64-hex key. See :func:`resolve_key`.
"""
from __future__ import annotations

import os
import shutil
from typing import Callable, Dict, Iterable, Optional, Union
from urllib.parse import quote

from . import constants
from .models import Layout


# Either a single master key (hex str) or a per-db map.
KeySource = Union[str, Dict[str, str]]


class DecryptError(Exception):
    """Raised when a database cannot be decrypted (wrong/expired key, etc.)."""


def _import_sqlcipher():
    """Lazy-import the real SQLCipher binding, with an actionable error."""
    try:
        import sqlcipher3  # noqa: F401
        from sqlcipher3 import dbapi2  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise DecryptError(
            "缺少 SQLCipher 解密库 sqlcipher3。请安装:\n"
            "    pip install 'sqlcipher3>=0.6.2'\n"
            "如果你的 Python 版本没有现成 wheel，可改用同名替代包:\n"
            "    pip install 'sqlcipher3-wheels>=0.5.7'"
        ) from exc
    return dbapi2


# --------------------------------------------------------------------------- #
# Salt / key helpers
# --------------------------------------------------------------------------- #
def read_salt(db_path: str) -> bytes:
    """Return the first :data:`constants.SALT_LEN` bytes of *db_path*.

    SQLCipher writes a unique per-file salt as the very first bytes of every
    encrypted database; we read it to build the raw key+salt literal.
    """
    with open(os.path.expanduser(db_path), "rb") as fh:
        salt = fh.read(constants.SALT_LEN)
    if len(salt) < constants.SALT_LEN:
        raise DecryptError(
            f"无法读取数据库盐值，文件过小或损坏: {db_path}"
        )
    return salt


def _key_literal(key_hex: str, db_path: str) -> str:
    """Build the SQLCipher raw-key literal ``x'<64hex><32hex>'``.

    *key_hex* is normally 64 hex chars (the 32-byte page key); we append the
    file's own 16-byte salt (32 hex chars) so SQLCipher skips KDF derivation and
    uses the bytes directly. If *key_hex* is 96 hex chars (key+salt, as the
    memory scanner may emit), we keep only the leading 64 (the key) and ALWAYS
    re-derive the salt from *this* file — salts are per-file, so a salt captured
    from another DB would not open this one.
    """
    key_hex = key_hex.strip().lower()
    if len(key_hex) not in (64, 96):
        raise DecryptError(
            f"密钥长度异常 (应为 64 或 96 个十六进制字符，实际 {len(key_hex)})。"
            " 请重新提取密钥。"
        )
    key32 = key_hex[:64]
    salt_hex = read_salt(db_path).hex()
    return "x'%s'" % (key32 + salt_hex)


def resolve_key(
    db_path: str,
    salt_hex: str,
    keys: KeySource,
) -> Optional[str]:
    """Resolve the 64-hex key for *db_path* from *keys*.

    *keys* may be a single hex string (v3.x / v4.0 master key) or a dict mapping
    ``salt_hex`` or the DB basename to a 64-hex key (v4.1+ per-database keys).
    Returns the matching key or ``None``.
    """
    if isinstance(keys, str):
        return keys.strip() or None

    if isinstance(keys, dict):
        basename = os.path.basename(db_path)
        salt_hex = (salt_hex or "").strip().lower()
        # Try the most specific lookups first: salt, then basename.
        for candidate in (salt_hex, basename):
            if not candidate:
                continue
            val = keys.get(candidate)
            if val:
                return val.strip()
        # Case-insensitive fallback over keys (salts may differ in case).
        lowered = {k.lower(): v for k, v in keys.items()}
        for candidate in (salt_hex, basename.lower()):
            if candidate and lowered.get(candidate):
                return lowered[candidate].strip()
        # Master-key wildcard: key_extractor emits {"*": key} for v3.x / v4.0
        # (a single master key opens every DB).
        if keys.get("*"):
            return keys["*"].strip()
    return None


# --------------------------------------------------------------------------- #
# Single-database decryption
# --------------------------------------------------------------------------- #
def _apply_pragmas(cur, version: str) -> None:
    """Apply the version-specific cipher PRAGMAs in declared order.

    String values (e.g. ``cipher_hmac_algorithm``) must be emitted as bare
    identifiers (``PRAGMA cipher_hmac_algorithm = HMAC_SHA1``), integers as
    integers.
    """
    pragmas = constants.SQLCIPHER_PRAGMAS.get(version)
    if pragmas is None:
        raise DecryptError(f"未知的数据库版本: {version!r}")
    for name, value in pragmas.items():
        if isinstance(value, str):
            cur.execute("PRAGMA %s = %s" % (name, value))
        else:
            cur.execute("PRAGMA %s = %d" % (name, int(value)))


def decrypt_database(
    src_db: str,
    key_hex: str,
    version: str,
    out_path: str,
) -> str:
    """Decrypt *src_db* into a plaintext SQLite copy at *out_path*.

    Drives a real SQLCipher build: opens the source with the raw key literal,
    applies the era's PRAGMAs, validates the key by reading ``sqlite_master``,
    then exports a fully decrypted copy via ``sqlcipher_export``. Returns
    *out_path*. The source database is only read.
    """
    dbapi2 = _import_sqlcipher()

    src_db = os.path.expanduser(src_db)
    out_path = os.path.expanduser(out_path)

    if not os.path.exists(src_db):
        raise DecryptError(f"源数据库不存在: {src_db}")

    key_literal = _key_literal(key_hex, src_db)

    # Ensure the destination dir exists and no stale copy lingers.
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(out_path):
        os.remove(out_path)

    # Open the SOURCE strictly read-only + immutable so we can NEVER write to
    # WeChat's own file or create -wal/-shm/-journal sidecars next to it. The
    # plaintext output is a separate, writable file attached below.
    src_uri = "file:" + quote(src_db) + "?mode=ro&immutable=1"
    conn = dbapi2.connect(src_uri, uri=True)
    try:
        cur = conn.cursor()
        # Raw key literal must be quoted as a string inside the PRAGMA.
        cur.execute('PRAGMA key = "%s"' % key_literal)
        _apply_pragmas(cur, version)

        # Validate the key: a wrong/expired key makes the first real read fail.
        try:
            cur.execute("SELECT count(*) FROM sqlite_master")
            cur.fetchone()
        except Exception as exc:  # sqlcipher3.dbapi2.DatabaseError et al.
            raise DecryptError(
                "无法用该密钥打开数据库（密钥可能错误或已失效）:\n"
                f"  {os.path.basename(src_db)}\n"
                "建议: 在微信『正在运行』时重新提取密钥（密钥仅存在于进程内存中），"
                "然后重试。"
            ) from exc

        # Export a fully decrypted plaintext copy.
        try:
            cur.execute("ATTACH DATABASE ? AS plaintext KEY ''", (out_path,))
            cur.execute("SELECT sqlcipher_export('plaintext')")
            cur.execute("DETACH DATABASE plaintext")
            conn.commit()
        except Exception as exc:
            raise DecryptError(
                f"导出明文副本失败: {os.path.basename(src_db)} ({exc})"
            ) from exc
    finally:
        conn.close()

    return out_path


# --------------------------------------------------------------------------- #
# Layout-wide decryption
# --------------------------------------------------------------------------- #
def _collect_sources(layout: Layout) -> Dict[str, bool]:
    """Map each existing source DB path -> is_message_db flag, in stable order."""
    sources: Dict[str, bool] = {}
    for db in layout.message_dbs:
        if db and os.path.exists(os.path.expanduser(db)):
            sources[db] = True
    for db in (layout.contact_db, layout.session_db, layout.group_db):
        if db and os.path.exists(os.path.expanduser(db)) and db not in sources:
            sources[db] = False
    return sources


def decrypt_layout(
    layout: Layout,
    keys: KeySource,
    only: Optional[Iterable[str]] = None,
    progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, str]:
    """Decrypt every relevant DB in *layout* into the decrypted output dir.

    Decrypts ``message_dbs`` + ``contact_db`` + ``session_db``/``group_db`` that
    exist. Output goes to ``<DECRYPTED_DIR>/<account_id>/`` mirroring basenames.

    :param keys: master key (str) or per-db map (dict); see :func:`resolve_key`.
    :param only: optional iterable of source paths to limit the work.
    :param progress: optional ``callable(done, total, label)`` for UI feedback.
    :returns: mapping ``{src_abs: out_abs}`` for every DB decrypted.
    :raises DecryptError: only if NONE of the message DBs could be decrypted; a
        single bad shard is collected as an error but does not abort the run.
    """
    sources = _collect_sources(layout)

    if only is not None:
        only_set = {os.path.expanduser(p) for p in only}
        sources = {
            src: is_msg
            for src, is_msg in sources.items()
            if os.path.expanduser(src) in only_set
        }

    out_dir = os.path.join(
        os.path.expanduser(constants.DECRYPTED_DIR), layout.account_id
    )
    os.makedirs(out_dir, exist_ok=True)

    result: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    message_total = sum(1 for is_msg in sources.values() if is_msg)
    message_ok = 0

    total = len(sources)
    done = 0
    for src, is_msg in sources.items():
        src_abs = os.path.expanduser(src)
        label = os.path.basename(src_abs)
        out_abs = os.path.join(out_dir, label)

        salt_hex = ""
        try:
            salt_hex = read_salt(src_abs).hex()
        except DecryptError:
            # Fall through; resolve_key/decrypt may still work via basename.
            pass

        key_hex = resolve_key(src_abs, salt_hex, keys)
        try:
            if not key_hex:
                raise DecryptError(
                    f"未找到匹配的密钥: {label}"
                    "（微信 4.1+ 使用每库独立密钥，请提供 salt/库名 -> 密钥 的映射）。"
                )
            decrypt_database(src_abs, key_hex, layout.version, out_abs)
            result[src_abs] = out_abs
            if is_msg:
                message_ok += 1
        except DecryptError as exc:
            errors[src_abs] = str(exc)

        done += 1
        if progress is not None:
            progress(done, total, label)

    # A single bad message shard is tolerated; total failure is not.
    if message_total > 0 and message_ok == 0:
        detail = "\n".join(
            "  - %s: %s" % (os.path.basename(s), m) for s, m in errors.items()
        )
        raise DecryptError(
            "所有消息数据库都解密失败，无法继续。\n" + detail
        )

    return result


# --------------------------------------------------------------------------- #
# Cleanup
# --------------------------------------------------------------------------- #
def cleanup_decrypted() -> None:
    """Remove the entire decrypted-copies directory (ignoring if missing)."""
    shutil.rmtree(os.path.expanduser(constants.DECRYPTED_DIR), ignore_errors=True)
