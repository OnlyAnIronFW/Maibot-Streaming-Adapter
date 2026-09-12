"""Dedicated file logging for the STS2-Agent integration."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import os
import subprocess
import sys
import threading
import traceback

from .config import STS2LoggingConfig


class STS2LogSession:
    """Owns one STS2 log file and an optional tail window."""

    def __init__(
        self,
        config: STS2LoggingConfig,
        *,
        base_dir: Path | str | None = None,
        parent_logger: Any = None,
    ) -> None:
        self.config = config
        self.base_dir = Path(base_dir or Path.cwd()).expanduser().resolve()
        self.parent_logger = parent_logger
        self.log_path: Path | None = None
        self._file: TextIO | None = None
        self._lock = threading.RLock()
        self._window_process: subprocess.Popen[Any] | None = None
        self._window_started = False

    @property
    def stderr(self) -> TextIO | None:
        if not self.config.capture_mcp_stderr:
            return None
        return self._file

    def start(self) -> "STS2LogSession":
        if not self.config.enabled or self._file is not None:
            return self
        log_dir = _resolve_log_dir(self.config.directory, self.base_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = log_dir / f"{self.config.file_prefix}-{timestamp}-{os.getpid()}.log"
        self._file = self.log_path.open("a", encoding="utf-8", buffering=1)
        self.info("STS2 log session started")
        self.info(f"log_path={self.log_path}")
        self.info(f"tail_lines={self.config.tail_lines}")
        if self.config.open_window:
            self.open_window()
        return self

    def stop(self) -> None:
        with self._lock:
            if self._file is None:
                return
            self.info("STS2 log session stopping")
            self._file.close()
            self._file = None

    def open_window(self) -> None:
        if self._window_started or self.log_path is None:
            return
        self._window_started = True
        if sys.platform != "win32":
            self.warning("STS2 log tail window is only supported on Windows.")
            return
        command = _build_tail_command(self.log_path, self.config.tail_lines, self.config.window_title)
        args = [
            "powershell.exe",
            "-NoExit",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        try:
            self._window_process = subprocess.Popen(
                args,
                cwd=str(self.log_path.parent),
                creationflags=creationflags,
                close_fds=True,
            )
            self.info(f"STS2 log tail window opened: pid={getattr(self._window_process, 'pid', '')}")
        except Exception as exc:
            self.exception(f"Failed to open STS2 log tail window: {exc}")

    def debug(self, message: str) -> None:
        self._write("DEBUG", message)

    def info(self, message: str) -> None:
        self._write("INFO", message)

    def warning(self, message: str) -> None:
        self._write("WARNING", message)

    def error(self, message: str) -> None:
        self._write("ERROR", message)

    def exception(self, message: str) -> None:
        detail = traceback.format_exc()
        if detail and detail.strip() != "NoneType: None":
            self._write("ERROR", f"{message}\n{detail.rstrip()}")
        else:
            self._write("ERROR", message)

    def _write(self, level: str, message: str) -> None:
        if not self.config.enabled:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"{timestamp} [{level}] {message}\n"
        with self._lock:
            if self._file is not None:
                self._file.write(line)
                self._file.flush()


def _resolve_log_dir(raw_path: str, base_dir: Path) -> Path:
    normalized = str(raw_path or "").strip() or "logs/sts2agent"
    path = Path(normalized).expanduser()
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()


def _build_tail_command(log_path: Path, tail_lines: int, title: str) -> str:
    quoted_path = _ps_single_quote(str(log_path))
    quoted_title = _ps_single_quote(str(title or "MaiBot STS2-Agent Logs"))
    safe_tail_lines = max(1, int(tail_lines or 200))
    return (
        "$ErrorActionPreference = 'Continue'; "
        "chcp.com 65001 > $null; "
        "$utf8 = New-Object System.Text.UTF8Encoding -ArgumentList $false; "
        "[Console]::InputEncoding = $utf8; "
        "[Console]::OutputEncoding = $utf8; "
        "$OutputEncoding = $utf8; "
        f"$Host.UI.RawUI.WindowTitle = {quoted_title}; "
        f"$path = {quoted_path}; "
        "Write-Host ('STS2-Agent log: ' + $path); "
        "Write-Host 'Close this window when you no longer need live STS2 logs.'; "
        f"Get-Content -LiteralPath $path -Encoding UTF8 -Tail {safe_tail_lines} -Wait"
    )


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
