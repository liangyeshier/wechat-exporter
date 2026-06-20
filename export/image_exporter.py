"""Render a :class:`core.models.ExportBundle` to A4 images and a PDF for PRINTING.

The goal is a paper-friendly transcript: WeChat-like left/right bubbles, but
packed densely so a long conversation fits on as few A4 sheets as possible.
Everything is drawn with Pillow (PIL) onto white A4 portrait canvases.

Public API
----------
- :func:`export_a4_images` -> writes ``<base_name>_p01.png`` ... and returns paths.
- :func:`export_pdf`        -> writes one multi-page A4 PDF and returns its path.

Both delegate to :func:`render_pages`, which returns the list of ``PIL.Image``
pages so the two entry points stay in sync.

Read-only with respect to WeChat data: we only ever *read* a message's
``media_src`` (a resolved jpg/wav on disk during export). Anything missing,
unreadable, or malformed degrades to a text placeholder instead of crashing.
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from core import models


# --------------------------------------------------------------------------- #
# Page geometry
# --------------------------------------------------------------------------- #
# A4 is 210x297 mm.  At a given DPI the pixel size is mm/25.4*dpi.
_A4_MM = (210.0, 297.0)


def _a4_px(dpi: int) -> Tuple[int, int]:
    w = int(round(_A4_MM[0] / 25.4 * dpi))
    h = int(round(_A4_MM[1] / 25.4 * dpi))
    return (w, h)


# Density presets are expressed as *fractions of the page width* so they scale
# cleanly across DPIs.  ``body`` etc. are font sizes; ``line``/``gap`` are
# multipliers; ``margin``/``avatar`` are page-width fractions.
_DENSITY = {
    "compact": {"body": 0.0150, "small": 0.0118, "title": 0.0260,
                "line": 1.18, "msg_gap": 0.0060, "avatar": 0.0220,
                "pad": 0.0070, "bubble_w": 0.62, "img_w": 0.46},
    "normal":  {"body": 0.0180, "small": 0.0140, "title": 0.0300,
                "line": 1.30, "msg_gap": 0.0100, "avatar": 0.0280,
                "pad": 0.0090, "bubble_w": 0.62, "img_w": 0.50},
    "large":   {"body": 0.0230, "small": 0.0175, "title": 0.0360,
                "line": 1.40, "msg_gap": 0.0150, "avatar": 0.0360,
                "pad": 0.0120, "bubble_w": 0.64, "img_w": 0.54},
}

# Colors
_WHITE = (255, 255, 255)
_PAGE_BG = (255, 255, 255)
_BUBBLE_IN = (255, 255, 255)
_BUBBLE_IN_BORDER = (224, 224, 224)
_BUBBLE_OUT = (149, 236, 105)        # #95ec69 WeChat outgoing green
_TEXT = (30, 30, 30)
_TEXT_MUTED = (120, 120, 120)
_PILL_BG = (224, 224, 224)
_PILL_TEXT = (110, 110, 110)
_AVATAR_TEXT = (255, 255, 255)
_CARD_BG = (245, 245, 247)
_CARD_BORDER = (220, 220, 222)

# Stable per-person avatar tints (mirrors html_exporter's palette intent).
_AVATAR_COLORS = [
    (91, 141, 239), (38, 166, 154), (236, 124, 75), (155, 109, 214),
    (224, 105, 159), (77, 182, 172), (192, 146, 90), (109, 138, 179),
    (84, 176, 106), (199, 91, 91),
]

# macOS font candidates, in priority order (CJK + broad Unicode coverage).
_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]

# Compact label per kind for the one-line "card" rendering of misc messages.
_KIND_LABELS = {
    "link": "链接", "file": "文件", "transfer": "转账", "redpacket": "红包",
    "location": "位置", "location_share": "实时位置", "card": "名片",
    "music": "音乐", "miniprogram": "小程序", "channel": "视频号",
    "channel_live": "视频号直播", "forward": "合并转发", "voip": "通话",
    "app": "应用消息", "quote": "引用",
}


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
def _find_font_path() -> Optional[str]:
    for p in _FONT_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


class _Fonts:
    """Lazily-built, size-keyed font cache around one resolved font file.

    Falls back to PIL's bitmap default if no system font loads, so rendering
    never hard-fails on a font-less machine (text just looks plainer).
    """

    def __init__(self) -> None:
        self._path = _find_font_path()
        self._cache = {}

    def get(self, size: int) -> ImageFont.ImageFont:
        size = max(6, int(size))
        if size in self._cache:
            return self._cache[size]
        font: ImageFont.ImageFont
        if self._path:
            try:
                font = ImageFont.truetype(self._path, size)
            except Exception:
                font = ImageFont.load_default()
        else:
            font = ImageFont.load_default()
        self._cache[size] = font
        return font


# --------------------------------------------------------------------------- #
# Emoji rendering (Apple Color Emoji, so emoji show in print instead of tofu)
# --------------------------------------------------------------------------- #
_APPLE_EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"
_ZWJ = 0x200D
_VS16 = 0xFE0F


def _is_emoji_start(ch: str) -> bool:
    o = ord(ch)
    return (0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
            or 0x1F1E6 <= o <= 0x1F1FF or o in (0x2122, 0x2139, 0x24C2, 0x3030,
            0x303D, 0x3297, 0x3299, 0x203C, 0x2049)
            or 0x2194 <= o <= 0x21AA or 0x231A <= o <= 0x231B
            or 0x23E9 <= o <= 0x23FA or 0x25AA <= o <= 0x25FF or 0x2934 <= o <= 0x2935)


def _is_emoji_cont(ch: str) -> bool:
    o = ord(ch)
    return (o in (_ZWJ, _VS16) or 0x1F3FB <= o <= 0x1F3FF or 0x1F1E6 <= o <= 0x1F1FF)


def _iter_units(text: str):
    """Yield ``(is_emoji, unit)``: non-emoji text per character, emoji per cluster
    (base + variation-selectors / ZWJ sequences / skin tones)."""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if _is_emoji_start(ch):
            j = i + 1
            while j < n and (_is_emoji_cont(text[j])
                             or (text[j - 1] == "‍" and _is_emoji_start(text[j]))):
                j += 1
            yield (True, text[i:j])
            i = j
        else:
            yield (False, ch)
            i += 1


class _EmojiRenderer:
    """Renders emoji clusters to RGBA glyphs via Apple Color Emoji, cached."""

    def __init__(self) -> None:
        self.font = None
        if os.path.isfile(_APPLE_EMOJI):
            for sz in (160, 137, 96, 64, 48, 40, 32, 26, 20):
                try:
                    self.font = ImageFont.truetype(_APPLE_EMOJI, sz)
                    break
                except Exception:
                    continue
        self._cache = {}

    def available(self) -> bool:
        return self.font is not None

    def glyph(self, cluster: str, target_h: int):
        if self.font is None or target_h <= 0:
            return None
        key = (cluster, target_h)
        if key in self._cache:
            return self._cache[key]
        g = None
        try:
            big = Image.new("RGBA", (360, 360), (0, 0, 0, 0))
            d = ImageDraw.Draw(big)
            try:
                d.text((4, 4), cluster, font=self.font, embedded_color=True)
            except TypeError:                       # very old PIL
                d.text((4, 4), cluster, font=self.font)
            bbox = big.getbbox()
            if bbox:
                crop = big.crop(bbox)
                w, h = crop.size
                nw = max(1, int(round(w * target_h / h)))
                try:
                    g = crop.resize((nw, target_h), Image.LANCZOS)
                except Exception:
                    g = crop.resize((nw, target_h))
        except Exception:
            g = None
        self._cache[key] = g
        return g


_EMOJI = None


def _emoji() -> _EmojiRenderer:
    global _EMOJI
    if _EMOJI is None:
        _EMOJI = _EmojiRenderer()
    return _EMOJI


def _emoji_h(font: ImageFont.ImageFont) -> int:
    return max(8, int(getattr(font, "size", 16)))


# --------------------------------------------------------------------------- #
# Text measuring / wrapping helpers
# --------------------------------------------------------------------------- #
def _text_size(draw: ImageDraw.ImageDraw, text: str,
               font: ImageFont.ImageFont) -> Tuple[int, int]:
    """Width/height of a single line of ``text`` (robust to PIL versions)."""
    if not text:
        # Still report line height so empty lines occupy a row.
        try:
            bbox = draw.textbbox((0, 0), "Ag", font=font)
            return (0, bbox[3] - bbox[1])
        except Exception:
            return (0, getattr(font, "size", 12))
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return (bbox[2] - bbox[0], bbox[3] - bbox[1])
    except Exception:
        try:
            w, h = font.getsize(text)  # very old PIL
            return (w, h)
        except Exception:
            return (len(text) * 8, getattr(font, "size", 12))


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont,
               max_w: int) -> List[str]:
    """Greedy character/word wrap to ``max_w`` pixels.

    Works for CJK (no spaces) and Latin alike by accumulating one character at a
    time and breaking when the running width would exceed ``max_w``.  Explicit
    newlines are honored.  ``max_w`` is clamped to at least one glyph so we never
    loop forever on a pathological width.
    """
    if text is None:
        text = ""
    max_w = max(max_w, 1)
    eh = _emoji_h(font)
    egap = max(1, eh // 12)
    lines: List[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line == "":
            lines.append("")
            continue
        cur = ""
        cur_w = 0
        for is_e, unit in _iter_units(raw_line):
            uw = (eh + egap) if is_e else _text_size(draw, unit, font)[0]
            if cur and cur_w + uw > max_w:
                lines.append(cur)
                cur = unit
                cur_w = uw
            else:
                cur += unit
                cur_w += uw
        lines.append(cur)
    return lines or [""]


def _line_width(draw: ImageDraw.ImageDraw, line: str,
                font: ImageFont.ImageFont) -> int:
    """Emoji-aware pixel width of a single rendered line."""
    eh = _emoji_h(font)
    egap = max(1, eh // 12)
    w = 0
    for is_e, unit in _iter_units(line or ""):
        w += (eh + egap) if is_e else _text_size(draw, unit, font)[0]
    return w


def _draw_line(img: Image.Image, draw: ImageDraw.ImageDraw, x: int, y: int,
               line: str, font: ImageFont.ImageFont, fill) -> int:
    """Draw a line mixing text (via ``font``) and color-emoji glyphs. Returns width."""
    eh = _emoji_h(font)
    egap = max(1, eh // 12)
    er = _emoji()
    cx = float(x)
    buf: List[str] = []

    def flush():
        nonlocal cx
        if buf:
            s = "".join(buf)
            draw.text((int(cx), int(y)), s, font=font, fill=fill)
            cx += _text_size(draw, s, font)[0]
            buf.clear()

    for is_e, unit in _iter_units(line or ""):
        if is_e and er.available():
            g = er.glyph(unit, eh)
            if g is not None:
                flush()
                img.paste(g, (int(cx), int(y)), g)
                cx += g.size[0] + egap
                continue
        buf.append(unit)
    flush()
    return int(cx) - int(x)


def _avatar_color(name: str) -> Tuple[int, int, int]:
    if not name:
        return _AVATAR_COLORS[0]
    return _AVATAR_COLORS[sum(bytearray(name.encode("utf-8"))) % len(_AVATAR_COLORS)]


def _load_image(path: Optional[str]) -> Optional[Image.Image]:
    """Open ``path`` as an RGB image, or return None if it isn't a usable image."""
    if not path:
        return None
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        return None
    try:
        img = Image.open(p)
        img.load()
        return img.convert("RGB")
    except Exception:
        return None


