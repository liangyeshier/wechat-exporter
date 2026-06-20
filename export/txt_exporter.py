"""Plain-text exporter — a human-readable chat transcript.

The output opens with a header block (title, owner, date range, count), then
lists messages grouped by date under ``===== YYYY-MM-DD =====`` separators.
Each line reads ``[HH:MM:SS] <发送者>: <内容>``; voice transcriptions are
appended inline and media-only messages surface their kind label. Written as
plain UTF-8 (no BOM) — this is a transcript, not a spreadsheet.
"""
from __future__ import annotations

from typing import List

from core.models import ExportBundle, Message


def _body(msg: Message) -> str:
    """The per-message body text after the timestamp/sender prefix."""
    text = msg.display_text or ""
    if msg.kind == "voice":
        # Prefer the transcription; fall back to the [语音] label.
        if msg.voice_text:
            base = text or ("[" + msg.kind_label + "]")
            return base + "  (语音转写: " + msg.voice_text + ")"
        return text or ("[" + msg.kind_label + "]")
    if not text:
        # Media-only / non-text message with no rendered text: show the kind.
        return "[" + msg.kind_label + "]"
    return text


def _header(bundle: ExportBundle) -> List[str]:
    """Build the leading metadata block lines."""
    messages = bundle.messages
    title = bundle.contact.display_name
    owner = bundle.owner.display_name or "我"

    # Date range: prefer the explicit filter, else derive from the messages.
    start = bundle.start_date
    end = bundle.end_date
    if not start and messages:
        start = messages[0].date_key
    if not end and messages:
        end = messages[-1].date_key
    date_range = "{0} ~ {1}".format(start or "-", end or "-")

    lines = [
        "会话: " + title,
        "本人: " + owner,
        "日期范围: " + date_range,
        "消息总数: " + str(len(messages)),
    ]
    if bundle.generated_at:
        lines.append("导出时间: " + bundle.generated_at)
    lines.append("")
    return lines


def export_txt(bundle: ExportBundle, out_path: str) -> str:
    """Write ``bundle`` as a readable transcript to ``out_path``; return it."""
    lines: List[str] = _header(bundle)

    current_date = None
    for msg in bundle.messages:
        if msg.date_key != current_date:
            current_date = msg.date_key
            lines.append("===== " + current_date + " =====")
        prefix = "[{0}] {1}: ".format(msg.time_str, bundle.sender_name(msg))
        lines.append(prefix + _body(msg))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")
    return out_path
