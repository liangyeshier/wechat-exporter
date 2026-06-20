"""Recover WeChat's SQLCipher key(s) from the LIVE WeChat process (macOS).

WeChat never writes its database key to disk: it derives a raw 256-bit page key
once and keeps it in process memory. To decrypt a copy of the DB we must read
that key out of the running app while it is logged in.

PRIMARY BACKEND — pure-Python Mach VM memory scan (``extract_via_memscan``)
--------------------------------------------------------------------------
WCDB caches the key in memory as the ASCII SQLite blob literal::

    x'<64 hex = 32-byte key><32 hex = 16-byte per-file salt>'

(see :data:`core.constants.KEY_LITERAL_RE`). We attach to the process with
``task_for_pid`` (libSystem / Mach), walk its readable VM regions and regex the
bytes. WeChat 4.1+ uses a SEPARATE key per database, so a single scan can yield
several ``salt -> key`` pairs; we therefore return a MAP, never one key.

macOS permission / re-sign caveats (READ THIS if extraction fails)
------------------------------------------------------------------
* WeChat must be RUNNING and logged in (the key only exists in memory).
* macOS hardened-runtime blocks ``task_for_pid`` against the shipped, Apple-
  signed WeChat binary even as root. You must ad-hoc re-sign the app once::

      sudo codesign --force --deep --sign - /Applications/WeChat.app

  then RELAUNCH WeChat and run this tool with ``sudo``.
* On the very newest builds you may additionally need to disable SIP
  (``csrutil disable`` from Recovery) for ``task_for_pid`` to succeed.
* ``frida`` (the optional secondary backend) generally CANNOT attach to the
  hardened WeChat either, which is why the memscan backend is preferred here.

This is a personal own-data tool: use it only to read YOUR OWN WeChat history.
It is strictly read-only with respect to WeChat's files.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
from typing import Dict, List, Optional

from . import constants


class KeyExtractionError(Exception):
    """Raised when no backend could recover a usable key (with remediation)."""


# --------------------------------------------------------------------------- #
# Process discovery
# --------------------------------------------------------------------------- #
def find_wechat_pid() -> Optional[int]:
    """Return the pid of the running WeChat process, or ``None`` if not running.

    Tries an exact-name ``pgrep -x`` for each known process name
    (:data:`core.constants.WECHAT_PROCESS_NAMES`), then falls back to a
    full-command-line match on the bundle's main executable.
    """
    candidates: List[List[str]] = [
        ["pgrep", "-x", name] for name in constants.WECHAT_PROCESS_NAMES
    ]
    # Fallback: match the bundle's main Mach-O by its path on the command line.
    candidates.append(["pgrep", "-f", "WeChat.app/Contents/MacOS"])

    for argv in candidates:
        try:
            out = subprocess.run(
                argv, capture_output=True, text=True, check=False
            ).stdout
        except (OSError, ValueError):
            # pgrep missing or bad args — try the next strategy.
            continue
        for line in out.split():
            line = line.strip()
            if line.isdigit():
                return int(line)
    return None


# --------------------------------------------------------------------------- #
# Key cache (on-disk map  salt_hex -> key_hex)
# --------------------------------------------------------------------------- #
def load_cached_keys() -> Dict[str, str]:
    """Load the cached ``{salt_hex_or_"*": key_hex}`` map; ``{}`` if absent/bad."""
    path = os.path.expanduser(constants.KEY_CACHE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Coerce to str->str and drop anything that is not a string pair.
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def save_keys(keys: Dict[str, str]) -> None:
    """Persist the ``{salt_hex_or_"*": key_hex}`` map to the cache (mkdir -p)."""
    path = os.path.expanduser(constants.KEY_CACHE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(keys, fh, indent=2, sort_keys=True)


# --------------------------------------------------------------------------- #
# Manual key entry
# --------------------------------------------------------------------------- #
def keys_from_manual(value: str) -> Dict[str, str]:
    """Build a key map from a user-supplied ``value``.

    ``value`` is either:

    * a bare 64-hex master key  -> ``{"*": <key_hex>}`` (applied to every DB), or
    * a path to a ``keys.json`` holding a ``{salt_hex: key_hex}`` map.

    Raises :class:`KeyExtractionError` if the value is neither.
    """
    if not value:
        raise KeyExtractionError("未提供密钥：--key 需要一个 64 位十六进制密钥或 keys.json 路径。")

    candidate = value.strip()

    # Case 1: a bare 64-hex key.
    if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
        return {"*": candidate.lower()}

    # Case 2: a path to a JSON map of salt -> key.
    expanded = os.path.expanduser(candidate)
    if os.path.isfile(expanded):
        try:
            with open(expanded, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise KeyExtractionError(
                f"无法读取密钥文件 {expanded}：{exc}"
            ) from exc
        if not isinstance(data, dict) or not data:
            raise KeyExtractionError(
                f"密钥文件 {expanded} 格式不正确：应为 {{\"salt 十六进制\": \"key 十六进制\"}} 的 JSON 映射。"
            )
        keys = {str(k): str(v) for k, v in data.items() if isinstance(v, str)}
        if not keys:
            raise KeyExtractionError(f"密钥文件 {expanded} 中没有有效的密钥项。")
        return keys

    raise KeyExtractionError(
        "无法识别 --key 的取值。\n"
        "  期望: 一个 64 位十六进制密钥 (例如 a1b2...，共 64 字符)，\n"
        "  或者一个指向 keys.json 的路径 ({\"salt 十六进制\": \"key 十六进制\"})。\n"
        f"  收到: {value!r}"
    )


# --------------------------------------------------------------------------- #
# Mach VM types / constants for the memory-scan backend
# --------------------------------------------------------------------------- #
# Mach typedefs (all 64-bit-safe here):
_kern_return_t = ctypes.c_int            # KERN_SUCCESS == 0
_mach_port_t = ctypes.c_uint             # task_t / vm_map_t are mach ports
_vm_map_t = ctypes.c_uint
_mach_vm_address_t = ctypes.c_uint64
_mach_vm_size_t = ctypes.c_uint64
_natural_t = ctypes.c_uint
_vm_prot_t = ctypes.c_int

KERN_SUCCESS = 0
VM_PROT_READ = 0x1
VM_PROT_WRITE = 0x2

#: vm_region_submap_info_64 has 19 ``natural_t`` (uint32) fields.
_VM_REGION_SUBMAP_INFO_COUNT_64 = 19

#: Never read more than this from a single region (shared caches can be huge).
_MAX_REGION_BYTES = 256 * 1024 * 1024            # 256 MB per region
#: Overall budget so a pathological process can't make us scan forever.
_TOTAL_SCAN_BUDGET = 4 * 1024 * 1024 * 1024      # 4 GB total read

#: Length of the in-memory key literal  x'<96 hex>'  = 2 + 96 + 1 = 99 bytes.
#: Used as the overlap between read windows so a literal straddling a window
#: boundary is never split.
_KEY_LITERAL_BYTES = 99

# NOTE: we deliberately do NOT model vm_region_submap_info_64 as a ctypes
# Structure. The kernel writes count*sizeof(natural_t) = 19*4 = 76 bytes into the
# info buffer; a hand-written Structure is easy to get a few bytes too small
# (c_ulonglong alignment), which silently corrupts adjacent heap. Instead we pass
# a raw (natural_t * 19) array (exactly 76 bytes) and read only field 0,
# ``protection`` — see extract_via_memscan.


def _load_libsystem() -> ctypes.CDLL:
    """Load libSystem and declare the Mach VM symbols we need."""
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)

    # task_t mach_task_self(void)  — convenience wrapper around mach_task_self_.
    libc.mach_task_self.restype = _mach_port_t
    libc.mach_task_self.argtypes = []

    # kern_return_t task_for_pid(task_t target, int pid, task_t *out);
    libc.task_for_pid.restype = _kern_return_t
    libc.task_for_pid.argtypes = [
        _mach_port_t, ctypes.c_int, ctypes.POINTER(_mach_port_t)
    ]

    # kern_return_t mach_vm_region_recurse(vm_map_t, mach_vm_address_t*,
    #     mach_vm_size_t*, natural_t *depth, vm_region_recurse_info_t,
    #     mach_msg_type_number_t *count);
    libc.mach_vm_region_recurse.restype = _kern_return_t
    libc.mach_vm_region_recurse.argtypes = [
        _vm_map_t,
        ctypes.POINTER(_mach_vm_address_t),
        ctypes.POINTER(_mach_vm_size_t),
        ctypes.POINTER(_natural_t),
        ctypes.c_void_p,                       # vm_region_recurse_info_t (int array)
        ctypes.POINTER(ctypes.c_uint),         # mach_msg_type_number_t *count
    ]

    # kern_return_t mach_vm_read_overwrite(vm_map_t, mach_vm_address_t,
    #     mach_vm_size_t, mach_vm_address_t data, mach_vm_size_t *outsize);
    libc.mach_vm_read_overwrite.restype = _kern_return_t
    libc.mach_vm_read_overwrite.argtypes = [
        _vm_map_t,
        _mach_vm_address_t,
        _mach_vm_size_t,
        _mach_vm_address_t,                    # dest buffer address
        ctypes.POINTER(_mach_vm_size_t),
    ]
    return libc


def _task_for_pid_error(rc: int, pid: int) -> KeyExtractionError:
    """Build the actionable error raised when ``task_for_pid`` is denied."""
    return KeyExtractionError(
        f"无法读取微信进程内存 (task_for_pid 失败, pid={pid}, kern_return={rc})。\n"
        "macOS 的强化运行时默认禁止读取微信进程内存，即使用 root 也不行。请按以下步骤操作：\n"
        "  1) 对微信做一次自签名(只需一次)：\n"
        "       sudo codesign --force --deep --sign - /Applications/WeChat.app\n"
        "  2) 重新启动微信并登录(密钥只存在于运行中的进程内存里)。\n"
        "  3) 用 sudo 重新运行本工具。\n"
        "  4) 若仍失败(最新版本)，可能需要在恢复模式下关闭 SIP：csrutil disable。\n"
        "本工具仅用于导出你自己的微信记录，且对微信数据只读。"
    )


# --------------------------------------------------------------------------- #
# Backend 1: pure-Python Mach VM memory scan (PRIMARY)
# --------------------------------------------------------------------------- #
def extract_via_memscan(pid: int) -> Dict[str, str]:
    """Scan the live WeChat process memory for cached SQLCipher key literals.

    Walks every readable VM region of process ``pid`` with
    ``mach_vm_region_recurse`` / ``mach_vm_read_overwrite`` and regex-matches
    :data:`core.constants.KEY_LITERAL_RE`. Each ``x'<96 hex>'`` match yields a
    ``key_hex`` (first 64 hex = 32-byte key) and ``salt_hex`` (last 32 hex =
    16-byte per-file salt). WeChat 4.1+ uses one key per DB, so several pairs
    may be found.

    :returns: ``{salt_hex: key_hex}`` (possibly empty if nothing matched).
    :raises KeyExtractionError: if the platform is unsupported or
        ``task_for_pid`` is denied (with full remediation guidance).
    """
    import sys

    if sys.platform != "darwin":
        raise KeyExtractionError(
            "内存扫描后端仅支持 macOS（依赖 Mach VM / task_for_pid）。"
        )

    libc = _load_libsystem()

    self_task = libc.mach_task_self()
    target_task = _mach_port_t(0)
    rc = libc.task_for_pid(self_task, ctypes.c_int(pid),
                           ctypes.byref(target_task))
    if rc != KERN_SUCCESS or target_task.value == 0:
        raise _task_for_pid_error(rc, pid)

    pattern = re.compile(constants.KEY_LITERAL_RE)
    found: Dict[str, str] = {}

    address = _mach_vm_address_t(0)
    size = _mach_vm_size_t(0)
    depth = _natural_t(0)
    total_read = 0

    while total_read < _TOTAL_SCAN_BUDGET:
        # Raw 19*uint32 = 76-byte buffer (matches what the kernel writes for
        # count=19); field 0 is `protection`. Avoids any Structure-size mistake.
        info = (_natural_t * _VM_REGION_SUBMAP_INFO_COUNT_64)()
        count = ctypes.c_uint(_VM_REGION_SUBMAP_INFO_COUNT_64)
        depth.value = 0  # only the top level of each region; recurse not needed
        rc = libc.mach_vm_region_recurse(
            target_task,
            ctypes.byref(address),
            ctypes.byref(size),
            ctypes.byref(depth),
            ctypes.cast(info, ctypes.c_void_p),
            ctypes.byref(count),
        )
        if rc != KERN_SUCCESS:
            # No more regions (KERN_INVALID_ADDRESS) — done walking.
            break

        region_addr = address.value
        region_size = size.value

        # Only scan readable regions. Large regions are read in overlapping
        # windows so a key literal located beyond the first _MAX_REGION_BYTES is
        # not missed and one straddling a window boundary is not split.
        readable = bool(info[0] & VM_PROT_READ)   # field 0 == protection
        if readable and region_size:
            offset = 0
            while offset < region_size and total_read < _TOTAL_SCAN_BUDGET:
                read_size = min(region_size - offset, _MAX_REGION_BYTES)
                chunk = _read_region(libc, target_task, region_addr + offset, read_size)
                if not chunk:
                    break
                total_read += len(chunk)
                for m in pattern.finditer(chunk):
                    hexpart = m.group(0)[2:-1]          # strip  x'  ...  '
                    try:
                        text = hexpart.decode("ascii").lower()
                    except UnicodeDecodeError:
                        continue
                    if len(text) != 96:
                        continue
                    found.setdefault(text[64:], text[:64])   # {salt_hex: key_hex}
                if read_size < _MAX_REGION_BYTES:
                    break  # consumed the whole region
                offset += _MAX_REGION_BYTES - _KEY_LITERAL_BYTES

        # Advance past this region. Guard against a zero-size region (would loop).
        if region_size == 0:
            address.value = region_addr + 1
        else:
            address.value = region_addr + region_size

        # Address wrapped or overflowed the 64-bit space — stop.
        if address.value <= region_addr:
            break

    return found


def _read_region(
    libc: ctypes.CDLL,
    task: "_mach_port_t",
    address: int,
    size: int,
) -> Optional[bytes]:
    """Copy ``size`` bytes at ``address`` from ``task`` via mach_vm_read_overwrite.

    Returns the bytes actually read, or ``None`` if the region is unreadable
    (the region is simply skipped by the caller).
    """
    buf = ctypes.create_string_buffer(size)
    outsize = _mach_vm_size_t(0)
    rc = libc.mach_vm_read_overwrite(
        task,
        _mach_vm_address_t(address),
        _mach_vm_size_t(size),
        _mach_vm_address_t(ctypes.addressof(buf)),
        ctypes.byref(outsize),
    )
    if rc != KERN_SUCCESS:
        return None
    n = outsize.value
    if n <= 0:
        return None
    return buf.raw[:n]


# --------------------------------------------------------------------------- #
# Image AES key (V2 .dat) — also memory-resident, found by validating candidates
# against a real ciphertext block. Cached in keys.json under these names.
# --------------------------------------------------------------------------- #
IMAGE_KEY_NAME = "_image_aes"
IMAGE_XOR_NAME = "_image_xor"


def load_image_key() -> Optional[str]:
    return load_cached_keys().get(IMAGE_KEY_NAME)


def load_image_xor() -> Optional[int]:
    v = load_cached_keys().get(IMAGE_XOR_NAME)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _iter_readable_regions(libc, task, max_total=_TOTAL_SCAN_BUDGET,
                           writable_only=False):
    """Yield readable memory chunks of *task* (windowed like extract_via_memscan).

    ``writable_only`` restricts to read-WRITE regions (heap/anon) — the image key
    lives there, and skipping read-only mapped libraries/shared-cache makes the
    scan dramatically faster.
    """
    address = _mach_vm_address_t(0)
    size = _mach_vm_size_t(0)
    depth = _natural_t(0)
    total = 0
    want = VM_PROT_READ | (VM_PROT_WRITE if writable_only else 0)
    while total < max_total:
        info = (_natural_t * _VM_REGION_SUBMAP_INFO_COUNT_64)()
        count = ctypes.c_uint(_VM_REGION_SUBMAP_INFO_COUNT_64)
        depth.value = 0
        rc = libc.mach_vm_region_recurse(
            task, ctypes.byref(address), ctypes.byref(size), ctypes.byref(depth),
            ctypes.cast(info, ctypes.c_void_p), ctypes.byref(count),
        )
        if rc != KERN_SUCCESS:
            break
        ra, rs = address.value, size.value
        if (info[0] & want) == want and rs:
            off = 0
            while off < rs and total < max_total:
                rd = min(rs - off, _MAX_REGION_BYTES)
                chunk = _read_region(libc, task, ra + off, rd)
                if not chunk:
                    break
                total += len(chunk)
                yield chunk
                if rd < _MAX_REGION_BYTES:
                    break
                off += _MAX_REGION_BYTES - _KEY_LITERAL_BYTES
        address.value = ra + (rs or 1)
        if address.value <= ra:
            break


def extract_image_key(pid: int, sample_block: bytes) -> Optional[str]:
    """Scan the live process for the 16-byte image AES key.

    Validates each 16/32-char ASCII candidate by AES-128-ECB decrypting
    *sample_block* (the first ciphertext block of a real V2 .dat) and checking
    the result begins with an image magic. Returns the key string or None.
    The key is only resident while WeChat is rendering images — open a chat with
    photos first.
    """
    import sys
    if sys.platform != "darwin":
        raise KeyExtractionError("图片密钥扫描仅支持 macOS。")
    try:
        from Crypto.Cipher import AES
    except ImportError as exc:
        raise KeyExtractionError("缺少 pycryptodome，无法校验图片密钥：pip install pycryptodome") from exc
    if not sample_block or len(sample_block) < 16:
        raise KeyExtractionError("缺少用于校验的图片样本块。")

    libc = _load_libsystem()
    self_task = libc.mach_task_self()
    target = _mach_port_t(0)
    rc = libc.task_for_pid(self_task, ctypes.c_int(pid), ctypes.byref(target))
    if rc != KERN_SUCCESS or target.value == 0:
        raise _task_for_pid_error(rc, pid)

    block = sample_block[:16]

    def good(k: bytes) -> bool:
        try:
            d = AES.new(k, AES.MODE_ECB).decrypt(block)
        except Exception:
            return False
        return (d[:3] == b"\xff\xd8\xff" or d[:8] == b"\x89PNG\r\n\x1a\n"
                or d[:4] in (b"GIF8",) or d[:4] == b"II*\x00" or d[:4] == b"RIFF")

    def scan(patterns):
        """One pass over the heap (RW) regions testing the given regex patterns."""
        seen = set()
        for chunk in _iter_readable_regions(libc, target, writable_only=True):
            for rgx in patterns:
                for m in rgx.finditer(chunk):
                    k = m.group(0)[:16]
                    if k in seen:
                        continue
                    seen.add(k)
                    if good(k):
                        return k.decode("ascii", "ignore")
        return None

    # The key is md5-derived hex in the common case — try that first (few
    # matches, fast), then fall back to broader alphanumeric tokens.
    hexpat = [re.compile(rb"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])"),
              re.compile(rb"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])")]
    res = scan(hexpat)
    if res:
        return res
    anpat = [re.compile(rb"(?<![0-9A-Za-z])[0-9A-Za-z]{32}(?![0-9A-Za-z])"),
             re.compile(rb"(?<![0-9A-Za-z])[0-9A-Za-z]{16}(?![0-9A-Za-z])")]
    return scan(anpat)


def get_image_key(pid: Optional[int], sample_block: bytes,
                  xor_byte: Optional[int] = None, use_cache: bool = True,
                  save: bool = True) -> Optional[str]:
    """Return the cached image key, or scan memory for it and cache it."""
    if use_cache:
        cached = load_image_key()
        if cached:
            return cached
    if pid is None:
        pid = find_wechat_pid()
    if not pid:
        raise KeyExtractionError("微信未运行，无法提取图片密钥（密钥只在渲染图片时存在于内存）。")
    key = extract_image_key(pid, sample_block)
    if key and save:
        d = load_cached_keys()
        d[IMAGE_KEY_NAME] = key
        if xor_byte is not None:
            d[IMAGE_XOR_NAME] = str(xor_byte)
        save_keys(d)
    return key


# --------------------------------------------------------------------------- #
# Backend 2: frida hook on sqlite3_key (OPTIONAL / secondary)
# --------------------------------------------------------------------------- #
#: JS injected into the target. Hooks the SQLCipher keying entry points and,
#: as an ObjC fallback, WCDB's DBEncryptInfo accessor. Hex-encodes the key and
#: ships it back over send().
_FRIDA_SCRIPT = r"""
'use strict';

