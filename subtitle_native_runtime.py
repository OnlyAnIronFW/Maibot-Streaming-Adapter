"""Native desktop subtitle runtime for the safe-copy Bilibili adapter."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence, TypeAlias

import contextlib
import queue
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk

    TK_AVAILABLE = True
except Exception:  # pragma: no cover - tkinter can be missing in test environments
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    TK_AVAILABLE = False

from .subtitle_native_segments import (
    RuntimeSubtitleSegment,
    compute_track_overflow_px,
    normalize_runtime_segment,
    reveal_bilingual_text,
)
from .live2d_control_state import DEFAULT_LIVE2D_CONTROL_STATE, Live2DControlState
from .subtitle_native_state import (
    DEFAULT_SUBTITLE_UI_SETTINGS,
    SubtitleUISettingsStore,
    blend_hex_colors,
    normalize_subtitle_ui_settings,
    select_tk_font_family,
)


ControlStateChangedCallback: TypeAlias = Callable[[Mapping[str, Any]], None]
ShellActionRequestedCallback: TypeAlias = Callable[[str], None]


@dataclass
class _SubtitleEntryWidgets:
    """Widget bundle for one rendered subtitle segment."""

    container: Any
    english_label: Any | None
    chinese_label: Any
    english_text: str
    chinese_text: str


class SubtitleNativeUIRuntime:
    """Hosts the native subtitle controller and overlay windows on a dedicated thread."""

    _POLL_INTERVAL_MS = 33
    _CONTROL_WINDOW_WIDTH = 460
    _CONTROL_WINDOW_HEIGHT = 720
    _OVERLAY_BASE_COLOR = "#10161f"
    _ENTRY_HORIZONTAL_PADDING = 18
    _ENTRY_VERTICAL_PADDING = 18
    _ENTRY_SPACING_PX = 10
    _SCROLL_TICK_MS = 16

    def __init__(
        self,
        *,
        settings_store: SubtitleUISettingsStore,
        logger: Any = None,
        on_audio_started: Callable[..., None] | None = None,
        control_state: Live2DControlState | Mapping[str, Any] | None = None,
        on_control_state_changed: ControlStateChangedCallback | None = None,
        shell_controls: Mapping[str, Any] | None = None,
        on_shell_action_requested: ShellActionRequestedCallback | None = None,
    ) -> None:
        self._settings_store = settings_store
        self._logger = logger
        self._on_audio_started = on_audio_started
        self._on_control_state_changed = self._normalize_control_state_callback(on_control_state_changed)
        self._on_shell_action_requested = self._normalize_shell_action_callback(on_shell_action_requested)
        self._control_state = self._normalize_control_state(control_state)
        self._shell_controls = self._normalize_shell_controls(shell_controls)
        self._settings = self._settings_store.load()
        self._pending_replies: deque[dict[str, Any]] = deque()
        self._pending_lock = threading.Lock()
        self._command_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ui_ready = threading.Event()
        self._stop_requested = threading.Event()
        self._startup_error: Exception | None = None

        self._root: Any = None
        self._overlay: Any = None
        self._subtitle_box: Any = None
        self._subtitle_canvas: Any = None
        self._subtitle_stack: Any = None
        self._subtitle_stack_window_id: Any = None
        self._status_var: Any = None
        self._reply_meta_var: Any = None
        self._font_family_var: Any = None
        self._font_size_var: Any = None
        self._text_color_var: Any = None
        self._background_color_var: Any = None
        self._background_opacity_var: Any = None
        self._box_width_var: Any = None
        self._box_height_var: Any = None
        self._left_var: Any = None
        self._bottom_var: Any = None
        self._mouse_follow_enabled_var: Any = None
        self._mouse_follow_status_var: Any = None
        self._shell_click_through_var: Any = None
        self._shell_interactive_var: Any = None
        self._shell_status_var: Any = None
        self._shell_controls_frame: Any = None
        self._shell_actions_container: Any = None

        self._subtitle_entries: list[_SubtitleEntryWidgets] = []
        self._current_reply: dict[str, Any] | None = None
        self._current_segment_index = 0
        self._segment_finish_after_id: str | None = None
        self._segment_reveal_after_id: str | None = None
        self._track_scroll_after_id: str | None = None
        self._track_scroll_current_px = 0.0
        self._track_scroll_target_px = 0.0

    def _normalize_control_state(
        self,
        control_state: Live2DControlState | Mapping[str, Any] | None,
    ) -> Live2DControlState:
        if isinstance(control_state, Live2DControlState):
            return control_state
        if control_state is None:
            return DEFAULT_LIVE2D_CONTROL_STATE
        normalized = Live2DControlState.from_dict(control_state)
        if normalized is not None:
            return normalized
        self._log_warning("Invalid control_state provided to SubtitleNativeUIRuntime; falling back to defaults.")
        return DEFAULT_LIVE2D_CONTROL_STATE

    def _normalize_control_state_callback(
        self,
        callback: ControlStateChangedCallback | None,
    ) -> ControlStateChangedCallback | None:
        if callback is None:
            return None
        if callable(callback):
            return callback
        self._log_warning(
            "Invalid on_control_state_changed callback provided to SubtitleNativeUIRuntime; ignoring callback."
        )
        return None

    def _normalize_shell_action_callback(
        self,
        callback: ShellActionRequestedCallback | None,
    ) -> ShellActionRequestedCallback | None:
        if callback is None:
            return None
        if callable(callback):
            return callback
        self._log_warning(
            "Invalid on_shell_action_requested callback provided to SubtitleNativeUIRuntime; ignoring callback."
        )
        return None

    def _normalize_shell_controls(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            return {"enabled": False, "click_through": True, "interactive": False, "actions": []}
        actions_payload = payload.get("actions")
        actions: list[str] = []
        if isinstance(actions_payload, Sequence) and not isinstance(actions_payload, (str, bytes, bytearray)):
            actions = [str(action).strip() for action in actions_payload if str(action).strip()]
        return {
            "enabled": bool(payload.get("enabled")),
            "click_through": bool(payload.get("click_through", True)),
            "interactive": bool(payload.get("interactive", False)),
            "actions": actions,
        }

    def _log_warning(self, message: str) -> None:
        logger = self._logger
        if logger is not None and hasattr(logger, "warning"):
            logger.warning(message)

    @property
    def is_running(self) -> bool:
        """Return whether the native subtitle UI thread is currently alive."""

        thread = self._thread
        return bool(self._ui_ready.is_set() and self._startup_error is None and thread is not None and thread.is_alive())

    @property
    def pending_reply_count(self) -> int:
        """Return the number of replies waiting to render."""

        with self._pending_lock:
            return len(self._pending_replies)

    def start(self) -> None:
        """Start the native subtitle UI thread."""

        if self.is_running:
            return
        if not TK_AVAILABLE:
            raise RuntimeError("tkinter is unavailable")
        self._stop_requested.clear()
        self._ui_ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run_ui_thread, name="MaiBotSubtitleNativeUI", daemon=True)
        self._thread.start()
        self._ui_ready.wait(timeout=5.0)
        if self._startup_error is not None:
            raise self._startup_error
        if not self._ui_ready.is_set():
            raise RuntimeError("native subtitle UI thread did not become ready in time")

    def stop(self) -> None:
        """Stop the native subtitle UI thread."""

        thread = self._thread
        if thread is None:
            return
        self._stop_requested.set()
        self._command_queue.put(("shutdown", {}))
        thread.join(timeout=3.0)
        self._thread = None
        self._ui_ready.clear()
        self.clear_pending_replies(clear_display=False)

    def enqueue_reply(self, payload: Mapping[str, Any]) -> None:
        """Push one reply payload into the native UI queue."""

        with self._pending_lock:
            self._pending_replies.append(dict(payload))
        self._command_queue.put(("wake", {}))

    def clear_pending_replies(self, *, clear_display: bool = True) -> None:
        """Clear waiting replies and optionally clear the current display."""

        with self._pending_lock:
            self._pending_replies.clear()
        if clear_display:
            self._command_queue.put(("clear_display", {}))

    def update_settings(self, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Update and persist UI settings."""

        normalized = self._settings_store.save({**self._settings, **dict(patch)})
        self._settings = normalized
        self._command_queue.put(("apply_settings", dict(normalized)))
        return dict(normalized)

    def update_shell_controls(self, payload: Mapping[str, Any] | None) -> dict[str, Any]:
        """Update the shell-control panel state shown in the native control window."""

        normalized = self._normalize_shell_controls(payload)
        self._shell_controls = normalized
        self._command_queue.put(("apply_shell_controls", dict(normalized)))
        return dict(normalized)

    def reset_settings(self) -> dict[str, Any]:
        """Reset UI settings back to defaults."""

        return self.update_settings(DEFAULT_SUBTITLE_UI_SETTINGS)

    def show_windows(self) -> None:
        """Bring the control and subtitle overlay windows to the foreground."""

        self._command_queue.put(("show_windows", {}))

    def _run_ui_thread(self) -> None:
        try:
            self._build_windows()
        except Exception as exc:  # pragma: no cover - UI startup failures are environment-specific
            self._startup_error = exc
            self._ui_ready.set()
            return
        self._ui_ready.set()
        assert self._root is not None
        self._root.after(self._POLL_INTERVAL_MS, self._poll_ui_commands)
        with contextlib.suppress(Exception):
            self._root.mainloop()

    def _build_windows(self) -> None:
        assert tk is not None
        assert ttk is not None
        self._root = tk.Tk()
        self._root.title("MaiBot Subtitle Control")
        self._root.geometry(f"{self._CONTROL_WINDOW_WIDTH}x{self._CONTROL_WINDOW_HEIGHT}+80+80")
        self._root.configure(background="#10161f")
        self._root.protocol("WM_DELETE_WINDOW", self._minimize_all_windows)

        self._overlay = tk.Toplevel(self._root)
        self._overlay.title("MaiBot Subtitle Overlay")
        self._overlay.configure(background=self._OVERLAY_BASE_COLOR)
        self._overlay.resizable(False, False)
        self._overlay.protocol("WM_DELETE_WINDOW", self._minimize_all_windows)
        screen_width = self._overlay.winfo_screenwidth()
        screen_height = self._overlay.winfo_screenheight()
        self._overlay.geometry(
            build_overlay_window_geometry(self._settings, screen_width=screen_width, screen_height=screen_height)
        )

        self._subtitle_box = tk.Frame(self._overlay, background=self._OVERLAY_BASE_COLOR, bd=0, highlightthickness=0)
        self._subtitle_box.place(x=0, y=0, width=1, height=1)
        self._subtitle_canvas = tk.Canvas(
            self._subtitle_box,
            background=self._OVERLAY_BASE_COLOR,
            bd=0,
            highlightthickness=0,
        )
        self._subtitle_canvas.pack(fill=tk.BOTH, expand=True)
        self._subtitle_stack = tk.Frame(self._subtitle_canvas, background=self._OVERLAY_BASE_COLOR, bd=0, highlightthickness=0)
        self._subtitle_stack_window_id = self._subtitle_canvas.create_window(
            self._ENTRY_HORIZONTAL_PADDING,
            self._ENTRY_VERTICAL_PADDING,
            anchor="nw",
            window=self._subtitle_stack,
        )

        self._status_var = tk.StringVar(master=self._root, value="Native subtitle UI is running")
        self._reply_meta_var = tk.StringVar(master=self._root, value="Waiting for the next reply")
        self._font_family_var = tk.StringVar(master=self._root)
        self._font_size_var = tk.StringVar(master=self._root)
        self._text_color_var = tk.StringVar(master=self._root)
        self._background_color_var = tk.StringVar(master=self._root)
        self._background_opacity_var = tk.IntVar(master=self._root)
        self._box_width_var = tk.StringVar(master=self._root)
        self._box_height_var = tk.StringVar(master=self._root)
        self._left_var = tk.StringVar(master=self._root)
        self._bottom_var = tk.StringVar(master=self._root)
        self._mouse_follow_enabled_var = tk.BooleanVar(master=self._root, value=self._control_state.mouse_follow_enabled)
        self._mouse_follow_status_var = tk.StringVar(
            master=self._root,
            value=self._live2d_control_status_text(self._control_state.mouse_follow_status),
        )
        self._shell_click_through_var = tk.StringVar(master=self._root)
        self._shell_interactive_var = tk.StringVar(master=self._root)
        self._shell_status_var = tk.StringVar(master=self._root)

        style = ttk.Style(self._root)
        with contextlib.suppress(Exception):
            style.theme_use("clam")

        wrapper = ttk.Frame(self._root, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)

        ttk.Label(wrapper, text="MaiBot Subtitle Control", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(wrapper, textvariable=self._status_var).pack(anchor="w", pady=(6, 0))
        ttk.Label(wrapper, textvariable=self._reply_meta_var, wraplength=400).pack(anchor="w", pady=(6, 12))

        notebook = ttk.Notebook(wrapper)
        notebook.pack(fill=tk.BOTH, expand=True)

        layout_tab = ttk.Frame(notebook, padding=12)
        text_tab = ttk.Frame(notebook, padding=12)
        background_tab = ttk.Frame(notebook, padding=12)
        system_tab = ttk.Frame(notebook, padding=12)
        notebook.add(layout_tab, text="Layout")
        notebook.add(text_tab, text="Text")
        notebook.add(background_tab, text="Background")
        notebook.add(system_tab, text="System")

        self._build_layout_tab(layout_tab)
        self._build_text_tab(text_tab)
        self._build_background_tab(background_tab)
        self._build_system_tab(system_tab)

        self._sync_control_vars(self._settings)
        self._apply_settings_to_widgets(self._settings)
        self._show_windows()

    def _build_layout_tab(self, parent: Any) -> None:
        assert ttk is not None
        self._build_labeled_entry(parent, "Width", self._box_width_var)
        self._build_labeled_entry(parent, "Height", self._box_height_var)
        self._build_labeled_entry(parent, "Left", self._left_var)
        self._build_labeled_entry(parent, "Bottom", self._bottom_var)
        ttk.Button(parent, text="Apply layout", command=self._apply_control_values).pack(anchor="w", pady=(12, 0))

    def _build_text_tab(self, parent: Any) -> None:
        assert ttk is not None
        self._build_labeled_entry(parent, "Font family", self._font_family_var)
        self._build_labeled_entry(parent, "Font size", self._font_size_var)
        self._build_labeled_entry(parent, "Text color", self._text_color_var)
        ttk.Button(parent, text="Apply text", command=self._apply_control_values).pack(anchor="w", pady=(12, 0))

    def _build_background_tab(self, parent: Any) -> None:
        assert ttk is not None
        self._build_labeled_entry(parent, "Background color", self._background_color_var)
        ttk.Label(parent, text="Background opacity").pack(anchor="w")
        opacity_scale = ttk.Scale(
            parent,
            from_=0,
            to=100,
            orient="horizontal",
            command=lambda value: self._background_opacity_var.set(int(float(value))),
        )
        opacity_scale.pack(fill=tk.X, pady=(4, 8))
        opacity_scale.configure(value=self._settings["background_opacity"])
        ttk.Label(parent, textvariable=self._background_opacity_var).pack(anchor="w")
        ttk.Button(parent, text="Apply background", command=self._apply_control_values).pack(anchor="w", pady=(12, 0))

    def _build_system_tab(self, parent: Any) -> None:
        assert ttk is not None
        ttk.Checkbutton(
            parent,
            text="Mouse Follow",
            variable=self._mouse_follow_enabled_var,
            command=lambda: self._handle_mouse_follow_toggle(bool(self._mouse_follow_enabled_var.get())),
        ).pack(anchor="w", pady=(0, 8))
        ttk.Label(parent, textvariable=self._mouse_follow_status_var, wraplength=360).pack(anchor="w", pady=(0, 12))
        ttk.Button(parent, text="Clear subtitles", command=self._clear_display).pack(fill=tk.X, pady=(0, 8))
        ttk.Button(parent, text="Reset settings", command=self._reset_from_ui).pack(fill=tk.X, pady=(0, 8))
        ttk.Button(parent, text="Show subtitle windows", command=self._show_windows).pack(fill=tk.X, pady=(0, 8))
        ttk.Button(parent, text="Minimize subtitle windows", command=self._minimize_all_windows).pack(fill=tk.X)
        self._shell_controls_frame = ttk.LabelFrame(parent, text="SoulLink Shell", padding=10)
        self._shell_controls_frame.pack(fill=tk.X, pady=(16, 0))
        ttk.Label(self._shell_controls_frame, textvariable=self._shell_status_var, wraplength=360).pack(anchor="w")
        ttk.Label(self._shell_controls_frame, textvariable=self._shell_click_through_var).pack(anchor="w", pady=(8, 0))
        ttk.Label(self._shell_controls_frame, textvariable=self._shell_interactive_var).pack(anchor="w", pady=(4, 0))
        self._shell_actions_container = ttk.Frame(self._shell_controls_frame)
        self._shell_actions_container.pack(fill=tk.X, pady=(10, 0))
        self._apply_shell_controls_to_widgets()

    def _build_labeled_entry(self, parent: Any, label: str, variable: Any) -> None:
        assert ttk is not None
        ttk.Label(parent, text=label).pack(anchor="w")
        ttk.Entry(parent, textvariable=variable).pack(fill=tk.X, pady=(4, 10))

    def _poll_ui_commands(self) -> None:
        if self._stop_requested.is_set():
            self._shutdown_ui()
            return
        while True:
            try:
                command, payload = self._command_queue.get_nowait()
            except queue.Empty:
                break
            if command == "shutdown":
                self._shutdown_ui()
                return
            if command == "clear_display":
                self._clear_display()
            elif command == "apply_settings":
                normalized = normalize_subtitle_ui_settings(payload, defaults=self._settings_store.defaults)
                self._settings = normalized
                self._sync_control_vars(normalized)
                self._apply_settings_to_widgets(normalized)
            elif command == "apply_shell_controls":
                self._shell_controls = self._normalize_shell_controls(payload)
                self._apply_shell_controls_to_widgets()
            elif command == "show_windows":
                self._show_windows()
        self._maybe_start_next_segment()
        if self._root is not None:
            self._root.after(self._POLL_INTERVAL_MS, self._poll_ui_commands)

    def _maybe_start_next_segment(self) -> None:
        if self._segment_finish_after_id is not None:
            return
        if self._current_reply is None:
            with self._pending_lock:
                if not self._pending_replies:
                    return
                self._current_reply = self._pending_replies.popleft()
            segments = list(self._current_reply.get("segments") or [])
            platform = str(self._current_reply.get("source_platform") or "").strip() or "local"
            if self._reply_meta_var is not None:
                self._reply_meta_var.set(f"{platform} reply / {len(segments)} segments")
            self._current_segment_index = 0

        segments = list(self._current_reply.get("segments") or [])
        if self._current_segment_index >= len(segments):
            self._current_reply = None
            self._current_segment_index = 0
            return
        segment = dict(segments[self._current_segment_index])
        self._current_segment_index += 1
        self._render_segment(segment)

    def _render_segment(self, segment: Mapping[str, Any]) -> None:
        assert tk is not None
        if self._subtitle_stack is None:
            return
        segment_model = normalize_runtime_segment(segment)
        if not segment_model.chinese_text and not segment_model.english_text:
            self._play_segment_audio(segment)
            self._maybe_start_next_segment()
            return

        entry = self._create_entry_widgets(segment_model)
        self._subtitle_entries.append(entry)
        self._sync_track_layout()
        self._animate_track_to_overflow()
        self._play_segment_audio(segment)
        self._start_reveal_animation(entry, segment_model, segment_model.duration_ms)
        assert self._root is not None
        self._segment_finish_after_id = self._root.after(
            segment_model.duration_ms,
            self._finish_segment,
            entry,
            segment_model,
        )

    def _create_entry_widgets(self, segment: RuntimeSubtitleSegment) -> _SubtitleEntryWidgets:
        assert tk is not None
        assert self._subtitle_stack is not None
        box_background = self._resolve_box_background()
        wraplength = self._entry_wraplength()
        font_family = select_tk_font_family(str(self._settings["font_family"]))
        chinese_font = (font_family, int(self._settings["font_size_px"]), "bold")
        english_font = (font_family, self._english_font_size(), "normal")
        english_color = self._english_text_color(box_background)

        container = tk.Frame(self._subtitle_stack, background=box_background, bd=0, highlightthickness=0)
        container.pack(fill=tk.X, anchor="w", pady=(0, self._ENTRY_SPACING_PX))

        english_label = None
        if segment.english_text:
            english_label = tk.Label(
                container,
                text="",
                anchor="w",
                justify="left",
                wraplength=wraplength,
                background=box_background,
                foreground=english_color,
                font=english_font,
            )
            english_label.pack(fill=tk.X, anchor="w", pady=(0, 4))

        chinese_label = tk.Label(
            container,
            text="",
            anchor="w",
            justify="left",
            wraplength=wraplength,
            background=box_background,
            foreground=str(self._settings["text_color"]),
            font=chinese_font,
        )
        chinese_label.pack(fill=tk.X, anchor="w")

        return _SubtitleEntryWidgets(
            container=container,
            english_label=english_label,
            chinese_label=chinese_label,
            english_text=segment.english_text,
            chinese_text=segment.chinese_text,
        )

    def _start_reveal_animation(
        self,
        entry: _SubtitleEntryWidgets,
        segment: RuntimeSubtitleSegment,
        duration_ms: int,
    ) -> None:
        if self._segment_reveal_after_id is not None and self._root is not None:
            with contextlib.suppress(Exception):
                self._root.after_cancel(self._segment_reveal_after_id)
        if self._root is None:
            self._set_entry_text(entry, segment.english_text, segment.chinese_text)
            return
        started_at = time.perf_counter()

        def tick() -> None:
            if self._root is None:
                return
            elapsed_ms = max(1.0, (time.perf_counter() - started_at) * 1000.0)
            progress = min(1.0, elapsed_ms / max(1.0, float(duration_ms)))
            english_text, chinese_text = reveal_bilingual_text(
                segment.english_text,
                segment.chinese_text,
                progress=progress,
            )
            self._set_entry_text(entry, english_text, chinese_text)
            self._sync_track_layout()
            self._animate_track_to_overflow()
            if progress < 1.0:
                self._segment_reveal_after_id = self._root.after(32, tick)
            else:
                self._segment_reveal_after_id = None

        tick()

    def _set_entry_text(self, entry: _SubtitleEntryWidgets, english_text: str, chinese_text: str) -> None:
        if entry.english_label is not None:
            entry.english_label.configure(text=english_text)
        entry.chinese_label.configure(text=chinese_text)

    def _finish_segment(self, entry: _SubtitleEntryWidgets, segment: RuntimeSubtitleSegment) -> None:
        self._set_entry_text(entry, segment.english_text, segment.chinese_text)
        self._sync_track_layout()
        self._animate_track_to_overflow()
        self._segment_finish_after_id = None
        if self._segment_reveal_after_id is not None and self._root is not None:
            with contextlib.suppress(Exception):
                self._root.after_cancel(self._segment_reveal_after_id)
            self._segment_reveal_after_id = None
        self._maybe_start_next_segment()

    def _animate_track_to_overflow(self) -> None:
        if self._root is None or self._subtitle_canvas is None or self._subtitle_stack_window_id is None:
            return
        self._track_scroll_target_px = float(self._sync_track_layout())
        if self._track_scroll_after_id is not None:
            return
        if abs(self._track_scroll_target_px - self._track_scroll_current_px) <= 1.0:
            self._track_scroll_current_px = self._track_scroll_target_px
            self._sync_track_layout(scroll_px=self._track_scroll_current_px)
            self._prune_scrolled_out_entries()
            return

        def tick() -> None:
            if self._root is None:
                self._track_scroll_after_id = None
                return
            delta = self._track_scroll_target_px - self._track_scroll_current_px
            if abs(delta) <= 1.0:
                self._track_scroll_current_px = self._track_scroll_target_px
                self._sync_track_layout(scroll_px=self._track_scroll_current_px)
                self._track_scroll_after_id = None
                self._prune_scrolled_out_entries()
                return
            step = max(1.0, abs(delta) * 0.35)
            if delta < 0:
                step *= -1.0
            self._track_scroll_current_px += step
            self._sync_track_layout(scroll_px=self._track_scroll_current_px)
            self._prune_scrolled_out_entries()
            self._track_scroll_after_id = self._root.after(self._SCROLL_TICK_MS, tick)

        self._track_scroll_after_id = self._root.after(self._SCROLL_TICK_MS, tick)

    def _sync_track_layout(self, *, scroll_px: float | None = None) -> int:
        if self._subtitle_canvas is None or self._subtitle_stack is None or self._subtitle_stack_window_id is None:
            return 0
        if scroll_px is not None:
            self._track_scroll_current_px = max(0.0, float(scroll_px))
        self._subtitle_canvas.update_idletasks()
        self._subtitle_stack.update_idletasks()

        box_width = max(240, int(self._settings["box_width_px"]))
        box_height = max(96, int(self._settings["box_height_px"]))
        inner_width = max(100, box_width - (self._ENTRY_HORIZONTAL_PADDING * 2))
        inner_height = max(1, box_height - (self._ENTRY_VERTICAL_PADDING * 2))
        track_height = max(1, int(self._subtitle_stack.winfo_reqheight()))
        overflow_px = compute_track_overflow_px(track_height=track_height, viewport_height=inner_height)

        self._track_scroll_target_px = float(overflow_px)
        self._track_scroll_current_px = min(self._track_scroll_current_px, self._track_scroll_target_px)
        base_y = self._ENTRY_VERTICAL_PADDING + max(0, inner_height - track_height)

        self._subtitle_canvas.configure(width=box_width, height=box_height)
        self._subtitle_canvas.itemconfigure(self._subtitle_stack_window_id, width=inner_width)
        self._subtitle_canvas.coords(
            self._subtitle_stack_window_id,
            self._ENTRY_HORIZONTAL_PADDING,
            base_y - self._track_scroll_current_px,
        )
        self._subtitle_canvas.configure(
            scrollregion=(0, 0, box_width, max(box_height, track_height + (self._ENTRY_VERTICAL_PADDING * 2)))
        )
        return overflow_px

    def _prune_scrolled_out_entries(self) -> None:
        if self._subtitle_stack is None:
            return
        self._subtitle_stack.update_idletasks()
        removed_any = False
        while len(self._subtitle_entries) > 5:
            first_entry = self._subtitle_entries[0]
            entry_bottom = int(first_entry.container.winfo_y()) + int(first_entry.container.winfo_height())
            if entry_bottom + self._ENTRY_VERTICAL_PADDING >= self._track_scroll_current_px:
                break
            self._subtitle_entries.pop(0).container.destroy()
            removed_any = True
        if removed_any:
            self._sync_track_layout()

    def _play_segment_audio(self, segment: Mapping[str, Any]) -> None:
        reply_id = ""
        if self._current_reply is not None:
            reply_id = str(self._current_reply.get("reply_id") or "").strip()
        segment_index = int(segment.get("index") or 0)
        started_at_ms = int(time.time() * 1000)
        if callable(self._on_audio_started):
            self._on_audio_started(reply_id, segment_index=segment_index, started_at_ms=started_at_ms)

    def _apply_control_values(self) -> None:
        updated = self.update_settings(
            {
                "box_width_px": self._box_width_var.get(),
                "box_height_px": self._box_height_var.get(),
                "left_px": self._left_var.get(),
                "bottom_px": self._bottom_var.get(),
                "font_family": self._font_family_var.get(),
                "font_size_px": self._font_size_var.get(),
                "text_color": self._text_color_var.get(),
                "background_color": self._background_color_var.get(),
                "background_opacity": self._background_opacity_var.get(),
            }
        )
        if self._reply_meta_var is not None:
            self._reply_meta_var.set("Settings applied")
        self._sync_control_vars(updated)

    def _apply_settings_to_widgets(self, settings: Mapping[str, Any]) -> None:
        if (
            self._overlay is None
            or self._subtitle_box is None
            or self._subtitle_canvas is None
            or self._subtitle_stack is None
        ):
            return
        screen_width = self._overlay.winfo_screenwidth()
        screen_height = self._overlay.winfo_screenheight()
        box_width = min(screen_width, int(settings["box_width_px"]))
        box_height = min(screen_height, int(settings["box_height_px"]))
        self._overlay.geometry(build_overlay_window_geometry(settings, screen_width=screen_width, screen_height=screen_height))

        box_background = self._resolve_box_background(settings)
        highlight = 1 if int(settings["background_opacity"]) > 0 else 0
        self._overlay.configure(background=box_background)
        self._subtitle_box.configure(background=box_background, highlightthickness=highlight, highlightbackground="#ffffff")
        self._subtitle_box.place(x=0, y=0, width=box_width, height=box_height)
        self._subtitle_canvas.configure(
            background=box_background,
            bd=0,
            highlightthickness=0,
        )
        self._subtitle_stack.configure(background=box_background)

        font_family = select_tk_font_family(str(settings["font_family"]))
        chinese_font = (font_family, int(settings["font_size_px"]), "bold")
        english_font = (font_family, self._english_font_size(int(settings["font_size_px"])), "normal")
        english_color = self._english_text_color(box_background, settings)
        wraplength = max(100, box_width - ((self._ENTRY_HORIZONTAL_PADDING * 2) + 8))
        for entry in self._subtitle_entries:
            entry.container.configure(background=box_background)
            if entry.english_label is not None:
                entry.english_label.configure(
                    background=box_background,
                    foreground=english_color,
                    font=english_font,
                    wraplength=wraplength,
                )
            entry.chinese_label.configure(
                background=box_background,
                foreground=str(settings["text_color"]),
                font=chinese_font,
                wraplength=wraplength,
            )
        self._sync_track_layout()

    def _resolve_box_background(self, settings: Mapping[str, Any] | None = None) -> str:
        source = normalize_subtitle_ui_settings(settings, defaults=self._settings)
        opacity = int(source["background_opacity"])
        if opacity <= 0:
            return self._OVERLAY_BASE_COLOR
        return blend_hex_colors(self._OVERLAY_BASE_COLOR, str(source["background_color"]), opacity / 100.0)

    def _english_text_color(self, box_background: str, settings: Mapping[str, Any] | None = None) -> str:
        source = normalize_subtitle_ui_settings(settings, defaults=self._settings)
        return blend_hex_colors(box_background, str(source["text_color"]), 0.68)

    def _entry_wraplength(self) -> int:
        return max(100, int(self._settings["box_width_px"]) - ((self._ENTRY_HORIZONTAL_PADDING * 2) + 8))

    def _english_font_size(self, font_size_px: int | None = None) -> int:
        chinese_font_size = int(font_size_px or self._settings["font_size_px"])
        return max(12, int(round(chinese_font_size * 0.58)))

    def _handle_mouse_follow_toggle(self, enabled: bool) -> None:
        status = "mouse" if enabled else "disabled"
        patch = {
            "mouse_follow_enabled": bool(enabled),
            "mouse_follow_status": status,
        }
        self._control_state = self._control_state.merge_patch(patch)
        if self._mouse_follow_enabled_var is not None:
            self._mouse_follow_enabled_var.set(bool(enabled))
        self._set_mouse_follow_status_hint(status)
        if callable(self._on_control_state_changed):
            self._on_control_state_changed(dict(patch))

    def _set_mouse_follow_status_hint(self, status: str) -> None:
        self._control_state = self._control_state.merge_patch({"mouse_follow_status": status})
        if self._mouse_follow_status_var is not None:
            self._mouse_follow_status_var.set(self._live2d_control_status_text(status))

    def _apply_shell_controls_to_widgets(self) -> None:
        frame = self._shell_controls_frame
        if frame is None:
            return
        enabled = bool(self._shell_controls.get("enabled"))
        click_through = bool(self._shell_controls.get("click_through", True))
        interactive = bool(self._shell_controls.get("interactive", False))
        actions = [
            str(action).strip()
            for action in self._shell_controls.get("actions", [])
            if str(action).strip()
        ]
        if self._shell_status_var is not None:
            self._shell_status_var.set("Shell controls active" if enabled else "Shell controls unavailable")
        if self._shell_click_through_var is not None:
            self._shell_click_through_var.set(f"Click-through: {'on' if click_through else 'off'}")
        if self._shell_interactive_var is not None:
            self._shell_interactive_var.set(f"Interactive: {'yes' if interactive else 'no'}")
        state = "normal" if enabled else "disabled"
        with contextlib.suppress(Exception):
            frame.configure(state=state)
        container = self._shell_actions_container
        if container is None:
            return
        for child in list(container.winfo_children()):
            with contextlib.suppress(Exception):
                child.destroy()
        if not enabled:
            return
        assert ttk is not None
        for action in actions:
            ttk.Button(
                container,
                text=self._shell_action_label(action),
                command=lambda action_name=action: self._handle_shell_action(action_name),
            ).pack(fill=tk.X, pady=(0, 8))

    def _shell_action_label(self, action: str) -> str:
        return {
            "toggle_click_through": "Toggle click-through",
            "unlock_drag": "Unlock drag mode",
            "reset_position": "Reset shell position",
            "open_settings": "Focus shell window",
        }.get(action, action.replace("_", " ").title())

    def _handle_shell_action(self, action: str) -> None:
        callback = self._on_shell_action_requested
        if callback is None:
            return
        callback(str(action))

    def _live2d_control_status_text(self, status: str | None = None) -> str:
        normalized = str(status or self._control_state.mouse_follow_status or "").strip().lower()
        status_map = {
            "mouse": "Mouse follow active",
            "cooldown": "Mouse follow cooling down",
            "ai": "AI motion active",
            "disabled": "Mouse follow disabled",
        }
        return status_map.get(normalized, status_map["disabled"])

    def _sync_control_vars(self, settings: Mapping[str, Any]) -> None:
        self._box_width_var.set(str(settings["box_width_px"]))
        self._box_height_var.set(str(settings["box_height_px"]))
        self._left_var.set(str(settings["left_px"]))
        self._bottom_var.set(str(settings["bottom_px"]))
        self._font_family_var.set(str(settings["font_family"]))
        self._font_size_var.set(str(settings["font_size_px"]))
        self._text_color_var.set(str(settings["text_color"]))
        self._background_color_var.set(str(settings["background_color"]))
        self._background_opacity_var.set(int(settings["background_opacity"]))

    def _clear_display(self) -> None:
        if self._root is not None and self._segment_finish_after_id is not None:
            with contextlib.suppress(Exception):
                self._root.after_cancel(self._segment_finish_after_id)
        if self._root is not None and self._segment_reveal_after_id is not None:
            with contextlib.suppress(Exception):
                self._root.after_cancel(self._segment_reveal_after_id)
        if self._root is not None and self._track_scroll_after_id is not None:
            with contextlib.suppress(Exception):
                self._root.after_cancel(self._track_scroll_after_id)
        self._segment_finish_after_id = None
        self._segment_reveal_after_id = None
        self._track_scroll_after_id = None
        self._track_scroll_current_px = 0.0
        self._track_scroll_target_px = 0.0
        self._current_reply = None
        self._current_segment_index = 0
        for entry in self._subtitle_entries:
            with contextlib.suppress(Exception):
                entry.container.destroy()
        self._subtitle_entries.clear()
        self._sync_track_layout(scroll_px=0.0)
        if self._reply_meta_var is not None:
            self._reply_meta_var.set("Subtitles cleared")

    def _reset_from_ui(self) -> None:
        updated = self.reset_settings()
        self._sync_control_vars(updated)
        if self._reply_meta_var is not None:
            self._reply_meta_var.set("Settings reset")

    def _restore_overlay_window(self) -> None:
        self._show_windows()

    def _show_windows(self) -> None:
        if self._root is None or self._overlay is None:
            return
        for window in (self._root, self._overlay):
            with contextlib.suppress(Exception):
                window.deiconify()
            with contextlib.suppress(Exception):
                window.lift()
        with contextlib.suppress(Exception):
            self._root.focus_force()

    def _minimize_all_windows(self) -> None:
        if self._overlay is None:
            if self._root is None:
                return
            with contextlib.suppress(Exception):
                self._root.iconify()
            return
        for window in (self._overlay, self._root):
            if window is None:
                continue
            with contextlib.suppress(Exception):
                window.iconify()

    def _shutdown_ui(self) -> None:
        if self._root is None:
            return
        root = self._root
        with contextlib.suppress(Exception):
            self._clear_display()
        self._current_reply = None
        self._current_segment_index = 0
        self._status_var = None
        self._reply_meta_var = None
        self._font_family_var = None
        self._font_size_var = None
        self._text_color_var = None
        self._background_color_var = None
        self._background_opacity_var = None
        self._box_width_var = None
        self._box_height_var = None
        self._left_var = None
        self._bottom_var = None
        self._mouse_follow_enabled_var = None
        self._mouse_follow_status_var = None
        self._shell_click_through_var = None
        self._shell_interactive_var = None
        self._shell_status_var = None
        self._shell_controls_frame = None
        self._shell_actions_container = None
        self._subtitle_box = None
        self._subtitle_canvas = None
        self._subtitle_stack = None
        self._subtitle_stack_window_id = None
        self._overlay = None
        self._root = None
        with contextlib.suppress(Exception):
            root.quit()
        with contextlib.suppress(Exception):
            root.destroy()
        self._ui_ready.clear()


def build_overlay_window_geometry(
    settings: Mapping[str, Any],
    *,
    screen_width: int,
    screen_height: int,
) -> str:
    """Build the floating subtitle overlay geometry from settings."""

    normalized = normalize_subtitle_ui_settings(settings)
    width = min(max(240, int(normalized["box_width_px"])), max(240, int(screen_width)))
    height = min(max(96, int(normalized["box_height_px"])), max(96, int(screen_height)))
    left = min(max(0, int(normalized["left_px"])), max(0, int(screen_width) - width))
    top = int(screen_height) - int(normalized["bottom_px"]) - height
    top = min(max(0, top), max(0, int(screen_height) - height))
    return f"{width}x{height}+{left}+{top}"