def _rounded(draw: ImageDraw.ImageDraw, box: Tuple[int, int, int, int],
             radius: int, fill=None, outline=None, width: int = 1) -> None:
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill,
                               outline=outline, width=width)
    except Exception:  # pragma: no cover - extremely old PIL
        draw.rectangle(box, fill=fill, outline=outline, width=width)


# --------------------------------------------------------------------------- #
# Layout context — one set of derived pixel metrics for the whole render.
# --------------------------------------------------------------------------- #
class _Layout:
    def __init__(self, page_w: int, page_h: int, density: str, fonts: _Fonts):
        d = _DENSITY.get(density, _DENSITY["normal"])
        self.page_w = page_w
        self.page_h = page_h
        self.fonts = fonts

        self.margin = max(8, int(page_w * d["pad"] * 4))   # outer page margin
        self.f_body = fonts.get(page_w * d["body"])
        self.f_small = fonts.get(page_w * d["small"])
        self.f_title = fonts.get(page_w * d["title"])

        self.line_mult = d["line"]
        self.msg_gap = max(2, int(page_w * d["msg_gap"]))
        self.avatar = max(14, int(page_w * d["avatar"]))
        self.pad = max(3, int(page_w * d["pad"]))           # inner bubble padding
        self.bubble_max_w = int(page_w * d["bubble_w"])
        self.img_max_w = int(page_w * d["img_w"])
        self.gutter = max(4, int(self.avatar * 0.35))       # avatar<->bubble gap

        # cache line heights
        self._bh = None
        self._sh = None

    def body_lh(self, draw) -> int:
        if self._bh is None:
            _, h = _text_size(draw, "Ag中", self.f_body)
            self._bh = int(h * self.line_mult)
        return self._bh

    def small_lh(self, draw) -> int:
        if self._sh is None:
            _, h = _text_size(draw, "Ag中", self.f_small)
            self._sh = int(h * self.line_mult)
        return self._sh

    @property
    def content_w(self) -> int:
        return self.page_w - 2 * self.margin

    @property
    def content_h(self) -> int:
        return self.page_h - 2 * self.margin


