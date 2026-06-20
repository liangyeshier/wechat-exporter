"""CSV exporter — flatten an ``ExportBundle`` into a spreadsheet-friendly table.

Columns are Chinese-headered and the file is written as UTF-8 *with BOM*
(``utf-8-sig``) so Microsoft Excel renders CJK text without mojibake. Pandas is
used when available for convenience; otherwise we fall back to the stdlib ``csv``
module producing byte-identical columns.
"""
from __future__ import annotations

from typing import Any, Dict, List

from core.models import ExportBundle, Message

# Column order is the contract — keep these two lists in lock-step.
_COLUMNS = [
    "日期",
    "时间",
    "发送者",
    "是否本人",
    "类型",
    "内容",
    "语音转写",
    "媒体文件",
]


def _row(bundle: ExportBundle, msg: Message) -> Dict[str, Any]:
    """Render one message into a column-keyed row dict."""
    return {
        "日期": msg.date_key,
        "时间": msg.time_str,
        "发送者": bundle.sender_name(msg),
        "是否本人": "是" if msg.is_sender else "否",
        "类型": msg.kind_label,
        "内容": msg.display_text,
        "语音转写": msg.voice_text or "",
        "媒体文件": msg.media_out or msg.media_src or "",
    }


def export_csv(bundle: ExportBundle, out_path: str) -> str:
    """Write ``bundle`` as a CSV table to ``out_path`` and return that path."""
    rows: List[Dict[str, Any]] = [_row(bundle, m) for m in bundle.messages]

    try:
        import pandas as pd  # lazy: pandas is heavy and optional at runtime
    except ImportError:
        return _export_csv_stdlib(rows, out_path)

    # Reindex guarantees column order/presence even when rows is empty.
    df = pd.DataFrame(rows, columns=_COLUMNS)
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return out_path


def _export_csv_stdlib(rows: List[Dict[str, Any]], out_path: str) -> str:
    """Pandas-free fallback writing the exact same columns (utf-8-sig)."""
    import csv

    # newline="" per the csv module docs to avoid blank lines on some platforms.
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return out_path
