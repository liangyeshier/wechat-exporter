"""Shared identity metadata and integrity manifests for exported conversations.

The manifest is not a digital signature and does not decide legal admissibility.
It records the export scope, a deterministic hash chain over the selected
messages, and SHA-256 hashes for every generated artifact so later changes are
detectable.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from core.models import Contact, ExportBundle, Message


SCHEMA_VERSION = "wechat-export-archive/v1"


def contact_identity(contact: Contact) -> Dict[str, str]:
    """Return the user-visible and source identifiers for one WeChat contact."""
    return {
        "display_name": contact.display_name or "",
        "remark": contact.remark or "",
        "nickname": contact.nickname or "",
        "wechat_id": contact.alias or "",
        "internal_id": contact.username or "",
        "type": "group" if contact.is_group else (
            "official" if contact.is_official else "contact"
        ),
    }


def identity_lines(contact: Contact, role: str) -> List[Tuple[str, str]]:
    """Compact non-empty identity rows shared by human-readable exporters."""
    ident = contact_identity(contact)
    rows = [(role + "名称", ident["display_name"])]
    if ident["remark"] and ident["remark"] != ident["display_name"]:
        rows.append((role + "备注", ident["remark"]))
    if ident["nickname"] and ident["nickname"] != ident["display_name"]:
        rows.append((role + "昵称", ident["nickname"]))
    if ident["wechat_id"]:
        rows.append((role + "微信号", ident["wechat_id"]))
    if ident["internal_id"]:
        rows.append((role + "内部标识", ident["internal_id"]))
    return rows


def date_range(bundle: ExportBundle) -> Tuple[str, str]:
    start = bundle.start_date or (bundle.messages[0].date_key if bundle.messages else "")
    end = bundle.end_date or (bundle.messages[-1].date_key if bundle.messages else "")
    return start, end


def _message_payload(bundle: ExportBundle, msg: Message, sequence: int) -> Dict[str, Any]:
    raw_sha256 = hashlib.sha256(
        (msg.raw_content or "").encode("utf-8", errors="replace")
    ).hexdigest()
    return {
        "sequence": sequence,
        "local_id": msg.local_id,
        "server_id": msg.server_id or "",
        "timestamp_unix": int(msg.timestamp),
        "timestamp_local": msg.datetime_str,
        "timestamp_iso8601": msg.iso_time_str,
        "timezone_utc_offset": msg.timezone_offset_str,
        "message_type": int(msg.msg_type),
        "message_subtype": int(msg.sub_type),
        "status": int(msg.status),
        "raw_content_sha256": raw_sha256,
        "sender_internal_id": bundle.sender_key(msg),
        "sender_name": bundle.sender_name(msg),
        "is_owner": bool(msg.is_sender),
        "kind": msg.kind,
        "text": msg.display_text or "",
        "voice_text": msg.voice_text or "",
    }


def message_hash_chain(bundle: ExportBundle) -> Tuple[List[Dict[str, Any]], str]:
    """Build an ordered SHA-256 chain without duplicating message bodies in output."""
    previous = "0" * 64
    rows: List[Dict[str, Any]] = []
    for sequence, msg in enumerate(bundle.messages, 1):
        payload = _message_payload(bundle, msg, sequence)
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        digest = hashlib.sha256(previous.encode("ascii") + b"\n" + canonical).hexdigest()
        rows.append({
            "sequence": sequence,
            "local_id": msg.local_id,
            "server_id": msg.server_id or "",
            "timestamp_unix": int(msg.timestamp),
            "timestamp_local": msg.datetime_str,
            "timestamp_iso8601": msg.iso_time_str,
            "timezone_utc_offset": msg.timezone_offset_str,
            "message_type": int(msg.msg_type),
            "message_subtype": int(msg.sub_type),
            "raw_content_sha256": payload["raw_content_sha256"],
            "record_sha256": digest,
        })
        previous = digest
    return rows, previous


def archive_metadata(bundle: ExportBundle) -> Dict[str, Any]:
    chain, chain_head = message_hash_chain(bundle)
    start, end = date_range(bundle)
    identity = {
        "account": contact_identity(bundle.owner),
        "conversation": contact_identity(bundle.contact),
        "source_account_id": bundle.layout.account_id,
        "source_layout": bundle.layout.version,
    }
    scope = {
        "start_date": start,
        "end_date": end,
        "message_count": len(bundle.messages),
        "first_timestamp_unix": int(bundle.messages[0].timestamp) if bundle.messages else None,
        "last_timestamp_unix": int(bundle.messages[-1].timestamp) if bundle.messages else None,
        "first_timestamp_iso8601": bundle.messages[0].iso_time_str if bundle.messages else None,
        "last_timestamp_iso8601": bundle.messages[-1].iso_time_str if bundle.messages else None,
    }
    archive_seed = {
        "schema": SCHEMA_VERSION,
        "identity": identity,
        "scope": scope,
        "message_chain_head_sha256": chain_head,
    }
    archive_id = hashlib.sha256(json.dumps(
        archive_seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return {
        "schema": SCHEMA_VERSION,
        "archive_id_sha256": archive_id,
        "archive_id_short": archive_id[:16],
        "generated_at_local": bundle.generated_at or "",
        "identity": identity,
        "scope": scope,
        "message_chain_algorithm": "SHA-256(previous_record_hash + canonical_message)",
        "message_chain_head_sha256": chain_head,
        "message_records": chain,
    }


def message_sequence_map(bundle: ExportBundle) -> Dict[int, int]:
    """Map object identity to a 1-based sequence, preserving duplicate local ids."""
    return {id(msg): sequence for sequence, msg in enumerate(bundle.messages, 1)}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_files(paths: Iterable[str]) -> List[str]:
    files = set()
    for raw in paths:
        path = os.path.abspath(os.path.expanduser(raw))
        if os.path.isfile(path):
            files.add(path)
            if path.lower().endswith(".html"):
                assets = os.path.splitext(path)[0] + "_assets"
                if os.path.isdir(assets):
                    for root, _, names in os.walk(assets):
                        for name in names:
                            candidate = os.path.join(root, name)
                            if os.path.isfile(candidate):
                                files.add(candidate)
        elif os.path.isdir(path):
            for root, _, names in os.walk(path):
                for name in names:
                    candidate = os.path.join(root, name)
                    if os.path.isfile(candidate):
                        files.add(candidate)
    return sorted(files)


def write_archive_manifest(
    bundle: ExportBundle,
    artifact_paths: Sequence[str],
    out_dir: str,
    base_name: str,
) -> List[str]:
    """Write machine-readable JSON plus a concise human verification guide."""
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    os.makedirs(out_dir, exist_ok=True)
    metadata = archive_metadata(bundle)
    files = []
    for path in _artifact_files(artifact_paths):
        files.append({
            "path": os.path.relpath(path, out_dir).replace(os.sep, "/"),
            "size_bytes": os.path.getsize(path),
            "sha256": _sha256_file(path),
        })

    manifest = dict(metadata)
    manifest.update({
        "files": files,
        "ordering_rules": [
            "Messages retain database query order and receive 1-based sequence numbers.",
            "A4 PNG filenames and page footers use the same 1-based page order.",
            "PDF pages are generated from the same ordered page images.",
            "Any content or artifact change causes the corresponding SHA-256 check to fail.",
        ],
        "verification_notice": (
            "This archive records provenance and detects later modification. It is not a "
            "digital signature, trusted timestamp, notarization, or guarantee of legal admissibility."
        ),
    })

    json_path = os.path.join(out_dir, base_name + "_归档校验.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    json_hash = _sha256_file(json_path)
    start, end = date_range(bundle)
    lines = [
        "微信聊天记录导出归档说明",
        "=" * 30,
    ]
    for label, value in identity_lines(bundle.contact, "会话"):
        lines.append(label + ": " + value)
    for label, value in identity_lines(bundle.owner, "导出账号"):
        lines.append(label + ": " + value)
    lines.extend([
        "导出范围: {0} ~ {1}".format(start or "-", end or "-"),
        "消息数量: " + str(len(bundle.messages)),
        "生成时间（本机）: " + (bundle.generated_at or "-"),
        "归档编号（SHA-256）: " + metadata["archive_id_sha256"],
        "消息链末值（SHA-256）: " + metadata["message_chain_head_sha256"],
        "",
        "文件校验值",
        "-" * 30,
    ])
    for item in files:
        lines.append("{sha256}  {path}".format(**item))
    lines.extend([
        json_hash + "  " + os.path.basename(json_path),
        "",
        "校验方法: macOS 可在此目录运行 shasum -a 256 <文件名>，并与以上值比较。",
        "说明: 本清单用于记录导出范围、顺序并发现导出后的文件变更。",
        "它不构成电子签名、可信时间戳、公证或对司法采信结果的保证。",
        "提交材料时应保留原设备、原始数据库、完整导出目录及获取过程记录。",
    ])
    txt_path = os.path.join(out_dir, base_name + "_归档说明.txt")
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return [json_path, txt_path]