# --------------------------------------------------------------------------- #
# Page manager — owns the current canvas and the y cursor; spawns new pages.
# --------------------------------------------------------------------------- #
class _Pager:
    def __init__(self, lay: _Layout):
        self.lay = lay
        self.pages: List[Image.Image] = []
        self.img: Image.Image
        self.draw: ImageDraw.ImageDraw
        self.y = 0
        self._new_page()

    def _new_page(self) -> None:
        img = Image.new("RGB", (self.lay.page_w, self.lay.page_h), _PAGE_BG)
        self.img = img
        self.draw = ImageDraw.Draw(img)
        self.pages.append(img)
        self.y = self.lay.margin

    @property
    def bottom(self) -> int:
        return self.lay.page_h - self.lay.margin

    def remaining(self) -> int:
        return self.bottom - self.y

    def ensure(self, needed: int) -> None:
        """Start a new page if ``needed`` px won't fit (unless already at top)."""
        if self.y + needed > self.bottom and self.y > self.lay.margin:
            self._new_page()

    def finish(self) -> List[Image.Image]:
        return self.pages


# --------------------------------------------------------------------------- #
# Block drawing
# --------------------------------------------------------------------------- #
def _draw_header(pg: _Pager, bundle: models.ExportBundle) -> None:
    lay = pg.lay
    draw = pg.draw
    title = bundle.contact.display_name or "聊天记录"
    draw.text((lay.margin, pg.y), title, font=lay.f_title, fill=_TEXT)
    _, th = _text_size(draw, title, lay.f_title)
    pg.y += int(th * 1.3)

    n = len(bundle.messages)
    first = bundle.messages[0].date_key if bundle.messages else ""
    last = bundle.messages[-1].date_key if bundle.messages else ""
    rng = ""
    if first and last:
        rng = first if first == last else f"{first} ~ {last}"
    sub = f"共 {n} 条消息" + (f"  ·  {rng}" if rng else "")
    draw.text((lay.margin, pg.y), sub, font=lay.f_small, fill=_TEXT_MUTED)
    _, sh = _text_size(draw, sub, lay.f_small)
    pg.y += int(sh * 1.6)

    # thin separator rule
    draw.line([(lay.margin, pg.y), (lay.page_w - lay.margin, pg.y)],
              fill=_BUBBLE_IN_BORDER, width=1)
    pg.y += lay.msg_gap * 2


