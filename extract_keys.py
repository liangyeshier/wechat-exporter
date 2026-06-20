#!/usr/bin/env python3
"""独立的密钥提取脚本（仅用 Python 标准库）。

可以用任意一个 Python（包括系统自带的 /usr/bin/python3 或虚拟环境的「基础」
Python）在 sudo 下运行，**不依赖本项目的虚拟环境 .venv**——这样就避开了
「sudo 执行 .venv/bin/python 报 command not found」的坑。

功能：内存扫描提取 SQLCipher 数据库密钥 + 离线推导图片密钥，写入
~/.wechat_exporter/keys.json，并把文件归属交还给当前用户。

用法（通常由 启动.command 自动调用）：
    sudo /usr/bin/python3  extract_keys.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _chown_back() -> None:
    """在 sudo 下运行时，把生成的缓存交还给原始用户（而非 root）。"""
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    su = os.environ.get("SUDO_USER")
    if not su:
        return
    try:
        import pwd
        pw = pwd.getpwnam(su)
    except (KeyError, ImportError):
        return
    from core import constants
    base = constants.APP_DIR
    for b, dirs, files in os.walk(base):
        for name in [b] + [os.path.join(b, x) for x in dirs + files]:
            try:
                os.chown(name, pw.pw_uid, pw.pw_gid)
            except OSError:
                pass


def main() -> int:
    try:
        from core import key_extractor, locator, image_dat
    except Exception as e:  # pragma: no cover
        print("✗ 无法加载核心模块:", e)
        return 1

    pid = key_extractor.find_wechat_pid()
    if pid:
        print("检测到微信进程 pid", pid)
    else:
        print("! 未检测到微信进程；内存扫描需要微信处于运行状态。")

    try:
        keys = key_extractor.get_keys(method="auto", manual=None,
                                      use_cache=True, save=True)
    except key_extractor.KeyExtractionError as e:
        print("\n✗ 提取数据库密钥失败：\n" + str(e))
        _chown_back()
        return 2

    n = len([k for k in keys if not k.startswith("_")])
    print("✓ 已提取并缓存 %d 个数据库密钥" % n)

    # 图片密钥：从 uin + wxid 离线推导（无需内存扫描）。
    try:
        layout = locator.select_account()
        ik, xb = image_dat.derive_image_key(layout)
        if ik:
            d = key_extractor.load_cached_keys()
            d["_image_aes"] = ik
            if xb is not None:
                d["_image_xor"] = str(xb)
            key_extractor.save_keys(d)
            print("✓ 已推导并缓存图片密钥")
        else:
            print("! 暂未推导出图片密钥（导出图片时会再尝试）")
    except Exception as e:
        print("! 图片密钥步骤跳过:", e)

    _chown_back()
    return 0


if __name__ == "__main__":
    sys.exit(main())
