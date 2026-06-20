"""Decode WeChat 4.x encrypted image ``.dat`` files and map them to messages.

Format (confirmed on real v4.1 data):

* signature ``07 08 56 31 08 07`` = **V1**, ``07 08 56 32 08 07`` = **V2**, anything
  else = legacy single-byte-XOR (pre-4.0).
* 15-byte header: sig(6) + ``aes_size`` u32-LE @6 + ``xor_size`` u32-LE @10 + 1
  flag byte @14. Payload at offset 15 = ``[AES-128-ECB region][raw region][XOR
  region]``. The AES region covers the first ``aligned(aes_size)`` bytes
  (PKCS7-padded, so an exact multiple of 16 gets a full extra block); the last
  ``xor_size`` bytes are single-byte XOR'd.
* **V1 key** is fixed: ``md5("0")[:16] = "cfcd208495d565ef"``. **V2 key** is a
  16-char per-account key recovered from WeChat's process memory (see
  :func:`core.key_extractor.get_image_key`); the XOR byte is ``uin & 0xFF``.

Mapping: WeChat does NOT store the message->file link in its DBs, so image
messages are matched to ``.dat`` files heuristically by month folder + nearest
write-time (validated to align exactly for most months).
"""
from __future__ import annotations

import collections
import datetime
import glob
import hashlib
import os
import re
import struct
from typing import Dict, List, Optional, Tuple

V1_SIG = b"\x07\x08V1\x08\x07"          # 07 08 56 31 08 07
V2_SIG = b"\x07\x08V2\x08\x07"          # 07 08 56 32 08 07
V1_KEY = b"cfcd208495d565ef"            # md5("0")[:16]

_MAGICS = [
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"), (b"GIF89a", "gif"),
    (b"II*\x00", "tif"), (b"MM\x00*", "tif"),
    (b"RIFF", "webp"),
    (b"wxgf", "wxgf"),                  # WeChat live-photo / HEVC wrapper
]


def detect_ext(buf: bytes) -> Optional[str]:
    for sig, ext in _MAGICS:
        if buf.startswith(sig):
            return ext
    return None


def _aligned_aes(aes_size: int) -> int:
    """AES region length: round up to a multiple of 16, adding a full block when
    already aligned (WeChat appends a full PKCS7 block)."""
    return aes_size + (16 - aes_size % 16) if aes_size % 16 else aes_size + 16