def _draw_date_pill(pg: _Pager, date_key: str) -> None:
    lay = pg.lay
    draw = pg.draw
    tw, th = _text_size(draw, date_key, lay.f_small)
    pad_x = lay.pad * 2
    pad_y = max(2, lay.pad // 2)
    pill_w = tw + 2 * pad_x
    pill_h = th + 2 * pad_y
    block = pill_h + lay.msg_gap * 2

    pg.ensure(block)
    cx = lay.page_w // 2
    x0 = cx - pill_w // 2
    y0 = pg.y + lay.msg_gap
    _rounded(draw, (x0, y0, x0 + pill_w, y0 + pill_h), radius=pill_h // 2,
             fill=_PILL_BG)
    draw.text((x0 + pad_x, y0 + pad_y), date_key, font=lay.f_small, fill=_PILL_TEXT)
    pg.y = y0 + pill_h + lay.msg_gap


def _draw_centered_line(pg: _Pager, text: str) -> None:
    """system / recall: a centered gray line, wrapped to content width."""
    lay = pg.lay
    draw = pg.draw
    lines = _wrap_text(draw, text or "", lay.f_small, lay.content_w)
    lh = lay.small_lh(draw)
    block = lh * len(lines) + lay.msg_gap
    pg.ensure(block)
    pg.y += lay.msg_gap // 2 or 1
    cx = lay.page_w // 2
    for ln in lines:
        tw = _line_width(draw, ln, lay.f_small)
        _draw_line(pg.img, draw, cx - tw // 2, pg.y, ln, lay.f_small, _TEXT_MUTED)
        pg.y += lh
    pg.y += lay.msg_gap // 2 or 1


def _scaled_image(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    scale = min(max_w / w, max_h / h, 1.0)
    if scale < 1.0:
        nw = max(1, int(w * scale))
        nh = max(1, int(h * scale))
        try:
            return img.resize((nw, nh), Image.LANCZOS)
        except Exception:
            return img.resize((nw, nh))
    return img


def _draw_avatar(pg: _Pager, x: int, y: int, bundle: models.ExportBundle,
                 msg: models.Message, name: str) -> None:
    lay = pg.lay
    draw = pg.draw
    sz = lay.avatar
    av = _load_image(bundle.avatar_src(msg))
    if av is not None:
        av = _scaled_image(av, sz, sz)
        # center-crop to square then paste
        try:
            sq = av.resize((sz, sz), Image.LANCZOS)
        except Exception:
            sq = av.resize((sz, sz))
        pg.img.paste(sq, (x, y))
        _rounded(draw, (x, y, x + sz, y + sz), radius=max(2, sz // 6),
                 outline=_BUBBLE_IN_BORDER, width=1)
        return
    # colored initial fallback
    _rounded(draw, (x, y, x + sz, y + sz), radius=max(2, sz // 6),
             fill=_avatar_color(name))
    initial = (name[:1] or "?").upper()
    tw, th = _text_size(draw, initial, lay.f_small)
    draw.text((x + (sz - tw) // 2, y + (sz - th) // 2), initial,
              font=lay.f_small, fill=_AVATAR_TEXT)


def _bubble_body(bundle: models.ExportBundle, msg: models.Message) -> Tuple[str, str]:
    """Return ``(text, label)`` for non-image bubble content.

    ``label`` is a small prefix tag for misc kinds (e.g. ``[转账]``); empty for
    plain text.  Voice/video get special handling by the caller.
    """
    kind = msg.kind
    text = msg.display_text or ""
    if kind in ("text", "quote"):
        return (text, "")
    label = _KIND_LABELS.get(kind, "")
    return (text, f"[{label}]" if label else "")


def _voice_seconds(msg: models.Message) -> Optional[int]:
    vlen = (msg.extra or {}).get("voicelength_ms")
    try:
        return max(1, int(round(int(vlen) / 1000))) if vlen else None
    except (TypeError, ValueError):
        return None


def _measure_bubble(pg: _Pager, msg: models.Message,
                    bundle: models.ExportBundle) -> Tuple[int, int, dict]:
    """Pre-compute the bubble's inner content size and a render plan.

    Returns ``(inner_w, inner_h, plan)`` where ``plan`` carries the wrapped text,
    optional scaled image, and the kind so :func:`_render_bubble` doesn't redo
    the work.  Sizes exclude the bubble padding.
    """
    lay = pg.lay
    draw = pg.draw
    kind = msg.kind
    max_text_w = lay.bubble_max_w - 2 * lay.pad
    lh = lay.body_lh(draw)
    slh = lay.small_lh(draw)

    plan = {"kind": kind, "lines": [], "img": None, "extra_lines": []}

    if kind in ("image", "sticker", "video"):
        img = _load_image(msg.media_src)
        if img is not None:
            max_h = lay.content_h - 2 * lay.pad
            img = _scaled_image(img, lay.img_max_w, max_h)
            plan["img"] = img
            return (img.size[0], img.size[1], plan)
        # placeholder text
        ph = "[图片]" if kind in ("image", "sticker") else "[视频]"
        lines = _wrap_text(draw, msg.display_text or ph, lay.f_body, max_text_w)
        plan["lines"] = lines
        w = max((_line_width(draw, l, lay.f_body) for l in lines), default=0)
        return (w, lh * len(lines), plan)

    if kind == "voice":
        secs = _voice_seconds(msg)
        head = "🔊 " + (f"{secs}″" if secs else "语音")
        plan["lines"] = _wrap_text(draw, head, lay.f_body, max_text_w)
        w = max((_line_width(draw, l, lay.f_body) for l in plan["lines"]),
                default=0)
        h = lh * len(plan["lines"])
        if msg.voice_text:
            extra = _wrap_text(draw, msg.voice_text, lay.f_small, max_text_w)
            plan["extra_lines"] = extra
            ew = max((_line_width(draw, l, lay.f_small) for l in extra),
                     default=0)
            w = max(w, ew)
            h += slh * len(extra) + max(1, slh // 3)
        return (w, h, plan)

    # text / quote / misc card
    text, label = _bubble_body(bundle, msg)
    body = text
    if label:
        body = (label + " " + text).strip() if text else label
    lines = _wrap_text(draw, body, lay.f_body, max_text_w)
    plan["lines"] = lines
    w = max((_line_width(draw, l, lay.f_body) for l in lines), default=0)
    return (w, lh * len(lines), plan)


def _render_bubble(pg: _Pager, msg: models.Message, bundle: models.ExportBundle,
                   plan: dict, bx: int, by: int, inner_w: int, inner_h: int,
                   is_out: bool) -> None:
    lay = pg.lay
    draw = pg.draw
    pad = lay.pad
    bub_w = inner_w + 2 * pad
    bub_h = inner_h + 2 * pad
    radius = max(4, pad)

    if plan["img"] is not None:
        # image bubble: thin frame, paste the picture inside the padding
        _rounded(draw, (bx, by, bx + bub_w, by + bub_h), radius=radius,
                 fill=_BUBBLE_IN, outline=_BUBBLE_IN_BORDER, width=1)
        pg.img.paste(plan["img"], (bx + pad, by + pad))
        return

    if is_out:
        _rounded(draw, (bx, by, bx + bub_w, by + bub_h), radius=radius,
                 fill=_BUBBLE_OUT)
    else:
        _rounded(draw, (bx, by, bx + bub_w, by + bub_h), radius=radius,
                 fill=_BUBBLE_IN, outline=_BUBBLE_IN_BORDER, width=1)

    tx = bx + pad
    ty = by + pad
    lh = lay.body_lh(draw)
    for ln in plan["lines"]:
        _draw_line(pg.img, draw, tx, ty, ln, lay.f_body, _TEXT)
        ty += lh
    if plan["extra_lines"]:
        ty += max(1, lay.small_lh(draw) // 3)
        slh = lay.small_lh(draw)
        for ln in plan["extra_lines"]:
            _draw_line(pg.img, draw, tx, ty, ln, lay.f_small, _TEXT_MUTED)
            ty += slh


def _draw_message(pg: _Pager, msg: models.Message,
                  bundle: models.ExportBundle) -> None:
    lay = pg.lay
    draw = pg.draw
    is_out = bool(msg.is_sender)
    name = bundle.sender_name(msg)
    show_name = (not is_out) and bundle.contact.is_group

    inner_w, inner_h, plan = _measure_bubble(pg, msg, bundle)
    # clamp a single oversized block to one page worth of height
    max_inner_h = lay.content_h - 2 * lay.pad - lay.small_lh(draw) * 2
    if inner_h > max_inner_h:
        inner_h = max_inner_h  # bubble box clamps; content may visually clip

    bub_w = inner_w + 2 * lay.pad
    bub_h = inner_h + 2 * lay.pad

    name_h = lay.small_lh(draw) if show_name else 0
    time_h = lay.small_lh(draw)
    block_h = max(lay.avatar, name_h + bub_h) + time_h + lay.msg_gap

    pg.ensure(block_h)

    top = pg.y + lay.msg_gap // 2
    av_x = lay.margin if not is_out else (lay.page_w - lay.margin - lay.avatar)
    _draw_avatar(pg, av_x, top, bundle, msg, name)

    # bubble x: leave room for avatar + gutter on the sender's side
    if is_out:
        bx = lay.page_w - lay.margin - lay.avatar - lay.gutter - bub_w
        bx = max(lay.margin, bx)
    else:
        bx = lay.margin + lay.avatar + lay.gutter

    by = top
    if show_name:
        if is_out:
            tw, _ = _text_size(draw, name, lay.f_small)
            draw.text((bx + bub_w - tw, by), name, font=lay.f_small,
                      fill=_TEXT_MUTED)
        else:
            draw.text((bx, by), name, font=lay.f_small, fill=_TEXT_MUTED)
        by += name_h

    _render_bubble(pg, msg, bundle, plan, bx, by, inner_w, inner_h, is_out)

    # timestamp under the bubble, aligned to the bubble's outer edge
    ts = msg.time_str
    tw, _ = _text_size(draw, ts, lay.f_small)
    ty = by + bub_h + 1
    if is_out:
        draw.text((bx + bub_w - tw, ty), ts, font=lay.f_small, fill=_TEXT_MUTED)
    else:
        draw.text((bx, ty), ts, font=lay.f_small, fill=_TEXT_MUTED)

    pg.y = max(top + lay.avatar, ty + time_h) + lay.msg_gap


# --------------------------------------------------------------------------- #
# Shared page renderer
# --------------------------------------------------------------------------- #
def render_pages(bundle: models.ExportBundle, dpi: int = 150,
                 density: str = "normal") -> List[Image.Image]:
    """Render ``bundle`` onto consecutive A4 portrait pages.

    Returns the list of ``PIL.Image`` pages (at least one, even when empty), so
    both the PNG and PDF entry points draw from the exact same pixels.
    """
    if density not in _DENSITY:
        density = "normal"
    page_w, page_h = _a4_px(dpi)
    fonts = _Fonts()
    lay = _Layout(page_w, page_h, density, fonts)
    pg = _Pager(lay)

    _draw_header(pg, bundle)

    last_date: Optional[str] = None
    for msg in bundle.messages:
        try:
            dk = msg.date_key
            if dk != last_date:
                _draw_date_pill(pg, dk)
                last_date = dk
            if msg.kind in ("system", "recall"):
                _draw_centered_line(pg, msg.display_text or msg.kind_label)
            else:
                _draw_message(pg, msg, bundle)
        except Exception:
            # One bad message must never abort the whole render.
            try:
                _draw_centered_line(pg, "[无法渲染的消息]")
            except Exception:
                pass

    return pg.finish()


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def export_a4_images(bundle: models.ExportBundle, out_dir: str, base_name: str,
                     dpi: int = 150, density: str = "normal") -> List[str]:
    """Render ``bundle`` to ``<base_name>_pNN.png`` files in ``out_dir``.

    Returns the absolute paths of the written PNGs, in page order.
    """
    out_dir = os.path.expanduser(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    pages = render_pages(bundle, dpi=dpi, density=density)

    paths: List[str] = []
    width = max(2, len(str(len(pages))))
    for i, page in enumerate(pages, 1):
        fn = "{0}_p{1:0{2}d}.png".format(base_name, i, width)
        path = os.path.join(out_dir, fn)
        try:
            page.save(path, "PNG", dpi=(dpi, dpi))
        except Exception:
            page.save(path, "PNG")
        paths.append(os.path.abspath(path))
    return paths


def export_pdf(bundle: models.ExportBundle, out_path: str, dpi: int = 150,
               density: str = "normal") -> str:
    """Render ``bundle`` to a single multi-page A4 PDF at ``out_path``.

    Returns the absolute output path.
    """
    out_path = os.path.expanduser(out_path)
    out_dir = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(out_dir, exist_ok=True)

    pages = render_pages(bundle, dpi=dpi, density=density)
    if not pages:  # render_pages always returns >=1, but stay defensive
        pages = [Image.new("RGB", _a4_px(dpi), _WHITE)]

    first, rest = pages[0], pages[1:]
    first.save(out_path, "PDF", resolution=float(dpi), save_all=True,
               append_images=rest)
    return os.path.abspath(out_path)
