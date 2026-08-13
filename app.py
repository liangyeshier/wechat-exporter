#!/usr/bin/env python3
"""app.py — 微信聊天记录导出工具 · 本地图形界面 (Flask).

在浏览器里挑选联系人、设定范围/格式并导出，无需使用终端。

* 本地专用 (local-only): 仅监听 127.0.0.1，绝不对外暴露；浏览器自动打开。
* 只读 (read-only): 复用 main.py 的既有流水线，只读取并复制微信数据，
  从不写入或修改微信本身的任何文件。语音转写完全离线。
* 图片密钥在本机离线推导，无需 sudo。

运行:  python app.py   ->  http://127.0.0.1:8765 (端口被占用则自动换一个)

这是一个导出你自己账号本地聊天记录的个人工具。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from typing import Dict, List, Optional, Tuple

try:
    from flask import (Flask, abort, jsonify, request, send_file,
                       send_from_directory)
except ImportError:  # pragma: no cover
    raise SystemExit(
        "缺少依赖 flask。请先安装:  pip install flask\n"
        "(本工具的图形界面基于 Flask；命令行版 main.py 不需要它。)"
    )

# Reuse the existing pipeline — do NOT reimplement crypto / parsing here.
from core import (constants, db_decryptor, db_reader, key_extractor,  # noqa: E402
                  locator, message_parser)
from core import macos_key_setup  # noqa: E402
from core.media import MediaResolver, silk_to_wav  # noqa: E402
from core.models import Contact, ExportBundle, Layout, Message  # noqa: E402
from export import csv_exporter, html_exporter, txt_exporter  # noqa: E402


# --------------------------------------------------------------------------- #
# Module globals: a per-account opened "session" so repeated requests do not
# re-decrypt. Keyed by account_id. Guarded by a lock (Flask is multi-threaded).
# --------------------------------------------------------------------------- #
_SESSIONS: Dict[str, "Session"] = {}
_SESSIONS_LOCK = threading.RLock()

# Resolved media (avatars/images/voice WAV) served to the preview, keyed by an
# opaque token -> absolute path on disk. Files live in a per-session temp dir.
_MEDIA_FILES: Dict[str, str] = {}
_MEDIA_LOCK = threading.RLock()

# Generated export files made downloadable, keyed by token -> abs path.
_EXPORT_FILES: Dict[str, str] = {}
_EXPORT_LOCK = threading.RLock()

# First-run key setup runs in a background thread because copying/signing WeChat
# and waiting for its database open can take a few minutes.
_KEY_SETUP_LOCK = threading.RLock()
_KEY_SETUP_STATE = {
    "state": "ready" if key_extractor.load_cached_keys() else "missing",
    "message": "数据库密钥已就绪。" if key_extractor.load_cached_keys()
               else "首次使用需要完成本机密钥设置。",
}

# Default output directory (sudo-safe expansion via constants.HOME).
DEFAULT_OUT_DIR = os.path.join(constants.HOME, "Desktop")
VOICE_TRANSCRIPTION_AVAILABLE = all(
    importlib.util.find_spec(name) is not None
    for name in ("faster_whisper", "numpy")
)

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Session: holds the decrypted map + layout + contacts + a shared MediaResolver.
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, layout: Layout, keys: Dict[str, str],
                 decrypted: Dict[str, str]) -> None:
        self.layout = layout
        self.keys = keys
        self.decrypted = decrypted
        self.contacts: Dict[str, Contact] = db_reader.read_contacts(decrypted, layout)
        self.table_index: Dict[str, str] = db_reader.build_table_index(
            [decrypted.get(db, db) for db in layout.message_dbs]
        )
        self.owner_wxid: str = (db_reader.resolve_owner_wxid(layout, decrypted)
                                if layout.is_v4 else "")
        # One MediaResolver per session (lazily decrypts aux DBs, reused).
        self.media = MediaResolver(layout, keys)
        # Per-session temp dir for resolved media we serve to the preview.
        self.media_tmp = tempfile.mkdtemp(prefix="wx_gui_media_")
        self._lock = threading.RLock()

    def decrypted_ok(self) -> bool:
        """True if our decrypted DB copies still exist on disk.

        The shared ~/.wechat_exporter/decrypted dir can be wiped by a CLI run's
        cleanup; if so this session is stale and must be rebuilt.
        """
        try:
            return bool(self.decrypted) and all(
                os.path.exists(p) for p in self.decrypted.values())
        except Exception:
            return False

    def has_table(self, c: Contact) -> bool:
        return c.chat_table(self.layout.version).lower() in self.table_index

    def find_table(self, c: Contact) -> Optional[Tuple[str, str]]:
        return db_reader.find_chat_table(self.table_index, c, self.layout.version)

    def lock(self):
        return self._lock


def _get_session(account_id: str) -> Session:
    """Return (opening if needed) the cached Session for an account id.

    Mirrors main.py: select_account -> get_keys(use_cache=True) ->
    decrypt_layout -> read_contacts + build_table_index.
    """
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(account_id)
        if sess is not None and sess.decrypted_ok():
            return sess
        if sess is not None:
            # Stale (decrypted copies were cleaned, e.g. by a CLI run) — re-decrypt.
            try:
                sess.media.close()
            except Exception:
                pass
        layout = locator.select_account(account_id or None)
        keys = key_extractor.get_keys(method="auto", manual=None,
                                      use_cache=True, save=True)
        decrypted = db_decryptor.decrypt_layout(layout, keys)
        sess = Session(layout, keys, decrypted)
        _SESSIONS[layout.account_id] = sess
        # Also alias under the requested id if it differed (e.g. empty -> real).
        if account_id and account_id != layout.account_id:
            _SESSIONS[account_id] = sess
        return sess


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
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


def _safe_name(name: str) -> str:
    bad = '/\\:*?"<>|'
    return "".join("_" if c in bad else c for c in name).strip() or "chat"


def _register_media(path: str) -> str:
    """Register an on-disk media file, returning a token used in /media/<token>."""
    token = hashlib.md5(os.path.abspath(path).encode()).hexdigest()[:20]
    with _MEDIA_LOCK:
        _MEDIA_FILES[token] = path
    return token


def _register_export(path: str) -> str:
    token = hashlib.md5((path + str(time.time())).encode()).hexdigest()[:20]
    with _EXPORT_LOCK:
        _EXPORT_FILES[token] = path
    return token


def _err(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def _account_info(layout: Layout) -> Dict:
    return {
        "account_id": layout.account_id,
        "version": layout.version,
        "message_dbs": len(layout.message_dbs),
    }


def _set_key_setup_state(state: str, message: str) -> None:
    with _KEY_SETUP_LOCK:
        _KEY_SETUP_STATE["state"] = state
        _KEY_SETUP_STATE["message"] = message


def _run_key_setup() -> None:
    try:
        macos_key_setup.setup(lambda message: _set_key_setup_state("running", message))
        # Cached sessions may have been opened with stale decrypted copies.
        with _SESSIONS_LOCK:
            for sess in set(_SESSIONS.values()):
                try:
                    sess.media.close()
                except Exception:
                    pass
            _SESSIONS.clear()
        db_decryptor.cleanup_decrypted()
        _set_key_setup_state("ready", "设置成功。联系人、群聊和消息现在可以读取。")
    except Exception as exc:
        traceback.print_exc()
        _set_key_setup_state("error", str(exc))


# --------------------------------------------------------------------------- #
# Building the list of message dicts for the preview + serving media.
# --------------------------------------------------------------------------- #
def _resolve_one_avatar(sess: Session, bundle: ExportBundle,
                        username: str, cache: Dict[str, Optional[str]]) -> Optional[str]:
    """Resolve a single avatar to a served-media token (or None). Cached per call."""
    if username in cache:
        return cache[username]
    token = None
    try:
        data = sess.media.avatar(username)
    except Exception:
        data = None
    if data:
        p = os.path.join(sess.media_tmp,
                         "av_" + hashlib.md5(username.encode()).hexdigest()[:12] + ".jpg")
        try:
            with open(p, "wb") as fh:
                fh.write(data)
            bundle.avatars[username] = p
            token = _register_media(p)
        except OSError:
            token = None
    cache[username] = token
    return token


def _build_preview_rows(sess: Session, bundle: ExportBundle,
                        load_images: bool, load_voice: bool) -> List[Dict]:
    """Resolve media for the preview and return one serializable dict per message.

    Avatars are always resolved (cheap). Images/voice WAVs are resolved when the
    flags are set, and exposed through /media/<token> URLs the page can render.
    """
    rows: List[Dict] = []
    avatar_cache: Dict[str, Optional[str]] = {}

    # Pre-resolve images in one batch (needs the offline-derived image key).
    image_tokens: Dict[int, str] = {}
    if load_images:
        image_msgs = [m for m in bundle.messages if m.kind in ("image", "sticker")]
        if image_msgs:
            try:
                sess.media.prepare_images(image_msgs, bundle.contact.username)
            except Exception:
                pass
            for m in image_msgs:
                try:
                    res = sess.media.image(m)
                except Exception:
                    res = None
                if res:
                    data, ext = res
                    p = os.path.join(sess.media_tmp, "img_%d.%s" % (m.local_id, ext or "jpg"))
                    try:
                        with open(p, "wb") as fh:
                            fh.write(data)
                        m.media_src = p
                        image_tokens[m.local_id] = _register_media(p)
                    except OSError:
                        pass

    for m in bundle.messages:
        sender_key = bundle.sender_key(m)
        avatar_token = _resolve_one_avatar(sess, bundle, sender_key, avatar_cache)

        voice_token = None
        voice_seconds = None
        vlen = (m.extra or {}).get("voicelength_ms")
        try:
            voice_seconds = max(1, int(round(int(vlen) / 1000))) if vlen else None
        except (TypeError, ValueError):
            voice_seconds = None
        if load_voice and m.needs_voice:
            try:
                silk = sess.media.voice_silk(m, bundle.contact.username)
            except Exception:
                silk = None
            if silk:
                m.extra["voice_blob"] = silk
                wav = silk_to_wav(silk)
                if wav:
                    p = os.path.join(sess.media_tmp, "voice_%d.wav" % m.local_id)
                    try:
                        with open(p, "wb") as fh:
                            fh.write(wav)
                        m.media_src = p
                        voice_token = _register_media(p)
                    except OSError:
                        pass

        rows.append({
            "id": m.local_id,
            "is_sender": bool(m.is_sender),
            "sender_name": bundle.sender_name(m),
            "time_str": m.time_str,
            "date_key": m.date_key,
            "kind": m.kind,
            "kind_label": m.kind_label,
            "text": m.display_text or "",
            "voice_text": m.voice_text or "",
            "voice_seconds": voice_seconds,
            "avatar_url": ("/media/" + avatar_token) if avatar_token else None,
            "image_url": ("/media/" + image_tokens[m.local_id]) if m.local_id in image_tokens else None,
            "voice_url": ("/media/" + voice_token) if voice_token else None,
        })
    return rows


def _load_messages(sess: Session, contact: Contact,
                   start_ts: Optional[int], end_ts: Optional[int]) -> Tuple[ExportBundle, List[Message]]:
    """Read + parse messages for a contact and build an ExportBundle (mirror main.py)."""
    hit = sess.find_table(contact)
    if not hit:
        raise RuntimeError("未找到该联系人的聊天记录表。")
    messages = db_reader.read_messages(
        hit[0], hit[1], sess.layout, contact, sess.owner_wxid, start_ts, end_ts
    )
    for m in messages:
        message_parser.parse(m, is_group=contact.is_group)

    owner = Contact(username=sess.owner_wxid or "me", nickname="我")
    members = sess.contacts if contact.is_group else {}
    bundle = ExportBundle(
        contact=contact, owner=owner, messages=messages, layout=sess.layout,
        members=members, generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return bundle, messages


def _find_contact(sess: Session, username: str) -> Optional[Contact]:
    c = sess.contacts.get(username)
    if c is not None:
        return c
    # Fall back to a synthetic contact (deleted/absent from contact table but the
    # chat table may still exist for a raw username).
    if username:
        synth = Contact(username=username)
        if sess.has_table(synth):
            return synth
    return None


# --------------------------------------------------------------------------- #
# API routes
# --------------------------------------------------------------------------- #
@app.route("/api/accounts")
def api_accounts():
    """Detected account(s): version + message-db count."""
    try:
        accounts = locator.discover_accounts()
    except Exception as e:  # pragma: no cover
        return _err("扫描账号失败: %s" % e, 500)
    if not accounts:
        return _err("未发现任何微信账号。请确认已登录微信，并给终端/本程序开启『完全磁盘访问权限』。", 404)
    return jsonify({"ok": True, "accounts": [_account_info(a) for a in accounts]})


@app.route("/api/key-status")
def api_key_status():
    with _KEY_SETUP_LOCK:
        state = dict(_KEY_SETUP_STATE)
    state["ok"] = True
    state["has_keys"] = bool(key_extractor.load_cached_keys())
    return jsonify(state)


@app.route("/api/features")
def api_features():
    return jsonify({
        "ok": True,
        "voice_transcription": VOICE_TRANSCRIPTION_AVAILABLE,
    })


@app.route("/api/key-setup", methods=["POST"])
def api_key_setup():
    if request.content_type != "application/json":
        return _err("请求格式无效。", 415)
    with _KEY_SETUP_LOCK:
        if _KEY_SETUP_STATE["state"] == "running":
            return jsonify({"ok": True, **_KEY_SETUP_STATE})
        _KEY_SETUP_STATE["state"] = "running"
        _KEY_SETUP_STATE["message"] = "正在准备首次设置…"
    threading.Thread(target=_run_key_setup, daemon=True).start()
    return jsonify({"ok": True, "state": "running", "message": "正在准备首次设置…"})


@app.route("/api/contacts")
def api_contacts():
    """List contacts (display name, wechat id, message count) for an account."""
    account_id = request.args.get("account_id", "").strip()
    keyword = request.args.get("q", "").strip().lower()
    kind = request.args.get("kind", "all").strip().lower()
    try:
        sess = _get_session(account_id)
    except key_extractor.KeyExtractionError as e:
        return _err("获取密钥失败:\n" + str(e), 500)
    except (FileNotFoundError, ValueError) as e:
        return _err(str(e), 404)
    except Exception as e:  # pragma: no cover
        traceback.print_exc()
        return _err("打开账号失败: %s" % e, 500)

    try:
        version = sess.layout.version
        matched: List[Contact] = [
            c for c in sess.contacts.values()
            if (not keyword or keyword in c.search_blob())
            and (kind == "all"
                 or (kind == "groups" and c.is_group)
                 or (kind == "friends" and not c.is_group and not c.is_official)
                 or (kind == "official" and c.is_official))
            and sess.has_table(c)
        ]
        matched.sort(key=lambda c: c.display_name)

        limit = 200
        out = []
        for c in matched[:limit]:
            try:
                hit = sess.find_table(c)
                cnt = db_reader.count_messages(hit[0], hit[1], version=version) if hit else 0
            except Exception:
                cnt = 0
            c.message_count = cnt
            out.append({
                "username": c.username,
                "display_name": c.display_name,
                "wechat_id": c.alias or c.username,
                "is_group": c.is_group,
                "message_count": cnt,
            })
        out.sort(key=lambda r: (-r["message_count"], r["display_name"]))
        return jsonify({
            "ok": True,
            "account": _account_info(sess.layout),
            "contacts": out,
            "truncated": len(matched) > limit,
            "total": len(matched),
        })
    except Exception as e:  # never leak a 500 HTML page to the JSON client
        traceback.print_exc()
        return _err("加载联系人失败: %s" % e, 500)


@app.route("/api/preview")
def api_preview():
    """Load a conversation into the preview with resolved avatars/images/voice."""
    account_id = request.args.get("account_id", "").strip()
    username = request.args.get("username", "").strip()
    start = request.args.get("start", "").strip()
    end = request.args.get("end", "").strip()
    voice_flag = request.args.get("voice", "0") == "1"
    if not username:
        return _err("缺少 username。")
    try:
        sess = _get_session(account_id)
        contact = _find_contact(sess, username)
        if contact is None:
            return _err("找不到该联系人。", 404)
        start_ts = _parse_date(start, end=False)
        end_ts = _parse_date(end, end=True)
    except ValueError:
        return _err("日期格式无效，请使用 YYYY-MM-DD（例如 2024-01-01）。")
    except (FileNotFoundError, key_extractor.KeyExtractionError) as e:
        return _err(str(e), 500)

    try:
        with sess.lock():
            bundle, messages = _load_messages(sess, contact, start_ts, end_ts)
            rows = _build_preview_rows(sess, bundle, load_images=True,
                                       load_voice=voice_flag)
    except Exception as e:
        traceback.print_exc()
        return _err("加载消息失败: %s" % e, 500)

    return jsonify({
        "ok": True,
        "contact": {
            "username": contact.username,
            "display_name": contact.display_name,
            "is_group": contact.is_group,
        },
        "owner_name": "我",
        "count": len(rows),
        "messages": rows,
    })


@app.route("/api/export", methods=["POST"])
def api_export():
    """Generate the selected formats and return download links."""
    data = request.get_json(silent=True) or {}
    account_id = (data.get("account_id") or "").strip()
    username = (data.get("username") or "").strip()
    start = (data.get("start") or "").strip()
    end = (data.get("end") or "").strip()
    formats = data.get("formats") or []
    excluded = set(int(x) for x in (data.get("excluded") or []) if str(x).strip())
    voice_flag = bool(data.get("voice"))
    density = (data.get("density") or "normal").strip()
    out_dir = os.path.expanduser(data.get("out_dir") or DEFAULT_OUT_DIR)

    if not username:
        return _err("缺少 username。")
    if not formats:
        return _err("请至少选择一种导出格式。")
    if voice_flag and not VOICE_TRANSCRIPTION_AVAILABLE:
        return _err("当前应用包未安装语音转文字引擎。语音播放不受影响；源码版可安装 requirements-voice.txt 启用。")

    try:
        sess = _get_session(account_id)
        contact = _find_contact(sess, username)
        if contact is None:
            return _err("找不到该联系人。", 404)
        start_ts = _parse_date(start, end=False)
        end_ts = _parse_date(end, end=True)
    except ValueError:
        return _err("日期格式无效，请使用 YYYY-MM-DD（例如 2024-01-01）。")
    except (FileNotFoundError, key_extractor.KeyExtractionError) as e:
        return _err(str(e), 500)

    import main as cli  # reuse the verified CLI pipeline (identical media + export)
    targets: List[str] = []
    errors: List[str] = []
    media_obj = media_tmp = None
    try:
        with sess.lock():
            bundle, messages = _load_messages(sess, contact, start_ts, end_ts)
            # Filter out excluded message ids BEFORE resolving media + export.
            if excluded:
                bundle.messages = [m for m in bundle.messages if m.local_id not in excluded]
                messages = bundle.messages
            if not bundle.messages:
                return _err("筛选后没有可导出的消息。")
            bundle.start_date = start or None
            bundle.end_date = end or None

            # Resolve REAL media exactly like the CLI (avatars/images/video/voice
            # into a fresh temp dir, setting msg.media_src / bundle.avatars).
            try:
                if sess.layout.is_v4:
                    media_obj, media_tmp = cli._resolve_media(bundle, sess.layout, sess.keys)
            except Exception:
                traceback.print_exc()
            if voice_flag:
                _transcribe(bundle)

            # Export each requested format via the proven stage_export.
            fmt_map = {"html": "html", "txt": "txt", "csv": "csv",
                       "pdf": "pdf", "png": "a4", "a4": "a4"}
            for f in [x.lower() for x in formats]:
                try:
                    # HTML is exported SELF-CONTAINED (media inlined) so the single
                    # downloaded file works without a sibling assets folder.
                    targets += cli.stage_export(bundle, fmt_map.get(f, f), out_dir,
                                                a4_density=density, embed_media=True)
                except Exception as e:
                    errors.append("%s 导出失败: %s" % (f, e))
    except Exception as e:
        traceback.print_exc()
        return _err("导出失败: %s" % e, 500)
    finally:
        if media_obj is not None:
            try:
                media_obj.close()
            except Exception:
                pass
        if media_tmp:
            import shutil
            shutil.rmtree(media_tmp, ignore_errors=True)

    files = []
    for path in targets:
        token = _register_export(path)
        files.append({
            "name": os.path.basename(path),
            "path": path,
            "url": "/download/" + token,
        })
    _chown_back([constants.APP_DIR] + targets)
    return jsonify({"ok": True, "files": files, "errors": errors,
                    "out_dir": out_dir, "count": len(bundle.messages)})


# --------------------------------------------------------------------------- #
# Export plumbing
# --------------------------------------------------------------------------- #
def _resolve_media_for_export(sess: Session, bundle: ExportBundle,
                              voice_flag: bool) -> None:
    """Resolve avatars + images (+ voice SILK/WAV) into media_src on each message."""
    avatar_cache: Dict[str, Optional[str]] = {}
    for uname in {bundle.sender_key(m) for m in bundle.messages}:
        if uname:
            _resolve_one_avatar(sess, bundle, uname, avatar_cache)

    image_msgs = [m for m in bundle.messages if m.kind in ("image", "sticker")]
    if image_msgs:
        try:
            sess.media.prepare_images(image_msgs, bundle.contact.username)
        except Exception:
            pass
        for m in image_msgs:
            try:
                res = sess.media.image(m)
            except Exception:
                res = None
            if res:
                data, ext = res
                p = os.path.join(sess.media_tmp, "img_%d.%s" % (m.local_id, ext or "jpg"))
                try:
                    with open(p, "wb") as fh:
                        fh.write(data)
                    m.media_src = p
                except OSError:
                    pass

    for m in bundle.messages:
        if not m.needs_voice:
            continue
        try:
            silk = sess.media.voice_silk(m, bundle.contact.username)
        except Exception:
            silk = None
        if not silk:
            continue
        m.extra["voice_blob"] = silk
        wav = silk_to_wav(silk)
        if wav:
            p = os.path.join(sess.media_tmp, "voice_%d.wav" % m.local_id)
            try:
                with open(p, "wb") as fh:
                    fh.write(wav)
                m.media_src = p
            except OSError:
                pass


def _transcribe(bundle: ExportBundle) -> None:
    voice_msgs = [m for m in bundle.messages if m.needs_voice]
    if not voice_msgs:
        return
    try:
        from core import voice_converter
    except ImportError:
        return
    conv = voice_converter.VoiceConverter(engine="faster", model="base")
    try:
        voice_converter.transcribe_messages(voice_msgs, conv)
    finally:
        conv.close()


def _run_exporters(bundle: ExportBundle, formats: List[str], out_dir: str,
                   density: str) -> Tuple[List[str], List[str]]:
    """Invoke the requested exporters. Returns (written_paths, error_messages)."""
    if out_dir.startswith("~"):
        out_dir = constants.HOME + out_dir[1:]
    out_dir = os.path.expanduser(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    rng = ""
    if bundle.start_date or bundle.end_date:
        rng = "_%s-%s" % (bundle.start_date or "", bundle.end_date or "今天")
    base = "微信导出_%s%s" % (_safe_name(bundle.contact.display_name), rng)

    targets: List[str] = []
    errors: List[str] = []
    fmts = [f.lower() for f in formats]

    if "html" in fmts:
        try:
            targets.append(html_exporter.export_html(
                bundle, os.path.join(out_dir, base + ".html")))
        except Exception as e:
            errors.append("HTML 导出失败: %s" % e)
    if "txt" in fmts:
        try:
            targets.append(txt_exporter.export_txt(
                bundle, os.path.join(out_dir, base + ".txt")))
        except Exception as e:
            errors.append("TXT 导出失败: %s" % e)
    if "csv" in fmts:
        try:
            targets.append(csv_exporter.export_csv(
                bundle, os.path.join(out_dir, base + ".csv")))
        except Exception as e:
            errors.append("CSV 导出失败: %s" % e)

    # PDF / A4 PNG are provided by the parallel image_exporter — import lazily so
    # the app still runs if that module is absent.
    if "pdf" in fmts or "png" in fmts:
        try:
            from export import image_exporter  # type: ignore
        except ImportError:
            errors.append("PDF / A4图片 模块 (export/image_exporter.py) 暂不可用，已跳过。")
            image_exporter = None  # type: ignore
        if image_exporter is not None:
            if "pdf" in fmts:
                try:
                    res = image_exporter.export_pdf(
                        bundle, os.path.join(out_dir, base + ".pdf"), density=density)
                    targets.extend(_as_list(res))
                except TypeError:
                    # Older signature without density kwarg.
                    try:
                        res = image_exporter.export_pdf(
                            bundle, os.path.join(out_dir, base + ".pdf"))
                        targets.extend(_as_list(res))
                    except Exception as e:
                        errors.append("PDF 导出失败: %s" % e)
                except Exception as e:
                    errors.append("PDF 导出失败: %s" % e)
            if "png" in fmts:
                try:
                    res = image_exporter.export_a4_images(
                        bundle, os.path.join(out_dir, base + "_A4"), density=density)
                    targets.extend(_as_list(res))
                except TypeError:
                    try:
                        res = image_exporter.export_a4_images(
                            bundle, os.path.join(out_dir, base + "_A4"))
                        targets.extend(_as_list(res))
                    except Exception as e:
                        errors.append("A4图片 导出失败: %s" % e)
                except Exception as e:
                    errors.append("A4图片 导出失败: %s" % e)
    return targets, errors


def _as_list(res) -> List[str]:
    if res is None:
        return []
    if isinstance(res, str):
        return [res]
    try:
        return [str(x) for x in res]
    except TypeError:
        return [str(res)]


def _chown_back(paths) -> None:
    """If running under sudo, hand created files back to the real user."""
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
        if not p:
            continue
        if os.path.isdir(p):
            for b, dirs, files in os.walk(p):
                ch(b)
                for n in dirs + files:
                    ch(os.path.join(b, n))
        elif os.path.exists(p):
            ch(p)
        # HTML exports also write a sibling <base>_assets/ folder.
        assets = os.path.splitext(p)[0] + "_assets"
        if os.path.isdir(assets):
            ch(assets)
            for b, dirs, files in os.walk(assets):
                for n in dirs + files:
                    ch(os.path.join(b, n))


# --------------------------------------------------------------------------- #
# Media + download serving
# --------------------------------------------------------------------------- #
@app.route("/media/<token>")
def media(token: str):
    with _MEDIA_LOCK:
        path = _MEDIA_FILES.get(token)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path)


@app.route("/download/<token>")
def download(token: str):
    with _EXPORT_LOCK:
        path = _EXPORT_FILES.get(token)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_from_directory(os.path.dirname(path), os.path.basename(path),
                               as_attachment=True)


@app.route("/api/quit", methods=["POST"])
def api_quit():
    """Stop the local packaged app after the response reaches the browser."""
    def stop() -> None:
        time.sleep(0.35)
        os._exit(0)
    threading.Thread(target=stop, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/")
def index():
    return PAGE


# --------------------------------------------------------------------------- #
# Single-page UI (inline HTML + CSS + JS; no external CDNs).
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>微信聊天记录导出</title>
<style>
  :root { --green:#07c160; --bg:#ededed; --bubble:#fff; --bubble-me:#95ec69; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
         background:#f5f5f5; color:#222; }
  header { background:var(--green); color:#fff; padding:12px 20px; font-size:18px; font-weight:600;
           display:flex; align-items:center; justify-content:space-between; gap:12px; }
  header small { font-weight:400; opacity:.85; font-size:12px; margin-left:8px; }
  header button { background:rgba(255,255,255,.16); border:1px solid rgba(255,255,255,.45);
                  padding:6px 10px; font-size:12px; white-space:nowrap; }
  .wrap { display:flex; gap:14px; padding:14px; align-items:flex-start; flex-wrap:wrap; }
  .panel { background:#fff; border-radius:10px; box-shadow:0 1px 4px rgba(0,0,0,.08); padding:14px; }
  .col-left { flex:0 0 360px; max-width:360px; }
  .col-right { flex:1 1 480px; min-width:360px; }
  h3 { margin:0 0 8px; font-size:14px; color:#333; }
  .acct { font-size:13px; color:#444; background:#f0fff5; border:1px solid #d6f5e3;
          padding:8px 10px; border-radius:8px; margin-bottom:10px; }
  .acct .pill { display:inline-block; background:var(--green); color:#fff; border-radius:10px;
                padding:1px 8px; font-size:11px; margin-right:6px; }
  input[type=text], input[type=date], select {
    width:100%; padding:8px 10px; border:1px solid #ddd; border-radius:8px; font-size:13px; }
  .search { margin-bottom:8px; }
  .seg { display:grid; grid-template-columns:repeat(4,1fr); gap:6px; margin-bottom:8px; }
  .seg button { padding:7px 4px; font-size:12px; background:#f7f7f7; color:#555; border:1px solid #ddd; }
  .seg button.sel { background:#e8fbef; color:#078c49; border-color:#07c160; font-weight:600; }
  .contacts { max-height:420px; overflow:auto; border:1px solid #eee; border-radius:8px; }
  .contact { display:flex; justify-content:space-between; gap:8px; padding:9px 11px;
             border-bottom:1px solid #f2f2f2; cursor:pointer; font-size:13px; }
  .contact:hover { background:#f6f6f6; }
  .contact.sel { background:#e8fbef; }
  .contact .nm { font-weight:600; }
  .contact .id { color:#999; font-size:11px; }
  .contact .cnt { color:#07c160; font-size:12px; white-space:nowrap; }
  .opts { margin-top:12px; font-size:13px; }
  .opts label { display:inline-flex; align-items:center; gap:5px; margin:4px 10px 4px 0; }
  .row { display:flex; gap:8px; margin:6px 0; align-items:center; }
  .row label.cap { flex:0 0 64px; color:#666; font-size:12px; }
  button { background:var(--green); color:#fff; border:none; border-radius:8px; padding:9px 16px;
           font-size:14px; cursor:pointer; }
  button.ghost { background:#fff; color:#07c160; border:1px solid #07c160; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .btns { display:flex; gap:10px; margin-top:12px; }
  .preview { background:var(--bg); border-radius:8px; padding:10px; height:560px; overflow:auto; }
  .datebar { text-align:center; color:#888; font-size:11px; margin:10px 0; }
  .msg { display:flex; gap:8px; margin:8px 4px; align-items:flex-start; }
  .msg.me { flex-direction:row-reverse; }
  .av { width:36px; height:36px; border-radius:6px; background:#bbb; flex:0 0 36px;
        object-fit:cover; display:flex; align-items:center; justify-content:center;
        color:#fff; font-size:14px; }
  .body { max-width:72%; }
  .nm2 { font-size:11px; color:#888; margin:0 2px 2px; }
  .msg.me .nm2 { text-align:right; }
  .bubble { background:var(--bubble); border-radius:8px; padding:8px 11px; font-size:14px;
            line-height:1.45; word-break:break-word; white-space:pre-wrap; position:relative; }
  .msg.me .bubble { background:var(--bubble-me); }
  .bubble img.pic { max-width:200px; max-height:240px; border-radius:6px; display:block; }
  .bubble .vt { color:#666; font-size:12px; margin-top:4px; border-top:1px dashed #ccc; padding-top:4px; }
  .bubble .tag { color:#888; font-size:12px; }
  .meta { font-size:10px; color:#aaa; margin-top:2px; }
  .msg.me .meta { text-align:right; }
  .del { position:absolute; top:-8px; cursor:pointer; font-size:11px; color:#fff; background:#e35;
         border-radius:10px; padding:0 6px; line-height:16px; opacity:.0; transition:opacity .15s; }
  .msg:hover .del { opacity:.9; }
  .msg.me .del { left:-8px; } .msg:not(.me) .del { right:-8px; }
  .msg.excluded { opacity:.35; }
  .msg.excluded .bubble { text-decoration:line-through; }
  .status { font-size:13px; color:#555; margin-top:8px; min-height:18px; }
  .err { color:#c0392b; white-space:pre-wrap; }
  .files a { display:block; margin:4px 0; color:#07c160; }
  audio { width:200px; height:32px; }
  .muted { color:#999; font-size:12px; }
  .hint { font-size:11px; color:#999; margin-top:4px; }
  .setup { display:flex; align-items:center; gap:10px; padding:10px; margin-bottom:12px;
           background:#f7f8fa; border:1px solid #e5e7eb; border-radius:8px; }
  .setup.ready { background:#edf9f1; border-color:#bee7ca; }
  .setup.error { background:#fff2f2; border-color:#f2caca; }
  .setup-copy { flex:1; min-width:0; font-size:12px; line-height:1.45; color:#555; }
  .setup-copy b { display:block; color:#222; font-size:13px; }
  .setup button { flex:0 0 auto; padding:8px 11px; font-size:12px; }
</style>
</head>
<body>
<header><span>微信聊天记录导出 <small>本地运行 · 全程只读 · 仅用于导出你自己的聊天记录</small></span>
  <button id="quitBtn" type="button" title="停止本地服务并退出应用">退出工具</button>
</header>
<div class="wrap">
  <div class="panel col-left">
    <div id="keySetup" class="setup">
      <div class="setup-copy"><b>正在检查数据库密钥…</b><span>请稍候</span></div>
      <button id="keySetupBtn" type="button" disabled>首次设置</button>
    </div>
    <h3>1 · 账号</h3>
    <div id="accounts" class="muted">正在检测账号…</div>

    <h3 style="margin-top:14px">2 · 联系人</h3>
    <input id="q" class="search" type="text" placeholder="搜索昵称 / 备注 / 微信号，回车搜索">
    <div class="seg" id="kindSeg">
      <button type="button" data-kind="all" class="sel">全部</button>
      <button type="button" data-kind="friends">好友</button>
      <button type="button" data-kind="groups">群聊</button>
      <button type="button" data-kind="official">公众号</button>
    </div>
    <div id="contacts" class="contacts"><div class="muted" style="padding:10px">请选择账号后加载…</div></div>
    <div id="contactHint" class="hint"></div>

    <div class="opts">
      <h3 style="margin-top:14px">3 · 范围与格式</h3>
      <div class="row"><label class="cap">开始</label><input id="start" type="date"></div>
      <div class="row"><label class="cap">结束</label><input id="end" type="date"></div>
      <div>
        <label><input type="checkbox" class="fmt" value="html" checked> HTML</label>
        <label><input type="checkbox" class="fmt" value="txt"> TXT</label>
        <label><input type="checkbox" class="fmt" value="csv"> CSV</label>
        <label><input type="checkbox" class="fmt" value="pdf"> PDF(A4)</label>
        <label><input type="checkbox" class="fmt" value="png"> A4图片(PNG)</label>
      </div>
      <div class="row" style="margin-top:6px">
        <label><input type="checkbox" id="voice"> 语音转文字(慢)</label>
        <label class="cap" style="flex:0 0 auto;margin-left:10px">密度</label>
        <select id="density" style="width:auto">
          <option value="compact">紧凑</option>
          <option value="normal" selected>正常</option>
          <option value="large">宽松</option>
        </select>
      </div>
    </div>
    <div class="btns">
      <button id="loadBtn" class="ghost" disabled>预览 / 加载消息</button>
      <button id="exportBtn" disabled>导出</button>
    </div>
    <div id="status" class="status"></div>
    <div id="files" class="files"></div>
  </div>

  <div class="panel col-right">
    <h3>4 · 预览（点 ✕ 可排除该条，不会被导出）</h3>
    <div id="preview" class="preview"><div class="muted" style="padding:20px">选择联系人后点击「预览 / 加载消息」。</div></div>
  </div>
</div>

<script>
let ACCOUNT = null;       // selected account_id
let CONTACT = null;       // selected username
let CONTACT_NAME = "";
let MESSAGES = [];        // loaded preview rows
let CONTACT_KIND = "all";
const EXCLUDED = new Set();
let KEY_POLL = null;

function $(id){ return document.getElementById(id); }
function esc(s){ const d=document.createElement('div'); d.textContent=s==null?"":s; return d.innerHTML; }
function setStatus(t, isErr){ const s=$("status"); s.className="status"+(isErr?" err":""); s.textContent=t||""; }

async function getJSON(url){
  const r = await fetch(url);
  const j = await r.json().catch(()=>({ok:false,error:"服务器返回了非 JSON 响应"}));
  if(!r.ok || !j.ok) throw new Error(j.error || ("HTTP "+r.status));
  return j;
}
async function postJSON(url, body){
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const j = await r.json().catch(()=>({ok:false,error:"服务器返回了非 JSON 响应"}));
  if(!r.ok || !j.ok) throw new Error(j.error || ("HTTP "+r.status));
  return j;
}

function renderKeyStatus(j){
  const box=$("keySetup"), copy=box.querySelector(".setup-copy"), btn=$("keySetupBtn");
  box.className="setup"+(j.state==="ready"?" ready":j.state==="error"?" error":"");
  const title=j.state==="ready"?"数据库已就绪":j.state==="running"?"正在设置数据库密钥":
              j.state==="error"?"设置没有完成":"需要首次设置";
  copy.innerHTML="<b>"+esc(title)+"</b><span>"+esc(j.message||"")+"</span>";
  btn.disabled=j.state==="running";
  btn.textContent=j.state==="ready"?"更新密钥":j.state==="error"?"重试":"首次设置";
  if(j.state==="running" && !KEY_POLL){ KEY_POLL=setInterval(loadKeyStatus, 1200); }
  if(j.state!=="running" && KEY_POLL){ clearInterval(KEY_POLL); KEY_POLL=null; }
  if(j.state==="ready" && ACCOUNT && !CONTACT){ loadContacts($("q").value||""); }
}

async function loadKeyStatus(){
  try{ renderKeyStatus(await getJSON("/api/key-status")); }
  catch(e){ renderKeyStatus({state:"error",message:e.message}); }
}

async function loadFeatures(){
  try{
    const j=await getJSON("/api/features"), voice=$("voice");
    if(!j.voice_transcription){
      voice.disabled=true;
      voice.parentElement.title="标准应用包支持语音播放；语音转文字需使用源码版安装可选依赖。";
      voice.parentElement.appendChild(document.createTextNode("（源码版可选）"));
    }
  }catch(_e){}
}

$("keySetupBtn").onclick=async()=>{
  const ok=confirm("首次设置会退出当前微信，并自动打开一个位于用户目录的临时签名副本。原始 /Applications/WeChat.app 不会被修改。继续吗？");
  if(!ok) return;
  try{ renderKeyStatus(await postJSON("/api/key-setup", {})); }
  catch(e){ renderKeyStatus({state:"error",message:e.message}); }
};

$("quitBtn").onclick=async()=>{
  if(!confirm("确定退出微信聊天记录导出工具吗？")) return;
  try{ await postJSON("/api/quit", {}); document.body.innerHTML="<p style='font:16px -apple-system;padding:40px'>工具已退出，可以关闭此页面。</p>"; }
  catch(e){ setStatus(e.message, true); }
};

async function loadAccounts(){
  try{
    const j = await getJSON("/api/accounts");
    const box = $("accounts"); box.innerHTML="";
    j.accounts.forEach((a,i)=>{
      const div=document.createElement("div"); div.className="acct";
      div.innerHTML = '<span class="pill">'+esc(a.version)+'</span><b>'+esc(a.account_id)+'</b>'+
                      ' · 消息库 '+a.message_dbs+' 个';
      div.style.cursor="pointer";
      div.onclick=()=>selectAccount(a.account_id, box, div);
      box.appendChild(div);
      if(i===0) selectAccount(a.account_id, box, div);
    });
  }catch(e){ $("accounts").innerHTML='<span class="err">'+esc(e.message)+'</span>'; }
}

function selectAccount(id, box, el){
  ACCOUNT=id;
  [...box.children].forEach(c=>c.style.outline="");
  if(el) el.style.outline="2px solid #07c160";
  loadContacts($("q").value || "");
}

async function loadContacts(q){
  const box=$("contacts");
  CONTACT=null; CONTACT_NAME=""; $("loadBtn").disabled=true; $("exportBtn").disabled=true;
  box.innerHTML='<div class="muted" style="padding:10px">加载中（首次会解密数据库，请稍候）…</div>';
  try{
    const j = await getJSON("/api/contacts?account_id="+encodeURIComponent(ACCOUNT)+
                            "&kind="+encodeURIComponent(CONTACT_KIND)+
                            "&q="+encodeURIComponent(q||""));
    box.innerHTML="";
    if(!j.contacts.length){ box.innerHTML='<div class="muted" style="padding:10px">没有匹配的联系人。</div>'; }
    j.contacts.forEach(c=>{
      const div=document.createElement("div"); div.className="contact"; div.dataset.u=c.username;
      div.innerHTML = '<span><span class="nm">'+esc(c.display_name)+(c.is_group?' 👥':'')+
                      '</span><br><span class="id">'+esc(c.wechat_id)+'</span></span>'+
                      '<span class="cnt">'+c.message_count+'</span>';
      div.onclick=()=>{
        [...box.children].forEach(x=>x.classList.remove("sel"));
        div.classList.add("sel");
        CONTACT=c.username; CONTACT_NAME=c.display_name;
        $("loadBtn").disabled=false; $("exportBtn").disabled=true;
        setStatus("已选择: "+c.display_name+"（点击『预览 / 加载消息』）");
      };
      box.appendChild(div);
    });
    $("contactHint").textContent = j.truncated ? ("仅显示前 "+j.contacts.length+" / "+j.total+" 条，输入更精确的关键词缩小范围。") : "";
  }catch(e){
    const msg = e.message || "";
    if(msg.includes("获取密钥失败")){
      box.innerHTML='<div class="err" style="padding:10px">数据库密钥尚未就绪。请点击页面顶部的“首次设置”，完成后这里会自动刷新好友和群聊。</div>';
      $("contactHint").textContent = "设置只处理本机数据，不会上传聊天内容。";
    }else{
      box.innerHTML='<div class="err" style="padding:10px">'+esc(msg)+'</div>';
    }
  }
}

$("kindSeg").onclick = (ev)=>{
  const btn = ev.target.closest("button[data-kind]");
  if(!btn) return;
  CONTACT_KIND = btn.dataset.kind;
  [...$("kindSeg").children].forEach(x=>x.classList.remove("sel"));
  btn.classList.add("sel");
  if(ACCOUNT) loadContacts($("q").value || "");
};

function fmtsSelected(){ return [...document.querySelectorAll(".fmt:checked")].map(c=>c.value); }

function renderPreview(){
  const box=$("preview"); box.innerHTML="";
  if(!MESSAGES.length){ box.innerHTML='<div class="muted" style="padding:20px">（没有消息）</div>'; return; }
  let lastDate=null;
  MESSAGES.forEach(m=>{
    if(m.date_key!==lastDate){ lastDate=m.date_key;
      const d=document.createElement("div"); d.className="datebar"; d.textContent=m.date_key; box.appendChild(d); }
    const row=document.createElement("div");
    row.className="msg"+(m.is_sender?" me":"")+(EXCLUDED.has(m.id)?" excluded":"");
    // avatar
    let av;
    if(m.avatar_url){ av=document.createElement("img"); av.className="av"; av.src=m.avatar_url; }
    else { av=document.createElement("div"); av.className="av"; av.textContent=(m.sender_name||"?").slice(0,1); }
    // body
    const body=document.createElement("div"); body.className="body";
    if(!m.is_sender){ const nm=document.createElement("div"); nm.className="nm2"; nm.textContent=m.sender_name; body.appendChild(nm); }
    const bub=document.createElement("div"); bub.className="bubble";
    const del=document.createElement("span"); del.className="del"; del.textContent="✕ 删除";
    del.onclick=(e)=>{ e.stopPropagation(); if(EXCLUDED.has(m.id))EXCLUDED.delete(m.id); else EXCLUDED.add(m.id); renderPreview(); };
    bub.appendChild(del);
    if(m.image_url){ const im=document.createElement("img"); im.className="pic"; im.src=m.image_url; bub.appendChild(im); }
    else if(m.voice_url){ const au=document.createElement("audio"); au.controls=true; au.src=m.voice_url; bub.appendChild(au);
      const lab=document.createElement("span"); lab.className="tag"; lab.textContent=" 🎤 语音"+(m.voice_seconds?(" "+m.voice_seconds+'″'):""); bub.appendChild(lab); }
    else if(m.kind==="text"){ const t=document.createElement("span"); t.textContent=m.text; bub.appendChild(t); }
    else { const t=document.createElement("span"); t.className="tag";
      t.textContent="["+m.kind_label+"]"+(m.text&&m.text!=="["+m.kind_label+"]"?(" "+m.text):""); bub.appendChild(t); }
    if(m.voice_text){ const v=document.createElement("div"); v.className="vt"; v.textContent="🗣 "+m.voice_text; bub.appendChild(v); }
    body.appendChild(bub);
    const meta=document.createElement("div"); meta.className="meta"; meta.textContent=m.time_str; body.appendChild(meta);
    row.appendChild(av); row.appendChild(body); box.appendChild(row);
  });
}

$("q").addEventListener("keydown", e=>{ if(e.key==="Enter"){ if(ACCOUNT) loadContacts($("q").value); }});

$("loadBtn").onclick = async ()=>{
  if(!CONTACT) return;
  EXCLUDED.clear();
  setStatus("正在加载消息…（图片/语音也在解析，可能稍慢）");
  $("loadBtn").disabled=true; $("exportBtn").disabled=true; $("files").innerHTML="";
  const url="/api/preview?account_id="+encodeURIComponent(ACCOUNT)+"&username="+encodeURIComponent(CONTACT)+
            "&start="+encodeURIComponent($("start").value)+"&end="+encodeURIComponent($("end").value)+
            "&voice="+($("voice").checked?"1":"0");
  try{
    const j=await getJSON(url);
    MESSAGES=j.messages; renderPreview();
    setStatus("已加载 "+j.count+" 条消息。可勾选格式后点「导出」。");
    $("exportBtn").disabled=false;
  }catch(e){ setStatus(e.message, true); }
  $("loadBtn").disabled=false;
};

$("exportBtn").onclick = async ()=>{
  if(!CONTACT) return;
  const formats=fmtsSelected();
  if(!formats.length){ setStatus("请至少勾选一种格式。", true); return; }
  setStatus("正在导出"+($("voice").checked?"（含语音转写，较慢）":"")+"…");
  $("exportBtn").disabled=true;
  try{
    const j=await postJSON("/api/export", {
      account_id:ACCOUNT, username:CONTACT, start:$("start").value, end:$("end").value,
      formats:formats, excluded:[...EXCLUDED], voice:$("voice").checked, density:$("density").value
    });
    const f=$("files"); f.innerHTML="";
    const head=document.createElement("div"); head.textContent="已导出 "+j.count+" 条到: "+j.out_dir; f.appendChild(head);
    j.files.forEach(file=>{ const a=document.createElement("a"); a.href=file.url; a.textContent="⬇ "+file.name; a.setAttribute("download",""); f.appendChild(a); });
    if(j.errors&&j.errors.length){ const e=document.createElement("div"); e.className="err"; e.textContent=j.errors.join("\n"); f.appendChild(e); }
    setStatus("导出完成。");
  }catch(e){ setStatus(e.message, true); }
  $("exportBtn").disabled=false;
};

loadKeyStatus();
loadFeatures();
loadAccounts();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Server bootstrap
# --------------------------------------------------------------------------- #
def _pick_port(preferred: int = 8765) -> int:
    """Return ``preferred`` if free, otherwise an OS-assigned free port."""
    for port in (preferred,):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _console_app_url(url: str) -> None:
    """Show a native alert when a frozen app cannot open the browser itself."""
    script = ('display dialog "微信聊天记录导出已启动。\\n\\n地址：%s" '
              'buttons {"退出", "打开浏览器"} default button "打开浏览器" '
              'with title "微信聊天记录导出"') % url
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script], capture_output=True, text=True, check=False
    )
    if "打开浏览器" in result.stdout:
        subprocess.run(["/usr/bin/open", url], check=False)


def main() -> None:
    port = _pick_port(8765)
    url = "http://127.0.0.1:%d" % port
    print("=" * 60)
    print(" 微信聊天记录导出 · 本地图形界面")
    print(" 打开:  " + url)
    print(" 全程只读 · 仅监听本机 · 仅用于导出你自己的聊天记录")
    print(" 按 Ctrl+C 退出")
    print("=" * 60)

    # Open the browser shortly after the server starts listening. A small native
    # dialog keeps the packaged .app visible even if the default browser launch
    # is delayed or blocked.
    def _open():
        time.sleep(1.0)
        try:
            webbrowser.open(url)
        except Exception:
            pass
        if getattr(sys, "frozen", False):
            _console_app_url(url)
    threading.Thread(target=_open, daemon=True).start()

    # Threaded so media/preview requests don't block each other. Reloader off so
    # the browser-open thread and module globals behave deterministically.
    app.run(host="127.0.0.1", port=port, threaded=True,
            debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