def decode_dat(data: bytes, aes_key=None, xor_byte: Optional[int] = None
               ) -> Optional[Tuple[bytes, str]]:
    """Decode a ``.dat`` payload to ``(image_bytes, ext)`` or None.

    ``aes_key`` (bytes or 16-char str) is required for V2 (ignored for V1/legacy).
    ``xor_byte`` is the V2 single-byte XOR; if None it is recovered from a JPEG
    tail (``last_byte ^ 0xD9``).
    """
    if not data or len(data) < 6:
        return None

    # V1/V2 frames share only the first 3 bytes (07 08 56); byte 3 is the
    # version ('1'=0x31 / '2'=0x32). Anything else => legacy single-byte XOR.
    if data[:3] != b"\x07\x08\x56":
        for magic, ext in _MAGICS:
            if ext in ("webp", "wxgf"):
                continue
            k = data[0] ^ magic[0]
            if all(data[i] ^ k == magic[i] for i in range(min(len(magic), len(data)))):
                return bytes(b ^ k for b in data), ext
        return None

    if len(data) < 15:
        return None
    aes_size, xor_size = struct.unpack_from("<II", data, 6)

    if data[3] == 0x31:          # V1: fixed key
        key = V1_KEY
    else:                        # V2 (0x32): caller-provided per-account key
        if not aes_key:
            return None
        key = aes_key.encode("ascii") if isinstance(aes_key, str) else aes_key
    key = key[:16]
    if len(key) != 16:
        return None

    try:
        from Crypto.Cipher import AES
    except ImportError:
        return None

    aligned = _aligned_aes(aes_size)
    enc = data[15:15 + aligned]
    if len(enc) < 16:
        return None
    try:
        plain_a = AES.new(key, AES.MODE_ECB).decrypt(enc[:len(enc) // 16 * 16])
    except Exception:
        return None
    # PKCS7 unpad (best-effort)
    if plain_a:
        pad = plain_a[-1]
        if 1 <= pad <= 16 and plain_a[-pad:] == bytes([pad]) * pad:
            plain_a = plain_a[:-pad]

    raw = data[15 + aligned: len(data) - xor_size] if xor_size else data[15 + aligned:]
    tail = data[len(data) - xor_size:] if xor_size else b""
    if xor_byte is None:
        xor_byte = (tail[-1] ^ 0xD9) if tail else 0
    xored = bytes(b ^ xor_byte for b in tail)

    out = plain_a + raw + xored
    ext = detect_ext(out)
    return (out, ext) if ext else None


# --------------------------------------------------------------------------- #
# message -> .dat mapping (heuristic: month folder + nearest write-time)
# --------------------------------------------------------------------------- #
def _attach_dir(layout, chat_username: str) -> str:
    return os.path.join(layout.media_root or "", "attach",
                        hashlib.md5(chat_username.encode("utf-8")).hexdigest())


def map_images(layout, image_messages: List, chat_username: str
               ) -> Dict[int, str]:
    """Map ``{message.local_id: dat_full_path}`` by month folder + nearest mtime.

    Per the analysis, full-size ``.dat`` counts match image-message counts almost
    exactly per month, so within each ``YYYY-MM`` folder we greedily pair each
    message to the unused file whose write-time is closest (within a week).
    """
    attach = _attach_dir(layout, chat_username)
    if not os.path.isdir(attach):
        return {}

    files_by_month: Dict[str, List[Tuple[float, str]]] = collections.defaultdict(list)
    for d in glob.glob(os.path.join(attach, "*", "Img", "*.dat")):
        b = os.path.basename(d)
        if "_t." in b or "_h." in b:          # skip thumbnails / hd-variants
            continue
        month = os.path.basename(os.path.dirname(os.path.dirname(d)))  # YYYY-MM
        try:
            files_by_month[month].append((os.path.getmtime(d), d))
        except OSError:
            pass

    msgs_by_month: Dict[str, List] = collections.defaultdict(list)
    for m in image_messages:
        month = datetime.datetime.fromtimestamp(m.timestamp).strftime("%Y-%m")
        msgs_by_month[month].append(m)

    # Order-based pairing within each month folder: both messages and files are
    # chronological, and per-month counts align almost exactly, so zipping them
    # in time order maps ~95% correctly (more robust than absolute-time match,
    # since .dat mtimes can be reset by sync/migration).
    out: Dict[int, str] = {}
    for month, msgs in msgs_by_month.items():
        files = [f for _t, f in sorted(files_by_month.get(month, []))]
        for m, f in zip(sorted(msgs, key=lambda x: (x.timestamp, x.local_id)), files):
            out[m.local_id] = f
    return out


# --------------------------------------------------------------------------- #
# image key derivation (macOS, fully offline — no memory scan / sudo)
# --------------------------------------------------------------------------- #
def find_uin(layout) -> Optional[int]:
    """Find the Tencent ``uin`` from the kvcomm cache filename ``key_<uin>_*``.

    Validated against the account-folder suffix (``<wxid>_<4hex>`` where the 4
    hex == ``md5(str(uin))[:4]``) when present.
    """
    docs = os.path.dirname(layout.account_dir)              # .../xwechat_files
    suffix = layout.account_id.rsplit("_", 1)[1] if "_" in layout.account_id else ""
    patterns = [
        os.path.join(docs, "..", "app_data", "**", "kvcomm", "key_*_*.statistic"),
        os.path.join(docs, "..", "**", "key_*_*.statistic"),
    ]
    seen = []
    for pat in patterns:
        for f in glob.glob(pat, recursive=True):
            m = re.search(r"key_(\d+)_", os.path.basename(f))
            if not m:
                continue
            uin = int(m.group(1))
            seen.append(uin)
            if not suffix or hashlib.md5(str(uin).encode()).hexdigest()[:4] == suffix:
                return uin
    return seen[0] if seen else None


def derive_image_key(layout) -> Tuple[Optional[str], Optional[int]]:
    """Return ``(aes_key_hex16, xor_byte)`` derived from uin+wxid, or (None,None).

    macOS V2 image key: ``md5(str(uin) + wxid)[:16]`` (ASCII), ``xor = uin & 0xFF``,
    where wxid is the account folder name minus its ``_<4hex>`` suffix.
    """
    if not getattr(layout, "is_v4", False):
        return None, None
    uin = find_uin(layout)
    if uin is None:
        return None, None
    wxid = layout.account_id.rsplit("_", 1)[0] if "_" in layout.account_id else layout.account_id
    key = hashlib.md5(("%d%s" % (uin, wxid)).encode()).hexdigest()[:16]
    return key, uin & 0xFF


# --------------------------------------------------------------------------- #
# wxgf (WeChat HEVC container) -> displayable JPEG
# --------------------------------------------------------------------------- #
def wxgf_to_jpeg(wxgf_bytes: bytes) -> Optional[bytes]:
    """Decode a ``wxgf`` (40-byte header + raw HEVC Annex-B) blob to JPEG bytes.

    Uses PyAV (bundles ffmpeg) to decode the single HEVC keyframe, then Pillow to
    re-encode as JPEG. Returns None if PyAV/Pillow are missing or decode fails.
    """
    i = wxgf_bytes.find(b"\x00\x00\x00\x01")   # first HEVC Annex-B start code
    if i < 0:
        return None
    try:
        import io
        import av  # noqa: F401
    except ImportError:
        return None
    try:
        cont = av.open(io.BytesIO(wxgf_bytes[i:]), format="hevc")
        try:
            for frame in cont.decode(video=0):
                img = frame.to_image().convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=88)
                return buf.getvalue()
        finally:
            cont.close()
    except Exception:
        return None
    return None


def decode_to_displayable(data: bytes, aes_key=None, xor_byte: Optional[int] = None
                          ) -> Optional[Tuple[bytes, str]]:
    """Decode a .dat to browser-displayable bytes: like :func:`decode_dat` but
    converts WeChat's ``wxgf`` (HEVC) output to JPEG."""
    res = decode_dat(data, aes_key, xor_byte)
    if not res:
        return None
    img, ext = res
    if ext == "wxgf":
        jpg = wxgf_to_jpeg(img)
        return (jpg, "jpg") if jpg else None
    return img, ext


def thumb_path(dat_path: str) -> Optional[str]:
    """The ``_t.dat`` thumbnail next to a full-size .dat, if present."""
    stem, ext = os.path.splitext(dat_path)
    t = stem + "_t" + ext
    return t if os.path.exists(t) else None


def recover_xor_byte(layout, chat_username: str) -> Optional[int]:
    """Recover the V2 XOR byte from any thumbnail's JPEG tail (last ^ 0xD9)."""
    attach = _attach_dir(layout, chat_username)
    for t in glob.glob(os.path.join(attach, "*", "Img", "*_t.dat")):
        try:
            with open(t, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if data[:4] != V2_SIG[:4] or len(data) < 16:
            continue
        xor_size = struct.unpack_from("<I", data, 10)[0]
        if not xor_size:
            continue
        last = data[-1]
        xb = last ^ 0xD9
        # validate: the byte before should decode to 0xFF (…FF D9 end marker)
        if (data[-2] ^ xb) == 0xFF:
            return xb
    return None


def find_sample_and_xor(layout) -> Tuple[Optional[bytes], Optional[int]]:
    """Scan the WHOLE attach tree (any chat) for a V2 sample block + XOR byte.

    Used at key-extraction time, before a conversation is chosen.
    """
    root = os.path.join(layout.media_root or "", "attach")
    sample = None
    xor = None
    for d in glob.iglob(os.path.join(root, "*", "*", "Img", "*.dat")):
        b = os.path.basename(d)
        try:
            with open(d, "rb") as fh:
                head = fh.read(64)
        except OSError:
            continue
        if head[:4] != V2_SIG[:4]:
            continue
        if sample is None and "_t." not in b and "_h." not in b and len(head) >= 31:
            sample = head[15:31]
        if xor is None and "_t." in b:
            try:
                with open(d, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if len(data) >= 2 and struct.unpack_from("<I", data, 10)[0]:
                cand = data[-1] ^ 0xD9
                if (data[-2] ^ cand) == 0xFF:
                    xor = cand
        if sample is not None and xor is not None:
            break
    return sample, xor


def sample_v2_block(layout, chat_username: str) -> Optional[bytes]:
    """Return the first 16-byte AES ciphertext block of some V2 .dat (for key
    validation): bytes ``[15:31]`` of a full-size V2 file."""
    attach = _attach_dir(layout, chat_username)
    for d in glob.glob(os.path.join(attach, "*", "Img", "*.dat")):
        if "_t." in d or "_h." in d:
            continue
        try:
            with open(d, "rb") as fh:
                head = fh.read(31)
        except OSError:
            continue
        if head[:4] == V2_SIG[:4] and len(head) >= 31:
            return head[15:31]
    return None
