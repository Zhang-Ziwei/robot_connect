"""
把 main.py 打到终端的 stdout/stderr 同时写入 logs/main_console.log，
供流程编辑器的「主程序输出」页实时查看。

不改各处 print / logger 调用：安装一次 Tee 即可覆盖本进程所有线程的 print。
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

_LOG_DIR = "logs"
_LOG_NAME = "main_console.log"
_PREV_NAME = "main_console.prev.log"
_ROTATE_BYTES = 12 * 1024 * 1024

_lock = threading.Lock()
_file: Optional[TextIO] = None
_installed = False


def console_log_path() -> str:
    """当前控制台日志的绝对路径（文件未必已经存在）。"""
    return os.path.abspath(os.path.join(_LOG_DIR, _LOG_NAME))


class _Tee:
    def __init__(self, original: TextIO, sink: TextIO, lock: threading.Lock):
        self._original = original
        self._sink = sink
        self._lock = lock

    def write(self, data):
        if not data:
            return 0
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        with self._lock:
            try:
                self._original.write(data)
                self._original.flush()
            except Exception:
                pass
            try:
                self._sink.write(data)
                self._sink.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        with self._lock:
            try:
                self._original.flush()
            except Exception:
                pass
            try:
                self._sink.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._original.fileno()

    def __getattr__(self, name):
        return getattr(self._original, name)


def start_console_capture() -> str:
    """
    把 sys.stdout / sys.stderr 复制一份到 logs/main_console.log。
    可重复调用，第二次是空操作。返回日志路径。
    """
    global _file, _installed
    if _installed:
        return console_log_path()

    Path(_LOG_DIR).mkdir(exist_ok=True)
    path = console_log_path()
    if os.path.isfile(path) and os.path.getsize(path) >= _ROTATE_BYTES:
        prev = os.path.join(os.path.dirname(path), _PREV_NAME)
        try:
            if os.path.isfile(prev):
                os.remove(prev)
            os.replace(path, prev)
        except OSError:
            pass

    _file = open(path, "a", encoding="utf-8", buffering=1)
    banner = (
        f"\n{'=' * 72}\n"
        f"主程序控制台  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  pid={os.getpid()}\n"
        f"{'=' * 72}\n"
    )
    _file.write(banner)
    _file.flush()

    sys.stdout = _Tee(sys.__stdout__, _file, _lock)
    sys.stderr = _Tee(sys.__stderr__, _file, _lock)
    _installed = True
    return path
