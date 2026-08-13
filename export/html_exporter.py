"""Render a WeChat-style HTML page from a :class:`core.models.ExportBundle`.

The page is fully self-contained (inline CSS + JS, no external requests).  Any
media referenced by a message (image thumbnails, decoded voice audio, etc.) is
copied into a sibling ``<out_basename>_assets/`` folder and the bubble points at
a relative path, so the exported file plus that one folder are portable.

Read-only with respect to WeChat data: we only ever *copy* from a message's
``media_src`` (the resolved source file on disk) into the assets folder.
"""
from __future__ import annotations

import base64
import hashlib
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader, select_autoescape

from core import models
from core.app_paths import resource_path
from export import archive_manifest


# Template directory lives next to the package root (../templates).
TEMPLATE_DIR = resource_path("templates")

# Image-ish kinds get rendered as <img>; voice gets an <audio> player.
_IMAGE_KINDS = frozenset({"image", "sticker"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"})

# Compact emoji icon per kind for the non-text "card" rendering.  Kinds not
# listed here fall back to a generic label-only card.
_KIND_ICONS: Dict[str, str] = {
    "text": "",
    "image": "🖼️",
    "sticker": "😀",
    "voice": "🎤",
    "video": "🎬",
    "card": "👤",
    "location": "📍",
    "location_share": "📡",
    "link": "🔗",
    "file": "📎",
    "music": "🎵",
    "miniprogram": "📱",
    "channel": "📺",
    "channel_live": "🔴",
    "quote": "💬",
    "forward": "📦",
    "transfer": "💰",
    "redpacket": "🧧",
    "voip": "📞",
    "system": "ℹ️",
    "recall": "↩️",
    "app": "🧩",
    "unknown": "❓",
}


# Stable per-person avatar colors (WeChat shows photos; we approximate with a
# pleasant, name-derived tint so different people are visually distinct).
_AVATAR_COLORS = [
    "#5b8def", "#26a69a", "#ec7c4b", "#9b6dd6", "#e0699f",
    "#4db6ac", "#c0925a", "#6d8ab3", "#54b06a", "#c75b5b",
]


def _avatar_color(name: str) -> str:
    return _AVATAR_COLORS[sum(bytearray(name.encode("utf-8"))) % len(_AVATAR_COLORS)]


def kind_presentation(kind: str) -> Dict[str, str]:
    """Map a message ``kind`` to a small ``{icon, label}`` pair for the template."""
    return {
        "icon": _KIND_ICONS.get(kind, _KIND_ICONS["unknown"]),
        "label": models.constants.KIND_LABELS.get(kind, kind),
    }


def _is_audio(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in _AUDIO_EXTS


def _copy_media(media_path: Optional[str], assets_dir: str, assets_name: str,
                seen: Dict[str, str]) -> Optional[str]:
    """Copy one media file into ``assets_dir`` and return its relative path.

    ``media_path`` is a message's resolved source file (``Message.media_src``).
    Missing files are silently skipped (the caller still renders the text), so a
    partial media set never breaks the export. ``seen`` maps an already-copied
    source's realpath to its relative path, so the same source referenced by
    several messages (or a re-export) is copied only once.
    """
    if not media_path:
        return None
    src = os.path.expanduser(media_path)
    if not os.path.isfile(src):
        return None

    real = os.path.realpath(src)
    if real in seen:
        return seen[real]

    os.makedirs(assets_dir, exist_ok=True)
    base = os.path.basename(src)
    dest = os.path.join(assets_dir, base)

    # If something already sits at dest, keep it only when it is literally the
    # same file; otherwise pick a non-colliding name so we never clobber a
    # distinct source that happens to share a basename.
    if os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        n = 1
        while os.path.exists(dest):
            try:
                if os.path.samefile(src, dest):
                    break
            except OSError:
                pass
            base = "{0}_{1}{2}".format(stem, n, ext)
            dest = os.path.join(assets_dir, base)
            n += 1

    if not os.path.exists(dest):
        try:
            shutil.copy2(src, dest)
        except OSError:
            return None

    # Path relative to the HTML file (which sits beside the assets folder).
    rel = "{0}/{1}".format(assets_name, base)
    seen[real] = rel
    return rel


# --------------------------------------------------------------------------- #
# Self-contained (embedded) export: media inlined as data: URIs so a single
# downloaded HTML file works anywhere with no sibling assets folder.
# --------------------------------------------------------------------------- #
_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".mp4": "video/mp4", ".mov": "video/quicktime",
}


def _data_uri(src: str) -> Optional[str]:
    ext = os.path.splitext(src)[1].lower()
    mime = _MIME.get(ext, "application/octet-stream")
    try:
        with open(src, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))


def _media_ref(media_path, assets_dir, assets_name, seen, embed):
    """Resolve a message media file to either a relative asset path (copy mode)
    or an inlined ``data:`` URI (embed mode). Each unique source is read once."""
    if not media_path:
        return None
    src = os.path.expanduser(media_path)
    if not os.path.isfile(src):
        return None
    if not embed:
        return _copy_media(media_path, assets_dir, assets_name, seen)
    real = os.path.realpath(src)
    if real in seen:
        return seen[real]
    uri = _data_uri(src)
    if uri is not None:
        seen[real] = uri
    return uri


def _avatar_ref(avatar_path, assets_dir, assets_name, seen, embed,
                avatar_styles) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(img_src, css_token)``. In embed mode the avatar is registered
    ONCE in ``avatar_styles`` (token -> data URI) and referenced by a CSS class,
    so an avatar shown on thousands of messages is inlined a single time."""
    if not avatar_path:
        return None, None
    src = os.path.expanduser(avatar_path)
    if not os.path.isfile(src):
        return None, None
    if not embed:
        return _copy_media(avatar_path, assets_dir, assets_name, seen), None
    real = os.path.realpath(src)
    cache_key = ("av", real)
    tok = seen.get(cache_key)
    if tok is None:
        uri = _data_uri(src)
        if uri is None:
            return None, None
        tok = "a" + hashlib.md5(real.encode()).hexdigest()[:10]
        avatar_styles[tok] = uri
        seen[cache_key] = tok
    return None, tok


def _build_message_dict(
    bundle: models.ExportBundle,
    msg: models.Message,
    assets_dir: str,
    assets_name: str,
    seen: Dict[str, str],
    embed: bool,
    avatar_styles: Dict[str, str],
    sequence: int,
    show_time_marker: bool,
) -> Dict[str, Any]:
    """Flatten one :class:`Message` into the dict the template consumes."""
    media_rel = _media_ref(msg.media_src, assets_dir, assets_name, seen, embed)
    pres = kind_presentation(msg.kind)

    is_image = msg.kind in _IMAGE_KINDS and bool(media_rel)
    is_audio = msg.kind == "voice" and bool(media_rel)

    # voice duration in whole seconds (for the WeChat-style voice bubble)
    vlen = (msg.extra or {}).get("voicelength_ms")
    try:
        voice_seconds = max(1, int(round(int(vlen) / 1000))) if vlen else None
    except (TypeError, ValueError):
        voice_seconds = None

    avatar_img, avatar_token = _avatar_ref(
        bundle.avatar_src(msg), assets_dir, assets_name, seen, embed, avatar_styles)
    video_src = msg.extra.get("video_file") if msg.kind == "video" else None
    video_out = _media_ref(video_src, assets_dir, assets_name, seen, embed) if video_src else None

    sender = bundle.sender_name(msg)
    return {
        "sender_name": sender,
        "sequence": sequence,
        "avatar_text": (sender[:1].upper() if sender else "?"),
        "avatar_color": _avatar_color(sender),
        "avatar_img": avatar_img,
        "avatar_token": avatar_token,
        "is_sender": bool(msg.is_sender),
        "time_str": msg.time_str,
        "full_time_str": msg.full_time_str,
        "datetime_str": msg.datetime_str,
        "iso_time_str": msg.iso_time_str,
        "timezone_offset_str": msg.timezone_offset_str,
        "timestamp": int(msg.timestamp),
        "show_time_marker": show_time_marker,
        "date_key": msg.date_key,
        "kind": msg.kind,
        "kind_label": msg.kind_label,
        "kind_icon": pres["icon"],
        "display_text": msg.display_text or "",
        "media_out": media_rel,
        "video_out": video_out,
        "is_image": is_image,
        "is_audio": is_audio,
        "voice_text": msg.voice_text or "",
        "voice_seconds": voice_seconds,
        "extra": msg.extra or {},
    }


def _group_by_date(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group already-ordered message dicts into ``[{date, messages}]`` runs."""
    groups: List[Dict[str, Any]] = []
    current_key: Optional[str] = None
    for row in rows:
        if row["date_key"] != current_key:
            current_key = row["date_key"]
            groups.append({"date": current_key, "messages": []})
        groups[-1]["messages"].append(row)
    return groups


def _build_stats(bundle: models.ExportBundle, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(rows)
    first = rows[0]["date_key"] if rows else None
    last = rows[-1]["date_key"] if rows else None
    return {
        "total": total,
        "start_date": bundle.start_date or first,
        "end_date": bundle.end_date or last,
    }


def export_html(bundle: models.ExportBundle, out_path: str,
                embed_media: bool = False) -> str:
    """Render ``bundle`` to an HTML file at ``out_path``.

    With ``embed_media=False`` (default), media is copied into a sibling
    ``<out_basename>_assets/`` folder and referenced relatively (smaller file,
    but the folder must travel with the HTML).

    With ``embed_media=True`` the HTML is FULLY self-contained — every image,
    avatar, voice clip and video is inlined as a ``data:`` URI — so a single
    downloaded/shared file works on its own with no assets folder.
    """
    out_path = os.path.expanduser(out_path)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)

    base_name = os.path.splitext(os.path.basename(out_path))[0]
    assets_name = "{0}_assets".format(base_name)
    assets_dir = os.path.join(out_dir, assets_name)
    # Clear stale assets so re-exports don't accumulate orphaned media (the temp
    # source paths change each run, defeating per-run dedup). Not needed in
    # embed mode (no assets folder is created).
    if not embed_media and os.path.isdir(assets_dir):
        shutil.rmtree(assets_dir, ignore_errors=True)

    seen: Dict[str, str] = {}
    avatar_styles: Dict[str, str] = {}
    rows = []
    previous_timestamp = None
    for sequence, msg in enumerate(bundle.messages, 1):
        show_time_marker = (
            previous_timestamp is None
            or msg.date_key != bundle.messages[sequence - 2].date_key
            or int(msg.timestamp) - int(previous_timestamp) >= 300
        )
        rows.append(_build_message_dict(
            bundle, msg, assets_dir, assets_name, seen,
            embed_media, avatar_styles, sequence, show_time_marker,
        ))
        previous_timestamp = int(msg.timestamp)
    messages_by_date = _group_by_date(rows)
    stats = _build_stats(bundle, rows)

    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("chat.html")
    html = template.render(
        contact=bundle.contact,
        owner=bundle.owner,
        messages_by_date=messages_by_date,
        stats=stats,
        generated_at=bundle.generated_at or "",
        avatar_styles=avatar_styles,
        archive=archive_manifest.archive_metadata(bundle),
        contact_identity=archive_manifest.contact_identity(bundle.contact),
        owner_identity=archive_manifest.contact_identity(bundle.owner),
    )

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    return out_path
