"""Safe first-run key setup for current macOS WeChat releases.

The shipped WeChat bundle is never modified. We copy it into the user's
Application Support directory, ad-hoc sign that copy, start it under LLDB, and
capture the 32-byte passphrase passed to CommonCrypto. Per-database SQLCipher
keys are then derived locally from each database's own salt.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Callable, Dict, Iterable, Optional

from . import constants, db_decryptor, key_extractor, locator
from .app_paths import resource_path


class KeySetupError(Exception):
    pass


Progress = Optional[Callable[[str], None]]
SOURCE_APP = "/Applications/WeChat.app"
COPY_ROOT = os.path.join(constants.HOME, "Library", "Application Support", "WeChatExporter")
COPY_APP = os.path.join(COPY_ROOT, "WeChat-Readable.app")


def _progress(callback: Progress, message: str) -> None:
    if callback is not None:
        callback(message)


def _run(argv: Iterable[str], description: str, timeout: int = 600) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, check=False, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KeySetupError("%s失败：%s" % (description, exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "未知错误").strip()
        raise KeySetupError("%s失败：%s" % (description, detail[-1200:]))
    return result


def _source_fingerprint(app_path: str) -> str:
    executable = os.path.join(app_path, "Contents", "MacOS", "WeChat")
    stat = os.stat(executable)
    return "%s:%s" % (stat.st_size, stat.st_mtime_ns)


def _prepare_copy(progress: Progress) -> str:
    if not os.path.isdir(SOURCE_APP):
        raise KeySetupError("没有找到 /Applications/WeChat.app，请先安装 macOS 微信。")
    os.makedirs(COPY_ROOT, exist_ok=True)
    marker = os.path.join(COPY_ROOT, "source-version.txt")
    fingerprint = _source_fingerprint(SOURCE_APP)
    current = ""
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            current = handle.read().strip()
    except OSError:
        pass

    if current != fingerprint or not os.path.isdir(COPY_APP):
        _progress(progress, "正在复制微信到用户目录，原始应用不会被修改…")
        staging = COPY_APP + ".staging"
        if os.path.exists(staging):
            shutil.rmtree(staging, ignore_errors=True)
        _run(["/usr/bin/ditto", SOURCE_APP, staging], "复制微信")
        if os.path.exists(COPY_APP):
            shutil.rmtree(COPY_APP, ignore_errors=True)
        os.replace(staging, COPY_APP)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(fingerprint)

    _progress(progress, "正在对用户目录中的微信副本做临时签名…")
    _run(
        ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", COPY_APP],
        "签名微信副本",
    )
    return os.path.join(COPY_APP, "Contents", "MacOS", "WeChat")


def _stop_wechat(progress: Progress) -> None:
    _progress(progress, "正在退出微信，稍后会自动打开可读取的副本…")
    subprocess.run(
        ["/usr/bin/pkill", "-TERM", "-x", "WeChat"],
        capture_output=True,
        check=False,
    )
    deadline = time.time() + 12
    while time.time() < deadline:
        result = subprocess.run(
            ["/usr/bin/pgrep", "-x", "WeChat"], capture_output=True, check=False
        )
        if result.returncode != 0:
            return
        time.sleep(0.25)
    raise KeySetupError("微信没有完全退出，请在微信菜单中选择“退出微信”后重试。")


def _lldb_python_path() -> str:
    lldb = shutil.which("lldb") or "/usr/bin/lldb"
    result = _run([lldb, "-P"], "定位 LLDB Python 模块")
    path = result.stdout.strip().splitlines()
    if not path:
        raise KeySetupError("LLDB 没有返回 Python 模块路径。请安装 Xcode Command Line Tools。")
    return path[-1]


def _capture_passphrase(executable: str, progress: Progress) -> bytes:
    helper = resource_path("tools", "lldb_capture_passphrase.py")
    if not os.path.isfile(helper):
        raise KeySetupError("应用包中缺少密钥捕获助手。")

    fd, output = tempfile.mkstemp(prefix="wechat-key-", suffix=".json")
    os.close(fd)
    os.chmod(output, 0o600)
    env = os.environ.copy()
    env["PYTHONPATH"] = _lldb_python_path()
    command = [
        "/usr/bin/python3", helper, "--executable", executable,
        "--output", output, "--timeout", "180",
    ]
    _progress(progress, "正在等待微信启动并捕获数据库密钥…")
    capture = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=env)
    try:
        time.sleep(1.0)
        _run(["/usr/bin/open", "-n", COPY_APP], "打开微信副本", timeout=30)
        stdout, stderr = capture.communicate(timeout=210)
        if capture.returncode != 0:
            detail = (stderr or stdout or "未知错误").strip()
            raise KeySetupError("密钥捕获失败：%s" % detail[-1200:])
        with open(output, "r", encoding="utf-8") as handle:
            value = json.load(handle).get("passphrase", "")
        passphrase = bytes.fromhex(value)
        if len(passphrase) != constants.KEY_LEN:
            raise KeySetupError("捕获到的密钥材料长度不正确。")
        return passphrase
    except subprocess.TimeoutExpired as exc:
        capture.kill()
        capture.communicate()
        raise KeySetupError("等待微信数据库打开超时，请确认微信副本已正常登录。") from exc
    finally:
        try:
            os.remove(output)
        except OSError:
            pass


def _all_database_paths() -> Iterable[str]:
    seen = set()
    for layout in locator.discover_accounts():
        paths = list(layout.message_dbs)
        paths.extend([layout.contact_db, layout.session_db, layout.group_db])
        db_root = os.path.join(layout.account_dir, "db_storage")
        if layout.is_v4 and os.path.isdir(db_root):
            for base, _dirs, files in os.walk(db_root):
                paths.extend(os.path.join(base, name) for name in files if name.endswith(".db"))
        for path in paths:
            if path and path not in seen and os.path.isfile(path):
                seen.add(path)
                yield path


def derive_keys(passphrase: bytes, progress: Progress = None) -> Dict[str, str]:
    if len(passphrase) != constants.KEY_LEN:
        raise KeySetupError("数据库口令长度不正确。")
    keys: Dict[str, str] = {}
    paths = list(_all_database_paths())
    if not paths:
        raise KeySetupError("没有发现可读取的微信数据库，请先登录微信。")
    for index, path in enumerate(paths, start=1):
        salt = db_decryptor.read_salt(path)
        key = hashlib.pbkdf2_hmac("sha512", passphrase, salt, 256000, dklen=32)
        keys[salt.hex()] = key.hex()
        _progress(progress, "正在生成数据库密钥（%d/%d）…" % (index, len(paths)))
    return keys


def _verify(keys: Dict[str, str], progress: Progress) -> None:
    layouts = locator.discover_accounts()
    if not layouts:
        raise KeySetupError("没有发现微信账号。")
    layout = layouts[0]
    candidates = [layout.contact_db] + list(layout.message_dbs)
    source = next((path for path in candidates if path and os.path.isfile(path)), None)
    if source is None:
        raise KeySetupError("没有可用于校验的联系人或消息数据库。")
    salt = db_decryptor.read_salt(source).hex()
    key = keys.get(salt)
    if not key:
        raise KeySetupError("校验数据库缺少对应密钥。")
    _progress(progress, "正在校验密钥是否能读取聊天数据库…")
    verify_dir = tempfile.mkdtemp(prefix="wechat-key-verify-")
    try:
        db_decryptor.decrypt_database(
            source, key, layout.version, os.path.join(verify_dir, "verified.db")
        )
    except db_decryptor.DecryptError as exc:
        raise KeySetupError(str(exc)) from exc
    finally:
        shutil.rmtree(verify_dir, ignore_errors=True)


def setup(progress: Progress = None) -> Dict[str, str]:
    if sys_platform() != "darwin":
        raise KeySetupError("自动密钥设置目前仅支持 macOS。")
    executable = _prepare_copy(progress)
    _stop_wechat(progress)
    passphrase = _capture_passphrase(executable, progress)
    try:
        keys = derive_keys(passphrase, progress)
    finally:
        passphrase = b""
    _verify(keys, progress)
    key_extractor.save_keys(keys)
    try:
        os.chmod(constants.KEY_CACHE, 0o600)
    except OSError:
        pass
    _progress(progress, "密钥设置成功，正在刷新联系人和群聊…")
    return keys


def sys_platform() -> str:
    import sys
    return sys.platform