function hexFromPtr(p, len) {
    if (p.isNull() || len <= 0) return null;
    try {
        var bytes = new Uint8Array(p.readByteArray(len));
        var out = '';
        for (var i = 0; i < bytes.length; i++) {
            out += ('0' + bytes[i].toString(16)).slice(-2);
        }
        return out;
    } catch (e) {
        return null;
    }
}

function hookKeyFn(name) {
    var addr = Module.findExportByName(null, name);
    if (addr === null) return false;
    Interceptor.attach(addr, {
        onEnter: function (args) {
            // int sqlite3_key(db, const void *pKey, int nKey)
            var keyPtr = args[1];
            var keyLen = args[2].toInt32();
            var hex = hexFromPtr(keyPtr, keyLen);
            if (hex) { send({ key: hex, via: name, len: keyLen }); }
        }
    });
    return true;
}

var hookedAny = false;
hookedAny = hookKeyFn('sqlite3_key') || hookedAny;
hookedAny = hookKeyFn('sqlite3_key_v2') || hookedAny;

// ObjC fallback: WCDB exposes the key through DBEncryptInfo m_dbEncryptKey.
try {
    if (ObjC.available) {
        var cls = ObjC.classes.DBEncryptInfo;
        if (cls && cls['- m_dbEncryptKey']) {
            Interceptor.attach(cls['- m_dbEncryptKey'].implementation, {
                onLeave: function (retval) {
                    try {
                        var data = new ObjC.Object(retval);
                        if (data && data.bytes && data.length) {
                            var len = data.length().toInt32();
                            var hex = hexFromPtr(data.bytes(), len);
                            if (hex) { send({ key: hex, via: 'DBEncryptInfo', len: len }); }
                        }
                    } catch (e) {}
                }
            });
            hookedAny = true;
        }
    }
} catch (e) {}

