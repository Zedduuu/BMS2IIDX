"""CLI 调用记录器：把每次外部命令 + 输出收集起来，给界面的"CLI"面板显示

不改变行为：只做记录。用 `run()` 包装 subprocess.run，用 `popen()` 包 Popen。
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time

_lock = threading.Lock()
_entries: list[dict] = []
_MAX = 400
_listener = None


def set_listener(fn):
    """界面注册回调：每新增一条就调用 fn(entry)"""
    global _listener
    _listener = fn


def _add(cmd, code, out, dur):
    entry = {
        "cmd": " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd),
        "code": code,
        "output": (out or "").strip(),
        "ms": int(dur * 1000),
        "at": time.strftime("%H:%M:%S"),
    }
    with _lock:
        _entries.append(entry)
        if len(_entries) > _MAX:
            del _entries[0:len(_entries) - _MAX]
    if _listener:
        try:
            _listener(entry)
        except Exception:                                      # noqa: BLE001
            pass


def entries() -> list[dict]:
    with _lock:
        return list(_entries)


def clear():
    with _lock:
        _entries.clear()


def _quiet(kw: dict) -> dict:
    """Windows 下别弹黑窗口（ffmpeg/ffprobe 之类一闪而过很烦）"""
    if sys.platform.startswith("win"):
        flags = kw.get("creationflags", 0) | 0x08000000      # CREATE_NO_WINDOW
        kw["creationflags"] = flags
        si = kw.get("startupinfo") or subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    return kw


def run(cmd, **kw):
    """等价 subprocess.run，但记录命令与输出

    调用方传 text=True 时，Python 会用系统 ANSI 编码（中文 Windows = GBK）解码子进程
    输出；ffmpeg/ffprobe 回显的路径一旦含非 GBK 字节（日文/emoji 文件名）就会抛
    UnicodeDecodeError。这里统一改成 utf-8 + errors="replace"。
    """
    if kw.get("text") or kw.get("universal_newlines"):
        kw.pop("text", None)
        kw.pop("universal_newlines", None)
        kw["encoding"] = "utf-8"
        kw["errors"] = "replace"
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, **_quiet(kw))
    dur = time.perf_counter() - t0
    out = b""
    for attr in ("stdout", "stderr"):
        v = getattr(proc, attr, None)
        if isinstance(v, bytes):
            out += v
        elif isinstance(v, str):
            out += v.encode("utf-8", "replace")
    _add(cmd, proc.returncode, out[-4000:].decode("utf-8", "replace"), dur)
    return proc


def popen(cmd, **kw):
    """等价 subprocess.Popen，只记录命令行（不接管输出）"""
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, **_quiet(kw))
    _add(cmd, None, f"（后台进程 pid={proc.pid}）", time.perf_counter() - t0)
    return proc
