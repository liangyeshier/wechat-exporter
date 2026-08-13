#!/usr/bin/env python3
"""Regression coverage for identity metadata, ordering, pages, and manifests."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core.models import Contact, ExportBundle, Layout, Message
from export import archive_manifest, csv_exporter, html_exporter, image_exporter, txt_exporter


def _bundle(count: int = 110) -> ExportBundle:
    owner = Contact(
        username="wxid_demo_owner",
        nickname="沈知夏（演示）",
        alias="demo_owner",
    )
    contact = Contact(
        username="design_archive@chatroom",
        nickname="产品归档讨论组（演示）",
        remark="项目资料群（演示）",
        alias="demo_archive_group",
    )
    member = Contact(
        username="wxid_demo_member",
        nickname="林小满（演示）",
        alias="demo_member",
    )
    messages = []
    base = 1_720_000_000
    for i in range(count):
        msg = Message(
            local_id=i + 1,
            server_id="demo-server-{0:04d}".format(i + 1),
            timestamp=base + i * 181,
            msg_type=1,
            is_sender=(i % 3 == 0),
            sender_username="" if i % 3 == 0 else member.username,
            kind="text",
            display_text=(
                "这是第 {0} 条完全虚构的测试消息，用于验证长会话分页、消息顺序、"
                "发送者身份和归档校验值。"
            ).format(i + 1),
        )
        if i == 1:
            msg.kind = "quote"
            msg.display_text = "收到，我会整理成归档版本。"
            msg.extra = {
                "title": "收到，我会整理成归档版本。",
                "quoted_from": "沈知夏（演示）",
                "quoted_text": "请同时保留消息顺序和页码。",
            }
        elif i == 2:
            msg.kind = "voice"
            msg.display_text = "[语音]"
            msg.extra = {"voicelength_ms": "12000"}
            msg.voice_text = "这是一段完全虚构的语音转写内容。"
        elif i == 3:
            msg.kind = "transfer"
            msg.display_text = "￥88.00 · 已收款 · 备注: 演示费用"
            msg.extra = {"amount": "￥88.00", "transfer_status": "已收款", "memo": "演示费用"}
        elif i == 4:
            msg.kind = "redpacket"
            msg.display_text = "归档测试红包"
            msg.extra = {"redpacket_greet": "归档测试红包"}
        elif i == 5:
            msg.kind = "link"
            msg.display_text = "[链接] 合成数据说明"
            msg.extra = {"title": "合成数据说明", "desc": "仅用于测试导出样式", "url": "https://example.invalid/demo"}
        elif i == 6:
            msg.kind = "file"
            msg.display_text = "[文件] 导出规则说明.pdf"
            msg.extra = {"title": "导出规则说明.pdf", "fileext": "pdf"}
        elif i == 7:
            msg.kind = "location"
            msg.display_text = "[位置] 演示地点"
            msg.extra = {"poiname": "演示地点", "label": "虚构地址，不对应真实位置"}
        elif i == 8:
            msg.kind = "card"
            msg.display_text = "[名片] 周砚（演示）"
            msg.extra = {"nickname": "周砚（演示）", "username": "wxid_demo_card"}
        elif i == 9:
            msg.kind = "system"
            msg.display_text = "你邀请了测试成员加入群聊（演示）"
        messages.append(msg)
    return ExportBundle(
        contact=contact,
        owner=owner,
        messages=messages,
        layout=Layout(
            version="v4",
            account_dir="/synthetic/not-a-real-account",
            account_id="wxid_demo_owner",
        ),
        members={member.username: member},
        generated_at="2026-08-13 13:00:00",
    )


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    bundle = _bundle()
    with tempfile.TemporaryDirectory() as out:
        html_path = html_exporter.export_html(bundle, os.path.join(out, "演示归档.html"), True)
        txt_path = txt_exporter.export_txt(bundle, os.path.join(out, "演示归档.txt"))
        csv_path = csv_exporter.export_csv(bundle, os.path.join(out, "演示归档.csv"))
        png_paths = image_exporter.export_a4_images(
            bundle, out, "演示归档", dpi=72, density="compact"
        )
        pdf_path = image_exporter.export_pdf(
            bundle, os.path.join(out, "演示归档.pdf"), dpi=72, density="compact"
        )
        manifest_paths = archive_manifest.write_archive_manifest(
            bundle, [html_path, txt_path, csv_path, pdf_path] + png_paths,
            out, "演示归档",
        )

        assert len(png_paths) >= 2, "synthetic conversation should span pages"
        total = len(png_paths)
        for number, path in enumerate(png_paths, 1):
            expected = "第{0:02d}页_共{1:02d}页.png".format(number, total)
            assert path.endswith(expected), (path, expected)
            assert os.path.getsize(path) > 0
        assert open(pdf_path, "rb").read(5) == b"%PDF-"

        html = open(html_path, encoding="utf-8").read()
        time_tags = re.findall(r'<time[^>]+datetime="([^"]+)"[^>]*>([^<]+)</time>', html)
        assert len(time_tags) == 110, len(time_tags)
        assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", iso)
                   for iso, _ in time_tags)
        assert all(re.fullmatch(r"\d{4}年\d{1,2}月\d{1,2}日 \d{2}:\d{2}:\d{2}", visible)
                   for _, visible in time_tags)
        for value in (
            "项目资料群（演示）", "产品归档讨论组（演示）", "demo_archive_group",
            "design_archive@chatroom", "沈知夏（演示）", "demo_owner",
            "wxid_demo_owner",
        ):
            assert value in html, value
        assert "#000001" not in html
        assert "#000110" not in html
        assert "第 1 页" not in html, "HTML is continuous, not paginated"

        txt = open(txt_path, encoding="utf-8").read()
        assert "会话微信号: demo_archive_group" in txt
        assert "导出账号微信号: demo_owner" in txt
        assert "[#000110 " in txt
        assert re.search(r"\[#000001 \d{4}年\d{1,2}月\d{1,2}日 \d{2}:\d{2}:\d{2}\]", txt)

        with open(csv_path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 110
        assert rows[0]["消息序号"] == "1"
        assert rows[-1]["消息序号"] == "110"
        assert rows[0]["会话内部标识"] == "design_archive@chatroom"
        assert rows[0]["导出账号微信号"] == "demo_owner"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", rows[0]["完整本地时间"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", rows[0]["ISO8601时间"])
        assert re.fullmatch(r"[+-]\d{2}:\d{2}", rows[0]["UTC偏移"])

        json_path, guide_path = manifest_paths
        manifest = json.load(open(json_path, encoding="utf-8"))
        assert manifest["schema"] == archive_manifest.SCHEMA_VERSION
        assert manifest["scope"]["message_count"] == 110
        assert len(manifest["message_records"]) == 110
        assert manifest["message_records"][0]["sequence"] == 1
        assert manifest["message_records"][-1]["sequence"] == 110
        assert all(row["timestamp_iso8601"] and row["timezone_utc_offset"]
                   for row in manifest["message_records"])
        assert len(manifest["message_chain_head_sha256"]) == 64
        file_map = {item["path"]: item for item in manifest["files"]}
        for artifact in [html_path, txt_path, csv_path, pdf_path] + png_paths:
            rel = os.path.relpath(artifact, out).replace(os.sep, "/")
            assert file_map[rel]["sha256"] == _sha256(artifact)
        guide = open(guide_path, encoding="utf-8").read()
        assert "不构成电子签名、可信时间戳、公证或对司法采信结果的保证" in guide

        # Pillow's PDF encoder writes one /Type /Page object per rendered page,
        # plus a /Pages tree object. This verifies PDF and PNG use the same pages.
        pdf = open(pdf_path, "rb").read()
        pdf_pages = len(re.findall(rb"/Type\s*/Page(?!s)", pdf))
        assert pdf_pages == total, (pdf_pages, total)

    print("EXPORT ARCHIVE TESTS PASSED")


if __name__ == "__main__":
    main()
