#!/usr/bin/env python3
"""wechat-exporter — 导出你本人微信账号在本机的聊天记录 (macOS).

只读、本地处理；语音转写完全离线。仅用于导出自己账号的聊天记录。

交互式运行:   python main.py
带参数运行:   python main.py --contact 张三 --start 2024-01-01 --format html --voice

流程: 检测账号 → 提取密钥 → 解密数据库 → 选择联系人 → 设置范围/格式 → 导出。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

import click

try:
    from rich.console import Console
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 rich。请先 pip install -r requirements.txt\n")
    raise

from core import constants, locator, db_decryptor, db_reader, message_parser
from core import key_extractor
from core.models import Contact, ExportBundle, Layout, Message

console = Console()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _step(n: int, total: int, title: str) -> None:
    console.print(f"\n[bold cyan][{n}/{total}][/] {title}")


def _ok(msg: str) -> None:
    console.print(f"      [green]✓[/] {msg}")


def _warn(msg: str) -> None:
    console.print(f"      [yellow]![/] {msg}")


def _fail(msg: str) -> None:
    console.print(f"      [red]✗[/] {msg}")


def _parse_date(s: Optional[str], end: bool = False) -> Optional[int]:
    """'YYYY-MM-DD' -> unix seconds (local). end=True => end of that day."""
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    dt = datetime.strptime(s, "%Y-%m-%d")
    if end:
        dt = dt.replace(hour=23, minute=59, second=59)
    return int(dt.timestamp())


def _progress():
    return Progress(
        TextColumn("      [progress.description]{task.description}"),
        BarColumn(bar_width=30),
        TextColumn("{task.completed}/{task.total}"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )


def _safe_name(name: str) -> str:
    bad = '/\\:*?"<>|'
    return "".join("_" if c in bad else c for c in name).strip() or "chat"


# --------------------------------------------------------------------------- #
# --detect : read-only environment diagnostic (no decrypt, no export)
# --------------------------------------------------------------------------- #
def _check_deps():
    """Return [(module, importable, required, description)] for a quick report."""
    deps = [
        ("sqlcipher3", True, "解密数据库（必需）"),
        ("zstandard", True, "v4 正文解压（必需）"),
        ("jinja2", True, "HTML 导出"),
        ("click", True, "命令行"),
        ("rich", True, "终端界面"),
        ("pandas", False, "CSV 导出（可选，缺失则降级为标准库）"),
        ("pysilk", False, "语音 SILK 解码（可选，--voice 需要）"),
        ("faster_whisper", False, "语音转写引擎（可选，--voice 需要）"),
        ("frida", False, "frida 密钥后端（可选，默认用 memscan 不需要）"),
    ]
    rows = []
    for mod, required, desc in deps:
        try:
            __import__(mod)
            ok = True
        except Exception:
            ok = False
        rows.append((mod, ok, required, desc))
    return rows


def run_detect() -> int:
    """Print a read-only diagnostic of the WeChat data / key / deps on this Mac."""
    console.rule("[bold]微信导出工具 · 环境诊断 (--detect)")
    console.print("[dim]全程只读：仅检测，不解密、不导出、不修改任何数据[/]\n")

    # 1) 容器与账号 ----------------------------------------------------------
    console.print("[bold cyan]1) 微信数据目录[/]")
    if not locator.container_exists():
        _fail("未找到微信容器: " + os.path.expanduser(constants.CONTAINER))
        _warn("可能原因: 这台 Mac 未安装/登录过微信，或终端没有『完全磁盘访问权限』。")
        _warn("请到『系统设置 › 隐私与安全性 › 完全磁盘访问』添加你的终端并重启终端。")
    else:
        _ok("容器存在: " + os.path.expanduser(constants.CONTAINER))

    try:
        accounts = locator.discover_accounts()
    except Exception as e:  # pragma: no cover
        accounts = []
        _fail("扫描账号时出错: %s" % e)

    if accounts:
        _ok("发现 %d 个账号:" % len(accounts))
        table = Table(show_header=True, header_style="bold")
        table.add_column("账号 id")
        table.add_column("版本")
        table.add_column("消息库", justify="right")
        table.add_column("联系人库")
        table.add_column("会话库")
        table.add_column("媒体目录")
        for a in accounts:
            table.add_row(
                a.account_id, a.version, str(len(a.message_dbs)),
                "有" if a.contact_db else "无",
                "有" if a.session_db else "无",
                "有" if a.media_root else "无",
            )
        console.print(table)
        for a in accounts:
            console.print("      [dim]%s → %s[/]" % (a.account_id, a.account_dir))
    else:
        _warn("未发现任何账号的消息数据库。")
        _warn("  v4 路径: " + os.path.expanduser(constants.V4_ROOT))
        _warn("  v3 路径: " + os.path.expanduser(constants.V3_ROOT))

    # 2) 微信进程 ------------------------------------------------------------
    console.print("\n[bold cyan]2) 微信进程[/]")
    pid = key_extractor.find_wechat_pid()
    if pid:
        _ok("微信正在运行 (pid %d) —— 可以提取密钥" % pid)
    else:
        _warn("未检测到微信进程。提取密钥时必须先启动并登录微信（密钥只在内存里）。")

    # 3) 密钥状态 ------------------------------------------------------------
    console.print("\n[bold cyan]3) 密钥[/]")
    cached = key_extractor.load_cached_keys()
    if cached:
        _ok("已有缓存密钥 %d 条: %s" % (len(cached), os.path.expanduser(constants.KEY_CACHE)))
    else:
        _warn("没有缓存密钥（首次运行需要提取，或用 --key 手动传入）。")

    if pid:
        console.print("      正在尝试内存扫描（不写缓存，仅测试是否可读）...")
        try:
            found = key_extractor.extract_via_memscan(pid)
            if found:
                _ok("内存扫描成功，找到 %d 个密钥候选 ✓（说明权限/签名已就绪）" % len(found))
            else:
                _warn("可读取进程内存，但未匹配到密钥特征（可能微信尚未打开任何聊天/数据库）。")
                _warn("  建议: 在微信里随便点开一个聊天，再运行一次。")
        except key_extractor.KeyExtractionError as e:
            _warn("暂时无法读取微信进程内存：")
            for line in str(e).splitlines():
                console.print("        [yellow]%s[/]" % line)
        except Exception as e:  # pragma: no cover
            _warn("内存扫描异常: %s" % e)

    # 4) 依赖 ----------------------------------------------------------------
    console.print("\n[bold cyan]4) Python 依赖[/]")
    dep_table = Table(show_header=True, header_style="bold")
    dep_table.add_column("包")
    dep_table.add_column("状态")
    dep_table.add_column("必需")
    dep_table.add_column("用途")
    missing_required = []
    for mod, ok, required, desc in _check_deps():
        status = "[green]已安装[/]" if ok else "[red]缺失[/]"
        dep_table.add_row(mod, status, "是" if required else "否", desc)
        if required and not ok:
            missing_required.append(mod)
    console.print(dep_table)
    if missing_required:
        _fail("缺少必需依赖: %s" % ", ".join(missing_required))
        _warn("请运行: pip install -r requirements.txt")

    # 结论 -------------------------------------------------------------------
    console.print("\n[bold cyan]结论[/]")
    if accounts and not missing_required:
        ready = bool(cached) or bool(pid)
        console.print("      数据目录与依赖就绪。" +
                      ("可以直接运行导出。" if ready else "请先启动微信以便提取密钥，或用 --key 手动传入。"))
        console.print("      下一步: [bold]sudo .venv/bin/python main.py[/]  （或带 --account/--contact 等参数）")
    elif not accounts:
        console.print("      [yellow]主要问题: 未找到微信数据。先确认已登录微信，并给终端开启完全磁盘访问。[/]")
    return 0


def _chown_back(paths) -> None:
    """When running as root via sudo, hand created files back to the real user.

    Avoids leaving root-owned files in ~/.wechat_exporter (which would block a
    later non-sudo run) or on the Desktop.
    """
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    sudo_user = os.environ.get("SUDO_USER")
    if not sudo_user:
        return
    try:
        import pwd
        pw = pwd.getpwnam(sudo_user)
    except (KeyError, ImportError):
        return
    uid, gid = pw.pw_uid, pw.pw_gid

    def ch(p):
        try:
            os.chown(p, uid, gid)
        except OSError:
            pass

    for p in paths:
        if p and os.path.isdir(p):
            for base, dirs, files in os.walk(p):
                ch(base)
                for n in dirs + files:
                    ch(os.path.join(base, n))
        elif p and os.path.exists(p):
            ch(p)


def run_extract_key(method: str, manual) -> int:
    """Extract the key(s) once (needs sudo + re-signed WeChat), cache, and exit."""
    console.rule("[bold]提取并缓存微信密钥")
    console.print("[dim]只把密钥写入本地缓存，不解密、不导出。之后导出就不需要 sudo 了。[/]\n")
    pid = key_extractor.find_wechat_pid()
    if pid:
        console.print("      微信进程 pid %d" % pid)
    else:
        _warn("未检测到微信进程；memscan 需要微信处于运行状态（密钥只在内存里）。")
    try:
        keys = key_extractor.get_keys(method=method, manual=manual,
                                      use_cache=True, save=True)
    except key_extractor.KeyExtractionError as e:
        _fail(str(e))
        return 2
    _chown_back([constants.APP_DIR])
    _ok("已提取并缓存 %d 个数据库密钥 → %s" % (len(keys), constants.KEY_CACHE))

    # Also grab the image (.dat V2) AES key from memory while we're here — it is
    # only resident while WeChat is rendering images, so open a chat with photos.
    try:
        from core import image_dat
        layout = locator.select_account()
        if layout.is_v4:
            # macOS image key is DERIVED offline from uin+wxid (no memory scan).
            ik, xb = image_dat.derive_image_key(layout)
            if ik:
                d = key_extractor.load_cached_keys()
                d["_image_aes"] = ik
                if xb is not None:
                    d["_image_xor"] = str(xb)
                key_extractor.save_keys(d)
                _chown_back([constants.APP_DIR])
                _ok("已推导并缓存图片密钥（图片将可还原）✓")
            else:
                _warn("暂未推导出图片密钥（缺少 uin/kvcomm 信息）；导出图片时会再次尝试。")
    except Exception as e:  # never let the image step break key extraction
        _warn("图片密钥步骤已跳过: %s" % e)

    console.print("      接下来可[bold]不用 sudo[/]直接导出: [bold].venv/bin/python main.py[/]")
    return 0


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def stage_keys(method: str, manual: Optional[str]) -> Dict[str, str]:
    if find := key_extractor.find_wechat_pid():
        console.print(f"      检测到微信进程 (pid {find})")
    keys = key_extractor.get_keys(method=method, manual=manual, use_cache=True, save=True)
    return keys


def stage_decrypt(layout: Layout, keys: Dict[str, str]) -> Dict[str, str]:
    sources = list(layout.message_dbs)
    for extra in (layout.contact_db, layout.session_db, layout.group_db):
        if extra:
            sources.append(extra)
    decrypted: Dict[str, str] = {}
    with _progress() as p:
        task = p.add_task("解密数据库", total=len(sources))

        def cb(done, total, label):
            p.update(task, completed=done)

        decrypted = db_decryptor.decrypt_layout(layout, keys, progress=cb)
        p.update(task, completed=len(sources))
    return decrypted


def stage_select_contact(decrypted: Dict[str, str], layout: Layout,
                         preset: Optional[str], assume_yes: bool) -> Contact:
    contacts = db_reader.read_contacts(decrypted, layout)
    index = db_reader.build_table_index(
        [decrypted.get(db, db) for db in layout.message_dbs]
    )
    if not contacts:
        _warn("联系人表为空，改用消息库中实际存在的会话。")
        contacts = {}

    # only keep contacts that actually have a chat table (have messages)
    def has_table(c: Contact) -> bool:
        return c.chat_table(layout.version).lower() in index

    # Note: conversations whose contact was deleted appear in the message DBs as
    # Chat_/Msg_<md5> tables we can't reverse to a username, so they aren't
    # listed here. Everything in the contact table that still has messages is.
    while True:
        kw = preset
        if kw is None:
            kw = Prompt.ask("      输入关键词搜索 (昵称/备注/微信号，留空列出全部)",
                            default="", console=console)
        kw = (kw or "").strip().lower()
        matched = [c for c in contacts.values()
                   if (not kw or kw in c.search_blob()) and has_table(c)]
        matched.sort(key=lambda c: c.display_name)
        if not matched:
            _warn("没有匹配的联系人，请重试。")
            preset = None
            continue

        # count messages for the (filtered) candidates
        table = Table(show_header=True, header_style="bold")
        table.add_column("序号", justify="right")
        table.add_column("名称")
        table.add_column("微信号/标识")
        table.add_column("消息数", justify="right")
        rows: List[Contact] = []
        for i, c in enumerate(matched[:50], 1):
            hit = db_reader.find_chat_table(index, c, layout.version)
            cnt = db_reader.count_messages(hit[0], hit[1], version=layout.version) if hit else 0
            c.message_count = cnt
            rows.append(c)
            table.add_row(str(i), c.display_name, c.alias or c.username, f"{cnt:,}")
        console.print(table)
        if len(matched) > 50:
            _warn(f"仅显示前 50 / {len(matched)} 条，输入更精确的关键词缩小范围。")

        if assume_yes and rows:
            return rows[0]
        sel = Prompt.ask("      请输入序号 (或回车重新搜索)", default="", console=console)
        if sel.strip().isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(rows):
                return rows[idx]
        preset = None


def stage_export(bundle: ExportBundle, fmt: str, out_dir: str,
                 a4_density: str = "compact", embed_media: bool = False,
                 write_manifest: bool = True) -> List[str]:
    from export import archive_manifest, html_exporter, csv_exporter, txt_exporter

    # Expand ~ against the REAL home (sudo-safe), not /var/root.
    if out_dir.startswith("~"):
        out_dir = constants.HOME + out_dir[1:]
    out_dir = os.path.expanduser(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    rng = ""
    if bundle.start_date or bundle.end_date:
        rng = "_%s-%s" % (bundle.start_date or "", bundle.end_date or "今天")
    base = "微信导出_%s%s" % (_safe_name(bundle.contact.display_name), rng)

    targets = []
    fmts = ["html", "csv", "txt", "pdf"] if fmt == "all" else [fmt]
    for f in fmts:
        if f == "html":
            targets.append(html_exporter.export_html(
                bundle, os.path.join(out_dir, base + ".html"), embed_media=embed_media))
        elif f == "csv":
            targets.append(csv_exporter.export_csv(bundle, os.path.join(out_dir, base + ".csv")))
        elif f == "txt":
            targets.append(txt_exporter.export_txt(bundle, os.path.join(out_dir, base + ".txt")))
        elif f == "pdf":
            try:
                from export import image_exporter
                targets.append(image_exporter.export_pdf(
                    bundle, os.path.join(out_dir, base + ".pdf"), density=a4_density))
            except ImportError:
                _warn("PDF 导出需要 pillow：pip install pillow")
        elif f in ("a4", "png", "image"):
            try:
                from export import image_exporter
                targets += image_exporter.export_a4_images(bundle, out_dir, base, density=a4_density)
            except ImportError:
                _warn("A4 图片导出需要 pillow：pip install pillow")
    if targets and write_manifest:
        targets += archive_manifest.write_archive_manifest(
            bundle, targets, out_dir, base
        )
    return targets


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
@click.command(context_settings=dict(help_option_names=["-h", "--help"]))
@click.option("--detect", is_flag=True, help="只检测环境(版本/账号/密钥/依赖)，不解密不导出")
@click.option("--extract-key", "extract_key", is_flag=True,
              help="仅提取并缓存密钥(需 sudo + 已重签名微信)后退出")
@click.option("--account", default=None, help="账号 id 片段 (多账号时指定)")
@click.option("--key", "manual_key", default=None,
              help="手动指定密钥 (64位hex) 或 keys.json 路径，跳过自动提取")
@click.option("--key-method", default="auto",
              type=click.Choice(["auto", "manual", "memscan", "frida", "cache"]),
              help="密钥提取方式 (默认 auto)")
@click.option("--contact", "contact_kw", default=None, help="预选联系人关键词")
@click.option("--start", default=None, help="开始日期 YYYY-MM-DD")
@click.option("--end", default=None, help="结束日期 YYYY-MM-DD")
@click.option("--voice/--no-voice", "do_voice", default=None, help="是否语音转文字")
@click.option("--whisper-engine", default="faster",
              type=click.Choice(["faster", "openai"]))
@click.option("--whisper-model", default="base")
@click.option("--format", "fmt", default=None,
              type=click.Choice(["html", "csv", "txt", "pdf", "a4", "all"]),
              help="导出格式 (a4=A4纸打印用的PNG图片; all=html+csv+txt+pdf)")
@click.option("--a4-density", default="compact",
              type=click.Choice(["compact", "normal", "large"]),
              help="A4/PDF 文字密度 (compact=最小字、每页最多)")
@click.option("--self-contained", is_flag=True,
              help="HTML 把图片/头像/语音内嵌成单一文件 (可直接分享/下载，文件较大)")
@click.option("--output-dir", default="~/Desktop", help="导出目录")
@click.option("--keep-decrypted", is_flag=True, help="保留解密后的临时数据库")
@click.option("--flip-sender", is_flag=True, help="v3: 收/发方向相反时翻转")
@click.option("--yes", "assume_yes", is_flag=True, help="非交互，使用默认值")
def main(detect, extract_key, account, manual_key, key_method, contact_kw, start,
         end, do_voice, whisper_engine, whisper_model, fmt, a4_density,
         self_contained, output_dir, keep_decrypted, flip_sender, assume_yes):
    """导出微信聊天记录。"""
    if detect:
        sys.exit(run_detect())
    if extract_key:
        method = "manual" if (manual_key and key_method == "auto") else key_method
        sys.exit(run_extract_key(method, manual_key))

    console.rule("[bold]微信聊天记录导出工具")
    console.print("[dim]仅用于导出你本人账号的本地聊天记录 · 全程只读 · 语音转写完全本地[/]")

    # ---- locate account ----------------------------------------------------
    try:
        layout = locator.select_account(account)
    except (FileNotFoundError, ValueError) as e:
        _fail(str(e))
        sys.exit(1)
    console.print(f"      账号: [bold]{layout.account_id}[/]  版本: [bold]{layout.version}[/]  "
                  f"消息库: {len(layout.message_dbs)} 个")

    total = 4
    # ---- 1. keys ------------------------------------------------------------
    _step(1, total, "正在获取微信数据库密钥...")
    # --key is a convenient shortcut for auto->manual, but respect an explicit
    # --key-method if the user set one.
    method = "manual" if (manual_key and key_method == "auto") else key_method
    try:
        keys = stage_keys(method, manual_key)
    except key_extractor.KeyExtractionError as e:
        _fail(str(e))
        sys.exit(2)
    _ok(f"密钥获取成功 ({len(keys)} 个)")

    # ---- 2. decrypt ---------------------------------------------------------
    _step(2, total, "正在解密数据库...")
    try:
        decrypted = stage_decrypt(layout, keys)
    except db_decryptor.DecryptError as e:
        _fail(str(e))
        sys.exit(3)
    _ok(f"解密完成，共 {len(decrypted)} 个数据库文件")

    # ---- 3. select contact --------------------------------------------------
    _step(3, total, "请选择联系人:")
    contact = stage_select_contact(decrypted, layout, contact_kw, assume_yes)
    _ok(f"已选择: {contact.display_name} ({contact.message_count:,} 条)")

    # ---- 4. range / options / export ---------------------------------------
    _step(4, total, "请设置导出范围:")
    if not assume_yes:
        start = start if start is not None else Prompt.ask(
            "      开始日期 (留空=全部)", default="", console=console)
        end = end if end is not None else Prompt.ask(
            "      结束日期 (留空=今天)", default="", console=console)
        if do_voice is None:
            do_voice = Confirm.ask("      导出语音转文字？(需要较长时间)", default=False,
                                   console=console)
        if fmt is None:
            choice = Prompt.ask("      导出格式 (1=HTML 2=CSV 3=TXT 4=PDF 5=A4图片 6=全部)",
                                choices=["1", "2", "3", "4", "5", "6"], default="1", console=console)
            fmt = {"1": "html", "2": "csv", "3": "txt", "4": "pdf", "5": "a4", "6": "all"}[choice]
    do_voice = bool(do_voice)
    fmt = fmt or "html"

    try:
        start_ts = _parse_date(start, end=False)
        end_ts = _parse_date(end, end=True)
    except ValueError:
        _fail("日期格式无效，请使用 YYYY-MM-DD（例如 2024-01-01）。")
        sys.exit(4)

    hit = db_reader.find_chat_table(
        db_reader.build_table_index([decrypted.get(db, db) for db in layout.message_dbs]),
        contact, layout.version,
    )
    if not hit:
        _fail("未找到该联系人的聊天记录表。")
        sys.exit(4)
    owner_username = (db_reader.resolve_owner_wxid(layout, decrypted)
                      if layout.is_v4 else "")
    messages = db_reader.read_messages(hit[0], hit[1], layout, contact,
                                       owner_username, start_ts, end_ts)
    if flip_sender:
        for m in messages:
            m.is_sender = not m.is_sender

    console.print(f"\n正在导出...")
    with _progress() as p:
        t = p.add_task("处理消息", total=max(len(messages), 1))
        for i, m in enumerate(messages, 1):
            message_parser.parse(m, is_group=contact.is_group)
            p.update(t, completed=i)

    # ---- build the bundle, then resolve REAL media (avatars + voice + images) ---
    all_contacts = db_reader.read_contacts(decrypted, layout)
    owner = all_contacts.get(owner_username) if owner_username else None
    if owner is None:
        owner = Contact(username=owner_username or layout.account_id or "me", nickname="我")
    members = all_contacts if contact.is_group else {}
    bundle = ExportBundle(
        contact=contact, owner=owner, messages=messages, layout=layout,
        members=members, start_date=start or None, end_date=end or None,
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    media = media_tmp = None
    if layout.is_v4:
        try:
            media, media_tmp = _resolve_media(bundle, layout, keys)
        except Exception as e:  # never let media resolution kill the export
            _warn(f"媒体解析部分失败（不影响文本导出）: {e}")

    # voice transcription (uses the real SILK stashed in extra by _resolve_media)
    if do_voice:
        voice_msgs = [m for m in messages if m.needs_voice]
        if voice_msgs:
            from core import voice_converter
            for m in voice_msgs:
                if "voice_blob" not in m.extra and not m.media_src:
                    fp = _resolve_voice(layout, m)
                    if fp:
                        m.media_src = fp
            conv = voice_converter.VoiceConverter(engine=whisper_engine, model=whisper_model)
            with _progress() as p:
                t = p.add_task("转换语音", total=len(voice_msgs))

                def vcb(d, total_):
                    p.update(t, completed=d)
                try:
                    voice_converter.transcribe_messages(voice_msgs, conv, progress=vcb)
                finally:
                    conv.close()
        else:
            _warn("没有语音消息需要转换。")

    targets = stage_export(bundle, fmt, output_dir, a4_density=a4_density,
                           embed_media=self_contained)

    console.print()
    _ok("导出完成！")
    for t in targets:
        console.print(f"      文件: [bold]{t}[/]")

    # If we ran under sudo, hand the cache + exports back to the real user.
    _chown_back([constants.APP_DIR] + list(targets))

    if media:
        media.close()
    if media_tmp:
        import shutil
        shutil.rmtree(media_tmp, ignore_errors=True)

    # cleanup
    if not keep_decrypted:
        if assume_yes or Confirm.ask("\n      清理解密后的临时数据库？", default=True,
                                     console=console):
            db_decryptor.cleanup_decrypted()
            _ok("已清理临时文件。")


def _resolve_media(bundle: ExportBundle, layout: Layout, keys):
    """Resolve real avatars + voice audio into temp files referenced by the bundle.

    Avatars (JPEG) come from head_image.db; voice (SILK) from media_*.db — both
    decrypted with the keys we already have. Returns ``(resolver, tmp_dir)`` for
    cleanup after export. Images need a separately-extracted key (see README).
    """
    import hashlib
    import tempfile
    from core.media import MediaResolver, silk_to_wav

    media = MediaResolver(layout, keys)
    tmp = tempfile.mkdtemp(prefix="wx_media_out_")

    # avatars: one per distinct sender (self / partner / each group member)
    n_av = 0
    for uname in {bundle.sender_key(m) for m in bundle.messages}:
        if not uname:
            continue
        try:
            data = media.avatar(uname)
        except Exception:
            data = None
        if data:
            p = os.path.join(tmp, "av_" + hashlib.md5(uname.encode()).hexdigest()[:12] + ".jpg")
            with open(p, "wb") as fh:
                fh.write(data)
            bundle.avatars[uname] = p
            n_av += 1

    # voice: stash the SILK (for transcription) + a WAV (for playback)
    n_voice = 0
    for m in bundle.messages:
        if not m.needs_voice:
            continue
        try:
            silk = media.voice_silk(m, bundle.contact.username)
        except Exception:
            silk = None
        if not silk:
            continue
        m.extra["voice_blob"] = silk
        wav = silk_to_wav(silk)
        if wav:
            p = os.path.join(tmp, "voice_%d.wav" % m.local_id)
            with open(p, "wb") as fh:
                fh.write(wav)
            m.media_src = p
            n_voice += 1

    # images: decode V2 .dat (needs the memory-extracted image key)
    n_img = 0
    image_msgs = [m for m in bundle.messages if m.kind in ("image", "sticker")]
    if image_msgs:
        try:
            media.prepare_images(image_msgs, bundle.contact.username)
        except Exception:
            pass
        for m in image_msgs:
            try:
                res = media.image(m)
            except Exception:
                res = None
            if res:
                data, ext = res
                p = os.path.join(tmp, "img_%d.%s" % (m.local_id, ext or "jpg"))
                with open(p, "wb") as fh:
                    fh.write(data)
                m.media_src = p
                n_img += 1

    # videos: plaintext .mp4 + thumbnail (mapped via message_resource.db)
    n_vid = 0
    for m in bundle.messages:
        if m.kind != "video":
            continue
        try:
            mp4, thumb = media.video(m)
        except Exception:
            mp4, thumb = None, None
        if thumb and os.path.exists(thumb):
            m.media_src = thumb                 # poster / A4 thumbnail
        if mp4 and os.path.exists(mp4):
            m.extra["video_file"] = mp4         # playable file (copied by exporter)
            n_vid += 1

    if n_av or n_voice or n_img or n_vid:
        _ok(f"已解析真实媒体：头像 {n_av} 个、语音 {n_voice} 条、图片 {n_img} 张、视频 {n_vid} 个")
    if image_msgs and n_img == 0 and not key_extractor.load_image_key():
        _warn("图片密钥未缓存 → 图片暂以占位显示。运行一次 "
              "[bold]sudo .venv/bin/python main.py --extract-key[/]（先在微信打开有图片的聊天）即可还原图片。")
    return media, tmp


def _resolve_voice(layout: Layout, msg: Message) -> Optional[str]:
    """Best-effort: locate the .silk voice file for a message on disk.

    Linking a message row to its audio file is version-specific and fragile; we
    try to match by clientmsgid / server_id in the media tree. Returns None when
    no candidate is found (the message keeps its [语音] label, untranscribed).
    """
    if not layout.media_root or not os.path.isdir(layout.media_root):
        return None
    needles = [n for n in (msg.extra.get("clientmsgid"), msg.server_id) if n]
    if not needles:
        return None
    for root, _dirs, files in os.walk(layout.media_root):
        for fn in files:
            low = fn.lower()
            if low.endswith((".silk", ".aud", ".amr", ".slk")) and any(n in fn for n in needles):
                return os.path.join(root, fn)
    return None


if __name__ == "__main__":
    main()
