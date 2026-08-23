from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sys
from typing import Callable

from .extract_cli import run_extract
from .toolchain import prepare_toolchain


EXPECTED_DIRS = (
    "avatar",
    "chart",
    "illustration",
    "illustrationBlur",
    "illustrationLowRes",
    "metadata",
)
class _BinaryLogBuffer:
    """Binary companion for libraries that touch sys.stdout.buffer."""

    def __init__(self, text: "_LineLogWriter") -> None:
        self._text = text
        self.closed = False

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        return self._text.write(data.decode(self._text.encoding, errors=self._text.errors))

    def flush(self) -> None:
        self._text.flush()

    def isatty(self) -> bool:
        return False

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("'_BinaryLogBuffer' has no fileno")


class _LineLogWriter:
    """Forward newline-delimited stdout/stderr chunks to the UI log callback.

    Mimics enough of TextIO for phiTool's log.init_console_logger(), which may call
    reconfigure() or wrap stdout.buffer on Windows.
    """

    def __init__(self, log: Callable[[str], None] | None) -> None:
        self._log = log
        self._buf = ""
        self.encoding = "utf-8"
        self.errors = "replace"
        self.buffer = _BinaryLogBuffer(self)
        self.name = "<phigros-publisher-log>"
        self.closed = False

    def write(self, data: str) -> int:
        if not data:
            return 0
        self._buf += data
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            message = line.rstrip("\r")
            if message and self._log:
                self._log(message)
        return len(data)

    def flush(self) -> None:
        if self._buf and self._log:
            self._log(self._buf.rstrip("\r"))
            self._buf = ""

    def isatty(self) -> bool:
        return False

    def reconfigure(self, *, encoding: str | None = None, errors: str | None = None, **_kwargs) -> None:
        if encoding is not None:
            self.encoding = encoding
        if errors is not None:
            self.errors = errors

    def fileno(self) -> int:
        raise OSError("'_LineLogWriter' has no fileno")

    def readable(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False


def extract_resources(
    demo_root: Path,
    apk_path: Path,
    toolchain_dir: Path,
    log: Callable[[str], None] | None = None,
    music: bool = False,
) -> Path:
    phi_tool = prepare_toolchain(demo_root, toolchain_dir)
    output = phi_tool / "output"
    if output.exists():
        shutil.rmtree(output)

    script_dir = (phi_tool / "script-py").resolve()
    apk = apk_path.resolve()
    if not script_dir.is_dir():
        raise FileNotFoundError(f"解包脚本目录不存在：{script_dir}")
    if not apk.is_file():
        raise FileNotFoundError(f"APK 不存在：{apk}")

    # 必须在进程内执行：frozen exe 的 sys.executable 指向自身，且 __file__ 目录
    # 不能作为 subprocess cwd（会触发 WinError 267 目录名称无效）。
    writer = _LineLogWriter(log)
    previous_cwd = Path.cwd()
    previous_path = list(sys.path)
    try:
        with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
            run_extract(script_dir, apk, music=music)
    finally:
        writer.flush()
        try:
            os.chdir(previous_cwd)
        except OSError:
            pass
        sys.path[:] = previous_path

    missing = [name for name in EXPECTED_DIRS if not (output / name).is_dir()]
    if music and not (output / "music").is_dir():
        missing.append("music")
    if missing:
        raise RuntimeError(f"解包结果缺少目录：{', '.join(missing)}")
    return output
