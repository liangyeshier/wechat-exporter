#!/usr/bin/env python3
"""Capture WeChat 4.x's 32-byte WCDB passphrase with macOS LLDB.

This helper is intentionally standard-library-only apart from the LLDB module
provided by Xcode Command Line Tools. It writes the passphrase to a caller-owned
temporary file with mode 0600; the parent process derives database-specific
keys and immediately removes that file.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import lldb


def _read_memory(process, address: int, size: int) -> bytes:
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _process_command(pid: int) -> str:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _save(path: str, passphrase: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"passphrase": passphrase.hex()}, handle)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def capture(expected_executable: str, output: str, timeout: int) -> int:
    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(False)
    target = debugger.CreateTarget("")
    listener = debugger.GetListener()
    error = lldb.SBError()
    process = target.AttachToProcessWithName(listener, "WeChat", True, error)

    if not error.Success() or not process or not process.IsValid():
        print("无法附加到微信进程：%s" % (error.GetCString() or "unknown"), flush=True)
        return 2

    command = _process_command(process.GetProcessID())
    if not command.startswith(expected_executable):
        print("检测到了另一个微信进程，请先完全退出所有微信实例。", flush=True)
        try:
            process.Detach()
        except Exception:
            pass
        return 3

    for setting in (
        "settings set target.preload-symbols false",
        "process handle SIGSTOP -s false -p true -n false",
        "process handle SIGPIPE -s false -p true -n false",
    ):
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(setting, result)

    breakpoint = target.BreakpointCreateByName("CCKeyDerivationPBKDF")
    if not breakpoint.IsValid():
        print("无法创建系统密钥派生函数断点。", flush=True)
        process.Detach()
        return 4

    # CommonCrypto may not be mapped at the initial attach stop. LLDB keeps a
    # zero-location breakpoint pending and resolves it when the dylib loads.

    deadline = time.time() + timeout
    skipped = {}
    while time.time() < deadline and process.IsValid():
        continue_error = process.Continue()
        if continue_error.Fail():
            time.sleep(0.05)

        state = process.GetState()
        if state in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateCrashed):
            break

        stopped_thread = None
        breakpoint_thread = None
        for index in range(process.GetNumThreads()):
            thread = process.GetThreadAtIndex(index)
            reason = thread.GetStopReason()
            if reason == lldb.eStopReasonBreakpoint:
                name = thread.GetFrameAtIndex(0).GetFunctionName() or ""
                if "CCKeyDerivationPBKDF" in name:
                    breakpoint_thread = thread
                    break
            if stopped_thread is None and reason not in (
                lldb.eStopReasonNone,
                lldb.eStopReasonInvalid,
            ):
                stopped_thread = thread

        if breakpoint_thread is not None:
            frame = breakpoint_thread.GetFrameAtIndex(0)
            pointer = frame.FindRegister("x1").GetValueAsUnsigned()
            length = frame.FindRegister("x2").GetValueAsUnsigned()
            salt_length = frame.FindRegister("x4").GetValueAsUnsigned()
            rounds = frame.FindRegister("x6").GetValueAsUnsigned()
            if length == 32 and salt_length == 16 and rounds in (256000, 64000, 4000):
                passphrase = _read_memory(process, pointer, 32)
                if len(passphrase) == 32:
                    _save(output, passphrase)
                    process.Detach()
                    print("密钥材料已捕获。", flush=True)
                    return 0
            continue

        # Some signed App Store builds stop on an internal trap while attaching.
        # Advance only after the same stop repeats; this mirrors LLDB's normal
        # handling and lets the application continue to its database open.
        if stopped_thread is not None:
            frame = stopped_thread.GetFrameAtIndex(0)
            pc = frame.GetPC()
            key = (pc, stopped_thread.GetStopReason())
            skipped[key] = skipped.get(key, 0) + 1
            instruction = _read_memory(process, pc, 4)
            traps = (b"\x00\x00 \xd4", b"\x20\x00 \xd4", b"\x00\x00@\xd4")
            if instruction in traps or skipped[key] >= 3:
                frame.SetPC(pc + 4)

    try:
        process.Detach()
    except Exception:
        pass
    print("等待微信打开数据库超时。", flush=True)
    return 5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()
    return capture(os.path.realpath(args.executable), args.output, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
