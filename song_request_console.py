"""Dedicated console session for song requests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, TextIO

import argparse
import contextlib
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from uuid import uuid4

try:
    import qrcode
except ImportError:  # pragma: no cover - optional dependency
    qrcode = None

from .config import SongRequestConfig

QR_URL_PATTERN = re.compile(r"(https://163cn\.tv/\S+)")


class SongRequestConsoleSession:
    """Owns one interactive console window, log file, state file, and command queue."""

    def __init__(
        self,
        settings: SongRequestConfig,
        *,
        base_dir: Path | str | None = None,
        parent_logger: Any = None,
    ) -> None:
        self.settings = settings
        self.base_dir = Path(base_dir or Path.cwd()).expanduser().resolve()
        self.parent_logger = parent_logger
        self.console_dir: Path | None = None
        self.log_path: Path | None = None
        self.state_path: Path | None = None
        self.command_dir: Path | None = None
        self._file: TextIO | None = None
        self._lock = threading.RLock()
        self._window_process: subprocess.Popen[Any] | None = None
        self._window_started = False
        self._state: dict[str, Any] = {}

    def start(self) -> "SongRequestConsoleSession":
        if not self.settings.console_enabled or self._file is not None:
            return self
        self.console_dir = _resolve_console_dir(self.settings, self.base_dir)
        self.console_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = self.console_dir / f"song-request-{timestamp}-{os.getpid()}.log"
        self.state_path = self.console_dir / "console-state.json"
        self.command_dir = self.console_dir / "commands"
        self.command_dir.mkdir(parents=True, exist_ok=True)
        self._file = self.log_path.open("a", encoding="utf-8", buffering=1)
        self._state = {
            "service_active": False,
            "pending_count": 0,
            "playback_active": False,
            "current_request_id": "",
            "current_song_title": "",
            "current_song_keyword": "",
            "last_error": "",
            "last_event": "",
            "last_qr_url": "",
            "log_path": str(self.log_path),
            "command_dir": str(self.command_dir),
            "window_title": self.settings.console_window_title,
        }
        self.update_state({})
        self.info("Song request console session started")
        self.info(f"log_path={self.log_path}")
        self.info(f"state_path={self.state_path}")
        self.info(f"command_dir={self.command_dir}")
        if self.settings.console_open_window:
            self.open_window()
        return self

    def stop(self) -> None:
        with self._lock:
            if self._file is None:
                return
            self.update_state({"service_active": False, "last_event": "console session stopped"})
            self.info("Song request console session stopping")
            self._file.close()
            self._file = None

    def open_window(self) -> None:
        if self._window_started or self.log_path is None or self.state_path is None or self.command_dir is None:
            return
        self._window_started = True
        if sys.platform != "win32":
            self.warning("Song request console window is only supported on Windows.")
            return
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        module_root = Path(__file__).resolve().parents[2]
        args = [
            sys.executable,
            "-m",
            "plugins.maibot_bilibili_live_adapter_copy.song_request_console",
            "--window",
            "--log-path",
            str(self.log_path),
            "--state-path",
            str(self.state_path),
            "--command-dir",
            str(self.command_dir),
            "--title",
            self.settings.console_window_title,
        ]
        try:
            self._window_process = subprocess.Popen(
                args,
                cwd=str(module_root),
                creationflags=creationflags,
                close_fds=True,
            )
            self.info(f"Song request console window opened: pid={getattr(self._window_process, 'pid', '')}")
        except Exception as exc:
            self.exception(f"Failed to open song request console window: {exc}")

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

    def update_state(self, updates: Mapping[str, Any]) -> None:
        if not self.settings.console_enabled or self.state_path is None:
            return
        with self._lock:
            if updates:
                self._state.update({str(key): value for key, value in updates.items()})
            self._state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            payload = json.dumps(self._state, ensure_ascii=False, indent=2)
            temp_path = self.state_path.with_suffix(".tmp")
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.replace(self.state_path)

    def enqueue_command(self, command: str, **payload: Any) -> Path | None:
        if not self.settings.console_enabled or self.command_dir is None:
            return None
        command_name = str(command or "").strip().lower()
        if not command_name:
            return None
        command_payload = {
            "id": uuid4().hex,
            "command": command_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **dict(payload or {}),
        }
        file_path = self.command_dir / f"{time.time_ns()}-{command_payload['id']}.json"
        file_path.write_text(json.dumps(command_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return file_path

    def consume_commands(self) -> list[dict[str, Any]]:
        if not self.settings.console_enabled or self.command_dir is None or not self.command_dir.exists():
            return []
        commands: list[dict[str, Any]] = []
        for path in sorted(self.command_dir.glob("*.json")):
            with contextlib.suppress(Exception):
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    commands.append(dict(payload))
            with contextlib.suppress(Exception):
                path.unlink()
        return commands

    def _write(self, level: str, message: str) -> None:
        if not self.settings.console_enabled:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = f"{timestamp} [{level}] {message}\n"
        with self._lock:
            if self._file is not None:
                self._file.write(line)
                self._file.flush()
        self._mirror_to_parent(level, message)
        qr_url = _extract_qr_url(message)
        state_update = {"last_event": message}
        if qr_url:
            state_update["last_qr_url"] = qr_url
        self.update_state(state_update)

    def _mirror_to_parent(self, level: str, message: str) -> None:
        parent_logger = self.parent_logger
        if parent_logger is None or parent_logger is self:
            return
        method_name = level.lower()
        method = getattr(parent_logger, method_name, None)
        if callable(method):
            with contextlib.suppress(Exception):
                method(f"[song_request] {message}")


def _resolve_console_dir(settings: SongRequestConfig, base_dir: Path) -> Path:
    configured = str(getattr(settings, "console_directory", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_absolute() else (base_dir / path).resolve()
    work_dir = str(getattr(settings, "work_dir", "") or "").strip()
    if work_dir:
        path = Path(work_dir).expanduser()
        resolved = path.resolve() if path.is_absolute() else (base_dir / path).resolve()
        return resolved / "_console"
    return (base_dir / "logs" / "song_request").resolve()


def _extract_qr_url(text: str) -> str:
    match = QR_URL_PATTERN.search(str(text or ""))
    return match.group(1) if match else ""


def _load_state(state_path: Path) -> dict[str, Any]:
    with contextlib.suppress(Exception):
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload
    return {}


def _set_console_title(title: str) -> None:
    normalized = str(title or "").strip() or "MaiBot Song Requests"
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetConsoleTitleW(normalized)
    else:
        print(f"\033]0;{normalized}\007", end="", flush=True)


def _print_help() -> None:
    print("Commands:")
    print("  help     Show this help")
    print("  status   Print the latest backend state snapshot")
    print("  login    Ask the backend to start a new NetEase QR login flow")
    print("  showqr   Reprint the latest QR login code from backend state")
    print("  clear    Clear the window")
    print("  exit     Close this console window")


def _print_status_snapshot(state: Mapping[str, Any]) -> None:
    if not state:
        print("No song-request state is available yet.")
        return
    print("Song-request state:")
    print(f"  service_active: {bool(state.get('service_active'))}")
    print(f"  pending_count: {int(state.get('pending_count') or 0)}")
    print(f"  playback_active: {bool(state.get('playback_active'))}")
    print(f"  current_song_title: {str(state.get('current_song_title') or '').strip() or '-'}")
    print(f"  current_song_keyword: {str(state.get('current_song_keyword') or '').strip() or '-'}")
    print(f"  current_request_id: {str(state.get('current_request_id') or '').strip() or '-'}")
    print(f"  last_error: {str(state.get('last_error') or '').strip() or '-'}")
    print(f"  last_event: {str(state.get('last_event') or '').strip() or '-'}")
    print(f"  updated_at: {str(state.get('updated_at') or '').strip() or '-'}")


def _print_qr_banner(url: str) -> None:
    normalized = str(url or "").strip()
    if not normalized:
        print("No QR login code is available yet.")
        return
    print("")
    print("NetEase QR login:")
    for line in _qr_ascii_lines(normalized):
        print(line)
    print(normalized)
    print("")


def _qr_ascii_lines(url: str) -> list[str]:
    normalized = str(url or "").strip()
    if not normalized:
        return []
    if qrcode is None:
        return ["[Install the optional 'qrcode' package to render an ASCII QR image here.]"]
    qr = qrcode.QRCode(border=1)
    qr.add_data(normalized)
    qr.make(fit=True)
    return ["".join("##" if cell else "  " for cell in row) for row in qr.get_matrix()]


def _tail_log_worker(log_path: Path, stop_event: threading.Event, print_lock: threading.Lock) -> None:
    position = 0
    while not stop_event.is_set():
        if not log_path.exists():
            stop_event.wait(0.25)
            continue
        with contextlib.suppress(Exception):
            with log_path.open("r", encoding="utf-8", errors="ignore") as file:
                if position == 0:
                    lines = file.readlines()
                    position = file.tell()
                    recent_lines = lines[-40:]
                    if recent_lines:
                        with print_lock:
                            for line in recent_lines:
                                print(line.rstrip())
                else:
                    file.seek(position)
                    lines = file.readlines()
                    position = file.tell()
                    if lines:
                        with print_lock:
                            for line in lines:
                                print(line.rstrip())
        stop_event.wait(0.25)


def _watch_state_worker(state_path: Path, stop_event: threading.Event, print_lock: threading.Lock) -> None:
    last_signature: tuple[Any, ...] | None = None
    last_qr_url = ""
    while not stop_event.is_set():
        state = _load_state(state_path)
        signature = (
            bool(state.get("service_active")),
            int(state.get("pending_count") or 0),
            bool(state.get("playback_active")),
            str(state.get("current_song_title") or ""),
            str(state.get("current_request_id") or ""),
            str(state.get("last_error") or ""),
        )
        if state and signature != last_signature:
            last_signature = signature
            with print_lock:
                print(
                    "[state]"
                    f" pending={int(state.get('pending_count') or 0)}"
                    f" playback={bool(state.get('playback_active'))}"
                    f" song={str(state.get('current_song_title') or '').strip() or '-'}"
                    f" request={str(state.get('current_request_id') or '').strip() or '-'}"
                )
        qr_url = str(state.get("last_qr_url") or "").strip()
        if qr_url and qr_url != last_qr_url:
            last_qr_url = qr_url
            with print_lock:
                _print_qr_banner(qr_url)
        stop_event.wait(0.5)


def run_song_request_console_window(
    *,
    log_path: Path,
    state_path: Path,
    command_dir: Path,
    title: str,
) -> int:
    _set_console_title(title)
    stop_event = threading.Event()
    print_lock = threading.Lock()
    print(f"{title}")
    print(f"log file: {log_path}")
    print(f"state file: {state_path}")
    print(f"command dir: {command_dir}")
    print("Type 'help' for commands.")
    _print_help()
    log_thread = threading.Thread(
        target=_tail_log_worker,
        args=(log_path, stop_event, print_lock),
        name="song_request_console.log_tail",
        daemon=True,
    )
    state_thread = threading.Thread(
        target=_watch_state_worker,
        args=(state_path, stop_event, print_lock),
        name="song_request_console.state_watch",
        daemon=True,
    )
    log_thread.start()
    state_thread.start()
    try:
        while True:
            try:
                raw = input("song> ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                print("")
                break
            if not raw:
                continue
            command = raw.lower()
            if command in {"help", "?"}:
                _print_help()
            elif command in {"exit", "quit"}:
                break
            elif command == "status":
                _print_status_snapshot(_load_state(state_path))
            elif command == "showqr":
                _print_qr_banner(str(_load_state(state_path).get("last_qr_url") or "").strip())
            elif command == "login":
                command_path = command_dir / f"{time.time_ns()}-{uuid4().hex}.json"
                command_path.write_text(
                    json.dumps(
                        {
                            "id": uuid4().hex,
                            "command": "login",
                            "source": "console_window",
                            "created_at": datetime.now().isoformat(timespec="seconds"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print("Queued backend command: login")
            elif command == "clear":
                os.system("cls" if os.name == "nt" else "clear")
            else:
                print(f"Unknown command: {raw}")
                _print_help()
    finally:
        stop_event.set()
        log_thread.join(timeout=1.0)
        state_thread.join(timeout=1.0)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MaiBot song-request console window")
    parser.add_argument("--window", action="store_true", help="Run the interactive song-request console window")
    parser.add_argument("--log-path", default="", help="Absolute path to the song-request log file")
    parser.add_argument("--state-path", default="", help="Absolute path to the song-request state file")
    parser.add_argument("--command-dir", default="", help="Absolute path to the song-request command directory")
    parser.add_argument("--title", default="MaiBot Song Requests", help="Console window title")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    if not args.window:
        return 0
    return run_song_request_console_window(
        log_path=Path(args.log_path).expanduser().resolve(),
        state_path=Path(args.state_path).expanduser().resolve(),
        command_dir=Path(args.command_dir).expanduser().resolve(),
        title=str(args.title or "").strip() or "MaiBot Song Requests",
    )


if __name__ == "__main__":
    raise SystemExit(main())
