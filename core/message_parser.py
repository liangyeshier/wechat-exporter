"""Classify and render each WeChat message.

Given a :class:`~core.models.Message` with its base ``msg_type``, optional
``sub_type`` and raw text/XML ``raw_content``, this module fills in ``kind``,
``display_text``, ``extra`` (type-specific metadata) and the voice flags.

It is deliberately defensive: WeChat XML is frequently truncated or has stray
bytes, group content carries a ``wxid:\\n`` sender prefix, and v4 content may be
zstd-decompressed upstream. Anything unparseable degrades to a readable label
rather than raising.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

from . import constants
from .models import Message

# WeChat group messages prefix the body with "<sender-wxid>:\n".
_GROUP_PREFIX_RE = re.compile(r"^([0-9A-Za-z_\-]+@?[0-9A-Za-z_\-.]*):\n", re.S)


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #
def split_group_prefix(content: str) -> Tuple[Optional[str], str]:
    """Return ``(sender_wxid, body)`` for group content; sender is None if absent."""
    if not content:
        return None, content
    m = _GROUP_PREFIX_RE.match(content)
    if m:
        return m.group(1), content[m.end():]
    return None, content


def _xml(content: str) -> Optional[ET.Element]:
    """Best-effort parse: trim junk before the first '<', tolerate failures."""
    if not content:
        return None
    start = content.find("<")
    if start < 0:
        return None
    snippet = content[start:]
    # WeChat link/app cards routinely contain a bare '&' (e.g. 'A&nbsp;B' or
    # 'x?a=1&b=2' in a title/url). XML only predefines amp/lt/gt/quot/apos (plus
    # numeric refs); anything else — including the very common '&nbsp;' — makes
    # ElementTree raise and silently drops the card's title/url. Escape every '&'
    # that is NOT one of those valid references.
    snippet = re.sub(
        r"&(?!(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);)", "&amp;", snippet
    )
    try:
        return ET.fromstring(snippet)
    except (ET.ParseError, ValueError):
        # Retry after stripping a trailing partial tag, a common truncation.
        try:
            return ET.fromstring(snippet[: snippet.rfind(">") + 1])
        except Exception:
            return None


def _find(el: Optional[ET.Element], path: str, default: str = "") -> str:
    if el is None:
        return default
    node = el.find(path)
    if node is not None and node.text:
        return node.text.strip()
    # also allow attribute-style "tag@attr"
    return default


def _attr(el: Optional[ET.Element], path: str, attr: str, default: str = "") -> str:
    if el is None:
        return default
    node = el if path == "." else el.find(path)
    if node is not None:
        return (node.get(attr) or default).strip()
    return default


# --------------------------------------------------------------------------- #
# Per-type handlers — each returns (kind, display_text) and mutates msg.extra.
# --------------------------------------------------------------------------- #
def _handle_app(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    appmsg = root.find("appmsg") if root is not None else None
    if appmsg is None:
        return "app", "[应用消息]"

    try:
        sub = int(_find(appmsg, "type", "0") or "0")
    except ValueError:
        sub = 0
    msg.sub_type = sub or msg.sub_type
    kind = constants.APPMSG_SUBTYPES.get(sub, "app")
    title = _find(appmsg, "title")
    url = _find(appmsg, "url")
    msg.extra.update({"title": title, "url": url, "appmsg_type": sub})

    if kind == "link":
        desc = _find(appmsg, "des")
        msg.extra["desc"] = desc
        return "link", "[链接] %s" % (title or url or "")
    if kind == "file":
        ext = _find(appmsg, "appattach/fileext")
        msg.extra["fileext"] = ext
        return "file", "[文件] %s" % title
    if kind == "miniprogram":
        return "miniprogram", "[小程序] %s" % title
    if kind == "channel" or kind == "channel_live":
        return kind, "[视频号] %s" % title
    if kind == "transfer":
        wp = appmsg.find("wcpayinfo")
        amount = _find(wp, "feedesc")              # e.g. "￥3025.00"
        subtype = _find(wp, "paysubtype")
        memo = _find(wp, "pay_memo")
        # paysubtype: 1 发起转账, 3 已收款, 4/9 已退还, 5 已收款, 8 待确认/转账
        status = {"1": "待收款", "3": "已收款", "4": "已退还", "5": "已收款",
                  "8": "微信转账", "9": "已退还"}.get(subtype, "微信转账")
        msg.extra.update({"amount": amount, "transfer_status": status,
                          "memo": memo, "paysubtype": subtype})
        parts = [p for p in [amount or "转账", status,
                             ("备注: " + memo) if memo else ""] if p]
        return "transfer", " · ".join(parts)
    if kind == "redpacket":
        wp = appmsg.find("wcpayinfo")
        greet = (_find(wp, "receivertitle") or _find(wp, "sendertitle")
                 or title or "微信红包")
        msg.extra["redpacket_greet"] = greet
        return "redpacket", greet
    if kind == "forward":
        return "forward", "[合并转发] %s" % title
    if kind == "quote":
        refer = appmsg.find("refermsg")
        quoted_from = _find(refer, "displayname")
        quoted_raw = _find(refer, "content")
        # The quoted original may itself be a structured (XML) message — e.g. an
        # image or another card. Don't dump raw XML into the text; summarize it
        # by the refermsg's own type so the bubble stays readable.
        if quoted_raw.lstrip().startswith("<"):
            try:
                qt = int(_find(refer, "type", "0") or "0")
            except ValueError:
                qt = 0
            quoted_text = "[%s]" % constants.KIND_LABELS.get(
                constants.BASE_MSG_TYPES.get(qt, "unknown"), "消息"
            )
        else:
            quoted_text = quoted_raw
        msg.extra.update({"quoted_from": quoted_from, "quoted_text": quoted_text})
        body_txt = title or _find(appmsg, "des")
        if quoted_text:
            return "quote", "%s\n  ↪ 引用 %s: %s" % (body_txt, quoted_from, quoted_text)
        return "quote", body_txt
    if kind == "music":
        return "music", "[音乐] %s" % title
    # generic appmsg
    return "app", "[应用消息] %s" % (title or url or "")


def _handle_image(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    img = root.find("img") if root is not None else None
    if img is not None:
        msg.extra.update({
            "md5": img.get("md5", ""),
            "aeskey": img.get("aeskey", ""),
            "cdnurl": img.get("cdnurl") or img.get("cdnthumburl", ""),
        })
    return "image", "[图片]"


def _handle_voice(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    vm = root.find("voicemsg") if root is not None else None
    if vm is not None:
        msg.extra.update({
            "voicelength_ms": vm.get("voicelength", ""),
            "clientmsgid": vm.get("clientmsgid", ""),
        })
    msg.needs_voice = True
    return "voice", "[语音]"


def _handle_video(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    vm = root.find("videomsg") if root is not None else None
    if vm is not None:
        msg.extra.update({
            "playlength": vm.get("playlength", ""),
            "md5": vm.get("rawmd5") or vm.get("md5", ""),
            "cdnurl": vm.get("cdnvideourl", ""),
        })
    return "video", "[视频]"


def _handle_sticker(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    emoji = root.find("emoji") if root is not None else None
    if emoji is not None:
        msg.extra["url"] = emoji.get("cdnurl") or emoji.get("thumburl", "")
    return "sticker", "[表情]"


def _handle_location(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    loc = None
    if root is not None:
        loc = root.find("location")
        if loc is None and root.tag == "location":
            loc = root
    label = poiname = ""
    if loc is not None:
        label = loc.get("label", "")
        poiname = loc.get("poiname", "")
        msg.extra.update({"x": loc.get("x", ""), "y": loc.get("y", ""),
                          "label": label, "poiname": poiname})
    text = poiname or label or ""
    return "location", "[位置] %s" % text if text else "[位置]"


def _handle_card(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    nickname = ""
    if root is not None:
        nickname = root.get("nickname", "")
        msg.extra.update({"nickname": nickname, "username": root.get("username", "")})
    return "card", "[名片] %s" % nickname if nickname else "[名片]"


def _handle_voip(msg: Message, body: str) -> Tuple[str, str]:
    # Extract any human-readable status text from the (often nested) XML.
    root = _xml(body)
    text = ""
    if root is not None:
        for node in root.iter():
            if node.text and node.text.strip() and len(node.text.strip()) < 40:
                text = node.text.strip()
                if any(k in text for k in ("通话", "Duration", "时长", "取消", "拒绝", "未接")):
                    break
    return "voip", "[通话] %s" % text if text else "[通话]"


def _handle_system(msg: Message, body: str) -> Tuple[str, str]:
    root = _xml(body)
    if root is not None:
        # revoke / pat / generic sysmsg often carry a plain text child
        for tag in ("revokemsg", "plaintext", "text"):
            t = _find(root, ".//" + tag)
            if t:
                return "system", t
    # strip tags from plain-ish content
    txt = re.sub(r"<[^>]+>", "", body or "").strip()
    return "system", txt or "[系统消息]"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def parse(msg: Message, is_group: bool = False) -> Message:
    """Enrich ``msg`` in place and return it."""
    content = msg.raw_content or ""

    # Resolve the group sender prefix if the reader did not already.
    if is_group and not msg.sender_username:
        sender, content = split_group_prefix(content)
        if sender:
            msg.sender_username = sender
    elif is_group:
        _, content = split_group_prefix(content)

    base = msg.msg_type & 0xFFFFFFFF
    high = (msg.msg_type >> 32) & 0xFFFFFFFF
    if high:
        msg.sub_type = msg.sub_type or high

    if base == 1:
        msg.kind, msg.display_text = "text", content.strip()
    elif base == 3:
        msg.kind, msg.display_text = _handle_image(msg, content)
    elif base == 34:
        msg.kind, msg.display_text = _handle_voice(msg, content)
    elif base == 42:
        msg.kind, msg.display_text = _handle_card(msg, content)
    elif base == 43:
        msg.kind, msg.display_text = _handle_video(msg, content)
    elif base == 47:
        msg.kind, msg.display_text = _handle_sticker(msg, content)
    elif base == 48:
        msg.kind, msg.display_text = _handle_location(msg, content)
    elif base == 49:
        msg.kind, msg.display_text = _handle_app(msg, content)
    elif base == 50:
        msg.kind, msg.display_text = _handle_voip(msg, content)
    elif base in (10000, 10002):
        msg.kind, msg.display_text = _handle_system(msg, content)
        if base == 10002:
            msg.kind = "recall"
    else:
        msg.kind = constants.BASE_MSG_TYPES.get(base, "unknown")
        msg.display_text = "[%s]" % constants.KIND_LABELS.get(msg.kind, "未知消息 type=%d" % base)

    # Translate WeChat built-in emoticon codes (e.g. "[捂脸]") to Unicode emoji.
    if msg.display_text:
        try:
            from . import emoji_map
            msg.display_text = emoji_map.replace_emoji(msg.display_text)
        except Exception:
            pass

    return msg