send({ ready: true, hooked: hookedAny });
"""


def extract_via_frida(pid: int) -> Dict[str, str]:
    """Hook ``sqlite3_key`` in the live process via frida to capture the key.

    Attaches to ``pid``, injects :data:`_FRIDA_SCRIPT` and listens for a few
    seconds for the keying call. You may need to nudge WeChat (open a chat) so
    that it (re)keys a database while the hook is live. The frida path yields
    the master key, so the result is ``{"*": key_hex}``.

    :raises KeyExtractionError: if frida is missing or cannot attach (which is
        common on macOS — the memscan backend is preferred there).
    """
    try:
        import frida  # type: ignore
    except ImportError as exc:
        raise KeyExtractionError(
            "未安装 frida，无法使用 frida 后端。\n"
            "  安装: pip install 'frida>=17,<18' 'frida-tools>=14,<15'\n"
            "  注意: macOS 上 frida 通常也无法附加到强化的微信进程，"
            "推荐改用默认的内存扫描(memscan)后端。"
        ) from exc

    collected: Dict[str, str] = {}

    try:
        session = frida.attach(pid)
    except Exception as exc:  # frida.* errors vary; normalize to our error type.
        raise KeyExtractionError(
            f"frida 无法附加到微信进程 (pid={pid}): {exc}\n"
            "macOS 上 frida 通常无法附加到强化运行时的微信，即使已自签名。\n"
            "请改用默认的内存扫描(memscan)后端(需要先自签名并用 sudo 运行)。"
        ) from exc

    def _on_message(message, data):  # noqa: ANN001 — frida callback signature
        if message.get("type") != "send":
            return
        payload = message.get("payload") or {}
        key = payload.get("key")
        if isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{64}", key):
            collected["*"] = key.lower()

    try:
        script = session.create_script(_FRIDA_SCRIPT)
        script.on("message", _on_message)
        script.load()
        # Give the user a window to open a chat so WeChat (re)keys a DB.
        import time
        deadline = time.time() + 8.0
        while time.time() < deadline and "*" not in collected:
            time.sleep(0.25)
    finally:
        try:
            session.detach()
        except Exception:
            pass

    return collected


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
_REMEDIATION = (
    "无法获取微信数据库密钥。请逐项检查：\n"
    "  1) 微信必须正在运行并已登录(密钥只存在于运行进程的内存中)。\n"
    "  2) 已对微信自签名(只需一次)：\n"
    "       sudo codesign --force --deep --sign - /Applications/WeChat.app\n"
    "     之后重新启动微信。\n"
    "  3) 用 sudo 运行本工具(读取进程内存需要特权)。\n"
    "  4) 若仍失败(最新版本)，可能需要关闭 SIP：在恢复模式执行 csrutil disable。\n"
    "  5) 也可手动提供密钥：--method manual --key <64位十六进制 或 keys.json 路径>。\n"
    "本工具仅用于导出你自己的微信聊天记录，且对微信数据保持只读。"
)


def get_keys(
    method: str = "auto",
    manual: Optional[str] = None,
    use_cache: bool = True,
    save: bool = True,
) -> Dict[str, str]:
    """Obtain the WeChat SQLCipher key map ``{salt_hex_or_"*": key_hex}``.

    :param method: one of ``auto`` / ``manual`` / ``memscan`` / ``frida`` /
        ``cache``. ``auto`` tries cache -> memscan -> frida in order.
    :param manual: for ``method="manual"`` (and accepted as an override in
        ``auto``): a 64-hex key or a path to a ``keys.json``.
    :param use_cache: in ``auto``, consult the on-disk cache first.
    :param save: persist a non-empty result to the cache.
    :raises KeyExtractionError: if every selected backend fails (with full
        remediation guidance).
    """
    method = (method or "auto").lower()

    # ---- explicit single backends -------------------------------------------
    if method == "manual":
        if not manual:
            raise KeyExtractionError(
                "method=manual 需要 --key：一个 64 位十六进制密钥或 keys.json 路径。"
            )
        keys = keys_from_manual(manual)
        if save and keys:
            save_keys(keys)
        return keys

    if method == "cache":
        keys = load_cached_keys()
        if keys:
            return keys
        raise KeyExtractionError(
            f"密钥缓存为空或不存在：{os.path.expanduser(constants.KEY_CACHE)}。\n"
            "请先用 --method memscan(或 auto)提取一次密钥。"
        )

    if method in ("memscan", "frida"):
        pid = find_wechat_pid()
        if pid is None:
            raise KeyExtractionError(
                "未检测到正在运行的微信进程。请先启动并登录微信，再重试。\n"
                f"  (查找的进程名: {', '.join(constants.WECHAT_PROCESS_NAMES)})"
            )
        keys = (
            extract_via_memscan(pid)
            if method == "memscan"
            else extract_via_frida(pid)
        )
        if not keys:
            raise KeyExtractionError(
                f"{method} 后端未能在微信进程中找到任何密钥。\n\n" + _REMEDIATION
            )
        if save:
            save_keys(keys)
        return keys

    if method != "auto":
        raise KeyExtractionError(
            f"未知的密钥提取方式 method={method!r}。"
            "可选: auto / manual / memscan / frida / cache。"
        )

    # ---- auto: manual override -> cache -> memscan -> frida ------------------
    if manual:
        keys = keys_from_manual(manual)
        if keys:
            if save:
                save_keys(keys)
            return keys

    if use_cache:
        cached = load_cached_keys()
        if cached:
            return cached

    pid = find_wechat_pid()
    if pid is None:
        raise KeyExtractionError(
            "未检测到正在运行的微信进程，且没有可用的密钥缓存。\n"
            "请先启动并登录微信(密钥只存在于运行进程的内存中)，再重试。\n\n"
            + _REMEDIATION
        )

    errors: List[str] = []

    # memscan first (preferred on macOS).
    try:
        keys = extract_via_memscan(pid)
        if keys:
            if save:
                save_keys(keys)
            return keys
        errors.append("memscan: 进程内存中未找到密钥字面量。")
    except KeyExtractionError as exc:
        errors.append(f"memscan: {exc}")

    # frida fallback (often blocked on macOS, but try anyway).
    try:
        keys = extract_via_frida(pid)
        if keys:
            if save:
                save_keys(keys)
            return keys
        errors.append("frida: 未捕获到 keying 调用(可尝试在 hook 期间打开一个会话)。")
    except KeyExtractionError as exc:
        errors.append(f"frida: {exc}")

    detail = "\n  - ".join(errors)
    raise KeyExtractionError(
        "所有密钥提取后端均失败：\n  - " + detail + "\n\n" + _REMEDIATION
    )
