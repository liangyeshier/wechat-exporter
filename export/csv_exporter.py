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
    "消息序号",
    "本地消息ID",
    "服务端消息ID",
    "Unix时间",
    "日期",
    "时间",
    "会话名称",
    "会话备注",
    "会话昵称",
    "会话微信号",
    "会话内部标识",
    "导出账号名称",
    "导出账号备注",
    "导出账号昵称",
    "导出账号微信号",
    "导出账号内部标识",
    "发送者",
    "发送者内部标识",
    "是否本人",
    "类型",
    "内容",
    "语音转写",
    "媒体文件",
]


def _row(bundle: ExportBundle, msg: Message, sequence: int) -> Dict[str, Any]:
    """Render one message into a column-keyed row dict."""
    return {
        "消息序号": sequence,
        "本地消息ID": msg.local_id,
        "服务端消息ID": msg.server_id or "",
        "Unix时间": int(msg.timestamp),
        "日期": msg.date_key,
        "时间": msg.time_str,
        "会话名称": bundle.contact.display_name,
        "会话备注": bundle.contact.remark or "",
        "会话昵称": bundle.contact.nickname or "",
        "会话微信号": bundle.contact.alias or "",
        "会话内部标识": bundle.contact.username,
        "导出账号名称": bundle.owner.display_name,
        "导出账号备注": bundle.owner.remark or "",
        "导出账号昵称": bundle.owner.nickname or "",
        "导出账号微信号": bundle.owner.alias or "",
        "导出账号内部标识": bundle.owner.username,
        "发送者": bundle.sender_name(msg),
        "发送者内部标识": bundle.sender_key(msg),
        "是否本人": "是" if msg.is_sender else "否",
        "类型": msg.kind_label,
        "内容": msg.display_text,
        "语音转写": msg.voice_text or "",
        "媒体文件": msg.media_out or msg.media_src or "",
    }


def export_csv(bundle: ExportBundle, out_path: str) -> str:
    """Write ``bundle`` as a CSV table to ``out_path`` and return that path."""
    rows: List[Dict[str, Any]] = [
        _row(bundle, m, sequence) for sequence, m in enumerate(bundle.messages, 1)
    ]

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
