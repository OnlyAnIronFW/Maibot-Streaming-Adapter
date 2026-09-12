"""Native desktop runtime for the realtime microphone speech input window."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import contextlib
import queue
import threading

from typing import Any

try:
    import tkinter as tk
    from tkinter import ttk

    TK_AVAILABLE = True
except Exception:  # pragma: no cover - tkinter can be missing in test environments
    tk = None  # type: ignore[assignment]
    ttk = None  # type: ignore[assignment]
    TK_AVAILABLE = False

from .local_voice_state import LocalVoiceSnapshot


_API_KEY_MASK = "********"
_HELP_SUMMARY_TEXT = "参数已按直播实时互动调过默认值；建议值通常可直接使用，只需要填 API Key、选择麦克风，然后点开始监听。"
_LOCAL_VOICE_HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "基础项",
        (
            ("麦克风设备", "选择要监听的输入设备。建议值：留空使用系统默认；直播时选择实际说话的麦克风。"),
            ("ASR 引擎", "当前实际运行只使用 aliyun_rasr。保留字段是为了后续切换其它 RASR 实现。建议值：aliyun_rasr。"),
            ("RASR 模型", "阿里云百炼实时语音识别模型 ID，可改成同协议的新模型。建议值：fun-asr-realtime。"),
            (
                "WebSocket URL",
                "阿里云 RASR 接入地址。建议值：北京地域 wss://dashscope.aliyuncs.com/api-ws/v1/inference/；"
                "国际地域可换成 wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference/。",
            ),
            ("API Key 环境变量", "从该环境变量读取 DashScope API Key。建议值：DASHSCOPE_API_KEY。"),
            ("API Key 覆写", "可直接填 Key，但更推荐留空走环境变量，避免把密钥写进配置文件。"),
            ("语言提示", "当前限制为中文和英文。建议值：zh,en；只说中文可填 zh，只说英文可填 en。"),
        ),
    ),
    (
        "实时性与稳定性",
        (
            ("音频块时长(ms)", "每次发送给 RASR 的麦克风块大小。建议值：100；更低延迟可试 60。"),
            ("句末静音(ms)", "服务端判断一句话结束的静音时长。建议值：800；短句更干脆可试 600，误切多可试 1000。"),
            ("中间结果", "打开后 UI 能实时显示未定稿文本。建议值：开启。"),
            ("中间结果发给 MaiBot", "默认关闭，只把最终句发给 MaiBot，避免重复触发回复。建议值：关闭。"),
            ("服务端噪声阈值", "阿里云服务端 VAD 阈值，范围 -1 到 1。建议值：0.3；噪声多可小幅试 0.35。"),
        ),
    ),
    (
        "发送给 MaiBot",
        (
            ("句子级后处理", "最终句进入 MaiBot 前再做轻量聚合和补标点。建议值：开启。"),
            ("句子缓冲超时(ms)", "没有新结果时多久补发当前句。建议值：700；直播低延迟可试 500。"),
            ("强制发送字数", "句子过长时提前发送，避免积压。建议值：16。"),
            ("自动补标点", "没有句末标点时补句号或问号。建议值：开启。"),
        ),
    ),
)


class LocalVoiceNativeRuntime:
    """Separate desktop window for microphone and Aliyun RASR control."""

    _POLL_INTERVAL_MS = 120

    def __init__(
        self,
        *,
        on_refresh_devices: Callable[[], None],
        on_refresh_models: Callable[[], None],
        on_toggle_listening: Callable[[], None],
        on_apply_settings: Callable[[Mapping[str, Any]], None],
        on_clear_log: Callable[[], None],
        logger: Any = None,
    ) -> None:
        self._on_refresh_devices = on_refresh_devices
        self._on_refresh_models = on_refresh_models
        self._on_toggle_listening = on_toggle_listening
        self._on_apply_settings = on_apply_settings
        self._on_clear_log = on_clear_log
        self._logger = logger
        self._thread: threading.Thread | None = None
        self._ui_ready = threading.Event()
        self._startup_error: Exception | None = None
        self._command_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._root: Any = None
        self._snapshot = LocalVoiceSnapshot()
        self._form_dirty = False
        self._pending_applied_patch: dict[str, Any] | None = None
        self._suppress_form_dirty_tracking = False

        self._status_var: Any = None
        self._error_var: Any = None
        self._device_var: Any = None
        self._speaker_user_id_var: Any = None
        self._speaker_username_var: Any = None
        self._engine_var: Any = None
        self._rasr_model_var: Any = None
        self._rasr_ws_url_var: Any = None
        self._rasr_api_key_env_var: Any = None
        self._rasr_api_key_var: Any = None
        self._rasr_api_key_entry: Any = None
        self._rasr_audio_format_var: Any = None
        self._rasr_language_hint_var: Any = None
        self._rasr_intermediate_var: Any = None
        self._rasr_punctuation_var: Any = None
        self._rasr_itn_var: Any = None
        self._rasr_sentence_silence_var: Any = None
        self._rasr_heartbeat_var: Any = None
        self._rasr_route_partials_var: Any = None
        self._rasr_noise_threshold_var: Any = None
        self._rasr_disfluency_var: Any = None
        self._sample_rate_var: Any = None
        self._channels_var: Any = None
        self._block_duration_var: Any = None
        self._sentence_postprocess_var: Any = None
        self._sentence_flush_inactivity_var: Any = None
        self._sentence_force_emit_var: Any = None
        self._sentence_auto_punctuation_var: Any = None
        self._min_transcript_var: Any = None
        self._device_combo: Any = None
        self._current_text_widget: Any = None
        self._log_text_widget: Any = None
        self._help_text_widget: Any = None
        self._toggle_button: Any = None

    def start(self) -> None:
        """Start the UI thread and create the control window."""

        if self.is_running:
            return
        if not TK_AVAILABLE:
            raise RuntimeError("tkinter is unavailable")
        self._ui_ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run_ui_thread, name="MaiBotRasrVoiceUI", daemon=True)
        self._thread.start()
        self._ui_ready.wait(timeout=5.0)
        if self._startup_error is not None:
            raise self._startup_error
        if not self._ui_ready.is_set():
            raise RuntimeError("local voice control window did not become ready in time")

    def stop(self) -> None:
        """Stop the UI thread."""

        thread = self._thread
        if thread is None:
            return
        self._command_queue.put(("shutdown", {}))
        thread.join(timeout=3.0)
        self._thread = None
        self._ui_ready.clear()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(self._ui_ready.is_set() and self._startup_error is None and thread is not None and thread.is_alive())

    def update_snapshot(self, snapshot: LocalVoiceSnapshot) -> None:
        """Push a new controller snapshot into the window thread."""

        self._command_queue.put(("snapshot", {"snapshot": snapshot}))

    def _run_ui_thread(self) -> None:
        try:
            self._build_window()
        except Exception as exc:  # pragma: no cover - UI startup failures are environment-specific
            self._startup_error = exc
            self._ui_ready.set()
            return
        self._ui_ready.set()
        assert self._root is not None
        self._root.after(self._POLL_INTERVAL_MS, self._poll_commands)
        with contextlib.suppress(Exception):
            self._root.mainloop()

    def _build_window(self) -> None:
        assert tk is not None
        assert ttk is not None
        self._root = tk.Tk()
        self._root.title("MaiBot 实时语音输入控制")
        self._root.geometry("760x860+520+80")
        self._root.configure(background="#11161f")
        self._root.protocol("WM_DELETE_WINDOW", self._iconify_window)

        style = ttk.Style(self._root)
        with contextlib.suppress(Exception):
            style.theme_use("clam")

        self._status_var = tk.StringVar(master=self._root, value="待命")
        self._error_var = tk.StringVar(master=self._root, value="就绪")
        self._device_var = tk.StringVar(master=self._root)
        self._speaker_user_id_var = tk.StringVar(master=self._root)
        self._speaker_username_var = tk.StringVar(master=self._root)
        self._engine_var = tk.StringVar(master=self._root)
        self._rasr_model_var = tk.StringVar(master=self._root)
        self._rasr_ws_url_var = tk.StringVar(master=self._root)
        self._rasr_api_key_env_var = tk.StringVar(master=self._root)
        self._rasr_api_key_var = tk.StringVar(master=self._root)
        self._rasr_audio_format_var = tk.StringVar(master=self._root)
        self._rasr_language_hint_var = tk.StringVar(master=self._root)
        self._rasr_intermediate_var = tk.BooleanVar(master=self._root)
        self._rasr_punctuation_var = tk.BooleanVar(master=self._root)
        self._rasr_itn_var = tk.BooleanVar(master=self._root)
        self._rasr_sentence_silence_var = tk.StringVar(master=self._root)
        self._rasr_heartbeat_var = tk.BooleanVar(master=self._root)
        self._rasr_route_partials_var = tk.BooleanVar(master=self._root)
        self._rasr_noise_threshold_var = tk.StringVar(master=self._root)
        self._rasr_disfluency_var = tk.BooleanVar(master=self._root)
        self._sample_rate_var = tk.StringVar(master=self._root)
        self._channels_var = tk.StringVar(master=self._root)
        self._block_duration_var = tk.StringVar(master=self._root)
        self._sentence_postprocess_var = tk.BooleanVar(master=self._root)
        self._sentence_flush_inactivity_var = tk.StringVar(master=self._root)
        self._sentence_force_emit_var = tk.StringVar(master=self._root)
        self._sentence_auto_punctuation_var = tk.BooleanVar(master=self._root)
        self._min_transcript_var = tk.StringVar(master=self._root)
        self._install_form_dirty_tracking()

        wrapper = ttk.Frame(self._root, padding=16)
        wrapper.pack(fill=tk.BOTH, expand=True)

        ttk.Label(wrapper, text="MaiBot 实时语音输入控制", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(wrapper, textvariable=self._status_var).pack(anchor="w", pady=(6, 0))
        ttk.Label(wrapper, textvariable=self._error_var, wraplength=680).pack(anchor="w", pady=(4, 12))

        notebook = ttk.Notebook(wrapper)
        notebook.pack(fill=tk.BOTH, expand=True)

        control_tab_shell, control_tab = self._build_scrollable_tab(notebook)
        transcript_tab = ttk.Frame(notebook, padding=12)
        help_tab = ttk.Frame(notebook, padding=12)
        notebook.add(control_tab_shell, text="控制面板")
        notebook.add(transcript_tab, text="转写内容")
        notebook.add(help_tab, text="参数说明")

        self._build_control_tab(control_tab)
        self._build_transcript_tab(transcript_tab)
        self._build_help_tab(help_tab)
        self._apply_snapshot(self._snapshot)

    def _build_scrollable_tab(self, parent: Any) -> tuple[Any, Any]:
        assert tk is not None
        assert ttk is not None
        shell = ttk.Frame(parent)
        canvas = tk.Canvas(shell, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas, padding=12)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        def _sync_scroll_region(_event: Any = None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_width(event: Any) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        content.bind("<Configure>", _sync_scroll_region)
        canvas.bind("<Configure>", _sync_width)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return shell, content

    def _build_control_tab(self, parent: Any) -> None:
        assert tk is not None
        assert ttk is not None

        ttk.Label(parent, text="麦克风设备").pack(anchor="w")
        device_row = ttk.Frame(parent)
        device_row.pack(fill=tk.X, pady=(4, 12))
        self._device_combo = ttk.Combobox(device_row, textvariable=self._device_var, state="readonly")
        self._device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(device_row, text="刷新设备", command=self._on_refresh_devices).pack(side=tk.LEFT, padx=(8, 0))

        action_row = ttk.Frame(parent)
        action_row.pack(fill=tk.X, pady=(0, 12))
        self._toggle_button = ttk.Button(action_row, text="开始监听", command=self._on_toggle_listening)
        self._toggle_button.pack(side=tk.LEFT)
        ttk.Button(action_row, text="应用设置", command=self._apply_form_settings).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(action_row, text="清空转写", command=self._on_clear_log).pack(side=tk.LEFT, padx=(8, 0))

        self._build_labeled_entry(parent, "说话人 ID", self._speaker_user_id_var)
        self._build_labeled_entry(parent, "显示昵称", self._speaker_username_var)
        self._build_labeled_entry(parent, "ASR 引擎", self._engine_var)
        self._build_labeled_entry(parent, "RASR 模型", self._rasr_model_var)
        self._build_labeled_entry(parent, "RASR WebSocket URL", self._rasr_ws_url_var)
        self._build_labeled_entry(parent, "API Key 环境变量", self._rasr_api_key_env_var)
        self._rasr_api_key_entry = self._build_labeled_entry(
            parent,
            "API Key 覆写（已隐藏，可留空）",
            self._rasr_api_key_var,
            show="*",
        )
        self._build_labeled_entry(parent, "音频格式", self._rasr_audio_format_var)
        self._build_labeled_entry(parent, "语言提示（推荐 zh,en）", self._rasr_language_hint_var)
        self._build_labeled_entry(parent, "采集采样率", self._sample_rate_var)
        self._build_labeled_entry(parent, "采集声道数", self._channels_var)
        self._build_labeled_entry(parent, "音频块时长(ms)", self._block_duration_var)
        self._build_labeled_entry(parent, "句末静音(ms)", self._rasr_sentence_silence_var)
        self._build_labeled_entry(parent, "服务端噪声阈值(-1~1)", self._rasr_noise_threshold_var)
        self._build_labeled_entry(parent, "最短转写长度", self._min_transcript_var)
        ttk.Checkbutton(parent, text="显示 RASR 中间结果", variable=self._rasr_intermediate_var).pack(anchor="w", pady=(0, 10))
        ttk.Checkbutton(parent, text="RASR 自动标点", variable=self._rasr_punctuation_var).pack(anchor="w", pady=(0, 10))
        ttk.Checkbutton(parent, text="RASR 文本规范化", variable=self._rasr_itn_var).pack(anchor="w", pady=(0, 10))
        ttk.Checkbutton(parent, text="RASR 静音心跳保活", variable=self._rasr_heartbeat_var).pack(anchor="w", pady=(0, 10))
        ttk.Checkbutton(parent, text="中间结果也发送给 MaiBot", variable=self._rasr_route_partials_var).pack(anchor="w", pady=(0, 10))
        ttk.Checkbutton(parent, text="过滤口吃/语气词", variable=self._rasr_disfluency_var).pack(anchor="w", pady=(0, 10))
        ttk.Checkbutton(parent, text="句子级后处理", variable=self._sentence_postprocess_var).pack(anchor="w", pady=(0, 10))
        self._build_labeled_entry(parent, "句子缓冲超时(ms)", self._sentence_flush_inactivity_var)
        self._build_labeled_entry(parent, "强制发送字数", self._sentence_force_emit_var)
        ttk.Checkbutton(parent, text="自动补标点", variable=self._sentence_auto_punctuation_var).pack(anchor="w", pady=(0, 12))

    def _build_transcript_tab(self, parent: Any) -> None:
        assert tk is not None
        assert ttk is not None

        ttk.Label(parent, text="转写内容", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(0, 8))
        ttk.Label(parent, text="当前转写").pack(anchor="w")
        current_frame = ttk.Frame(parent)
        current_frame.pack(fill=tk.BOTH, expand=False, pady=(4, 12))
        self._current_text_widget = tk.Text(current_frame, height=6, wrap="word", state="disabled")
        current_scroll = ttk.Scrollbar(current_frame, orient="vertical", command=self._current_text_widget.yview)
        self._current_text_widget.configure(yscrollcommand=current_scroll.set)
        self._current_text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        current_scroll.pack(side=tk.LEFT, fill=tk.Y)

        ttk.Label(parent, text="转写日志").pack(anchor="w")
        log_frame = ttk.Frame(parent)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self._log_text_widget = tk.Text(log_frame, height=18, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self._log_text_widget.yview)
        self._log_text_widget.configure(yscrollcommand=log_scroll.set)
        self._log_text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.LEFT, fill=tk.Y)

    def _build_help_tab(self, parent: Any) -> None:
        assert tk is not None
        assert ttk is not None

        ttk.Label(parent, text="参数说明", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(parent, text=_HELP_SUMMARY_TEXT, wraplength=660).pack(anchor="w", pady=(6, 12))

        help_frame = ttk.Frame(parent)
        help_frame.pack(fill=tk.BOTH, expand=True)
        self._help_text_widget = tk.Text(help_frame, wrap="word", state="disabled")
        help_scroll = ttk.Scrollbar(help_frame, orient="vertical", command=self._help_text_widget.yview)
        self._help_text_widget.configure(yscrollcommand=help_scroll.set)
        self._help_text_widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        help_scroll.pack(side=tk.LEFT, fill=tk.Y)
        self._set_text_widget(self._help_text_widget, build_local_voice_help_text())

    def _build_labeled_entry(self, parent: Any, label: str, variable: Any, *, show: str = "") -> Any:
        assert ttk is not None
        ttk.Label(parent, text=label).pack(anchor="w")
        entry = ttk.Entry(parent, textvariable=variable, show=show)
        entry.pack(fill=tk.X, pady=(4, 10))
        return entry

    def _poll_commands(self) -> None:
        if self._root is None:
            return
        while True:
            try:
                command, payload = self._command_queue.get_nowait()
            except queue.Empty:
                break
            if command == "shutdown":
                self._shutdown()
                return
            if command == "snapshot":
                self._apply_snapshot(payload["snapshot"])
        self._root.after(self._POLL_INTERVAL_MS, self._poll_commands)

    def _apply_snapshot(self, snapshot: LocalVoiceSnapshot) -> None:
        self._snapshot = snapshot
        if self._status_var is None:
            return
        settings = snapshot.settings
        self._status_var.set("监听中" if snapshot.is_listening else "待命")
        self._error_var.set(snapshot.last_error or "就绪")
        if self._should_refresh_form_from_snapshot(settings):
            self._apply_snapshot_settings_to_form(settings)
        if self._device_combo is not None:
            self._device_combo.configure(values=list(snapshot.available_devices))
        if self._toggle_button is not None:
            self._toggle_button.configure(text="停止监听" if snapshot.is_listening else "开始监听")
        self._set_text_widget(self._current_text_widget, snapshot.current_display_text)
        log_lines = [
            f"[{entry.timestamp_text}] {'临时' if entry.partial else '最终'} {entry.text}"
            for entry in snapshot.transcript_log
        ]
        self._set_text_widget(self._log_text_widget, "\n".join(log_lines))

    def _apply_form_settings(self) -> None:
        values = {
            "input_device": self._device_var.get(),
            "speaker_user_id": self._speaker_user_id_var.get(),
            "speaker_username": self._speaker_username_var.get(),
            "engine": self._engine_var.get(),
            "rasr_model": self._rasr_model_var.get(),
            "rasr_ws_url": self._rasr_ws_url_var.get(),
            "rasr_api_key_env": self._rasr_api_key_env_var.get(),
            "rasr_api_key": self._rasr_api_key_var.get(),
            "rasr_audio_format": self._rasr_audio_format_var.get(),
            "rasr_language_hint": self._rasr_language_hint_var.get(),
            "rasr_enable_intermediate_result": self._rasr_intermediate_var.get(),
            "rasr_enable_punctuation_prediction": self._rasr_punctuation_var.get(),
            "rasr_enable_inverse_text_normalization": self._rasr_itn_var.get(),
            "rasr_max_sentence_silence_ms": self._rasr_sentence_silence_var.get(),
            "rasr_heartbeat": self._rasr_heartbeat_var.get(),
            "rasr_route_partials_to_maibot": self._rasr_route_partials_var.get(),
            "rasr_speech_noise_threshold": self._rasr_noise_threshold_var.get(),
            "rasr_disfluency_removal_enabled": self._rasr_disfluency_var.get(),
            "sample_rate_hz": self._sample_rate_var.get(),
            "channels": self._channels_var.get(),
            "block_duration_ms": self._block_duration_var.get(),
            "sentence_postprocess_enabled": self._sentence_postprocess_var.get(),
            "sentence_flush_inactivity_ms": self._sentence_flush_inactivity_var.get(),
            "sentence_force_emit_chars": self._sentence_force_emit_var.get(),
            "sentence_auto_punctuation": self._sentence_auto_punctuation_var.get(),
            "min_transcript_length": self._min_transcript_var.get(),
        }
        patch = coerce_runtime_patch(values)
        self._form_dirty = True
        self._pending_applied_patch = dict(patch)
        self._on_apply_settings(patch)

    def _install_form_dirty_tracking(self) -> None:
        for variable in self._iter_form_variables():
            with contextlib.suppress(Exception):
                variable.trace_add("write", self._on_form_variable_changed)

    def _iter_form_variables(self) -> tuple[Any, ...]:
        return (
            self._device_var,
            self._speaker_user_id_var,
            self._speaker_username_var,
            self._engine_var,
            self._rasr_model_var,
            self._rasr_ws_url_var,
            self._rasr_api_key_env_var,
            self._rasr_api_key_var,
            self._rasr_audio_format_var,
            self._rasr_language_hint_var,
            self._rasr_intermediate_var,
            self._rasr_punctuation_var,
            self._rasr_itn_var,
            self._rasr_sentence_silence_var,
            self._rasr_heartbeat_var,
            self._rasr_route_partials_var,
            self._rasr_noise_threshold_var,
            self._rasr_disfluency_var,
            self._sample_rate_var,
            self._channels_var,
            self._block_duration_var,
            self._sentence_postprocess_var,
            self._sentence_flush_inactivity_var,
            self._sentence_force_emit_var,
            self._sentence_auto_punctuation_var,
            self._min_transcript_var,
        )

    def _on_form_variable_changed(self, *_args: Any) -> None:
        if self._suppress_form_dirty_tracking:
            return
        self._form_dirty = True
        self._pending_applied_patch = None

    def _should_refresh_form_from_snapshot(self, settings: Any) -> bool:
        if not self._form_dirty:
            return True
        pending_patch = self._pending_applied_patch
        if pending_patch and _settings_view_matches_patch(settings, pending_patch):
            self._form_dirty = False
            self._pending_applied_patch = None
            return True
        return False

    def _apply_snapshot_settings_to_form(self, settings: Any) -> None:
        self._suppress_form_dirty_tracking = True
        try:
            self._device_var.set(settings.input_device)
            self._speaker_user_id_var.set(settings.speaker_user_id)
            self._speaker_username_var.set(settings.speaker_username)
            self._engine_var.set(settings.engine)
            self._rasr_model_var.set(settings.rasr_model)
            self._rasr_ws_url_var.set(settings.rasr_ws_url)
            self._rasr_api_key_env_var.set(settings.rasr_api_key_env)
            self._rasr_api_key_var.set(_API_KEY_MASK if str(settings.rasr_api_key or "").strip() else "")
            self._rasr_audio_format_var.set(settings.rasr_audio_format)
            self._rasr_language_hint_var.set(settings.rasr_language_hint)
            self._rasr_intermediate_var.set(settings.rasr_enable_intermediate_result)
            self._rasr_punctuation_var.set(settings.rasr_enable_punctuation_prediction)
            self._rasr_itn_var.set(settings.rasr_enable_inverse_text_normalization)
            self._rasr_sentence_silence_var.set(str(settings.rasr_max_sentence_silence_ms))
            self._rasr_heartbeat_var.set(settings.rasr_heartbeat)
            self._rasr_route_partials_var.set(settings.rasr_route_partials_to_maibot)
            self._rasr_noise_threshold_var.set(str(settings.rasr_speech_noise_threshold))
            self._rasr_disfluency_var.set(settings.rasr_disfluency_removal_enabled)
            self._sample_rate_var.set(str(settings.sample_rate_hz))
            self._channels_var.set(str(settings.channels))
            self._block_duration_var.set(str(settings.block_duration_ms))
            self._sentence_postprocess_var.set(settings.sentence_postprocess_enabled)
            self._sentence_flush_inactivity_var.set(str(settings.sentence_flush_inactivity_ms))
            self._sentence_force_emit_var.set(str(settings.sentence_force_emit_chars))
            self._sentence_auto_punctuation_var.set(settings.sentence_auto_punctuation)
            self._min_transcript_var.set(str(settings.min_transcript_length))
        finally:
            self._suppress_form_dirty_tracking = False

    def _set_text_widget(self, widget: Any, text: str) -> None:
        if widget is None:
            return
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.insert("1.0", str(text or ""))
        widget.configure(state="disabled")
        widget.see(tk.END)

    def _iconify_window(self) -> None:
        if self._root is not None:
            with contextlib.suppress(Exception):
                self._root.iconify()

    def _shutdown(self) -> None:
        root = self._root
        if root is not None:
            with contextlib.suppress(Exception):
                root.quit()
            with contextlib.suppress(Exception):
                root.destroy()
        self._status_var = None
        self._error_var = None
        self._device_var = None
        self._speaker_user_id_var = None
        self._speaker_username_var = None
        self._engine_var = None
        self._rasr_model_var = None
        self._rasr_ws_url_var = None
        self._rasr_api_key_env_var = None
        self._rasr_api_key_var = None
        self._rasr_api_key_entry = None
        self._rasr_audio_format_var = None
        self._rasr_language_hint_var = None
        self._rasr_intermediate_var = None
        self._rasr_punctuation_var = None
        self._rasr_itn_var = None
        self._rasr_sentence_silence_var = None
        self._rasr_heartbeat_var = None
        self._rasr_route_partials_var = None
        self._rasr_noise_threshold_var = None
        self._rasr_disfluency_var = None
        self._sample_rate_var = None
        self._channels_var = None
        self._block_duration_var = None
        self._sentence_postprocess_var = None
        self._sentence_flush_inactivity_var = None
        self._sentence_force_emit_var = None
        self._sentence_auto_punctuation_var = None
        self._min_transcript_var = None
        self._device_combo = None
        self._current_text_widget = None
        self._log_text_widget = None
        self._help_text_widget = None
        self._toggle_button = None
        self._root = None

    def _on_refresh_devices(self) -> None:
        self._on_refresh_devices()

    def _on_refresh_models(self) -> None:
        self._on_refresh_models()

    def _on_toggle_listening(self) -> None:
        self._on_toggle_listening()

    def _on_clear_log(self) -> None:
        self._on_clear_log()


def _settings_view_matches_patch(settings: Any, patch: Mapping[str, Any]) -> bool:
    for key, expected in patch.items():
        if not hasattr(settings, key):
            continue
        if key == "rasr_api_key" and not str(expected or "").strip():
            continue
        actual = getattr(settings, key)
        if isinstance(expected, float):
            try:
                if abs(float(actual) - expected) > 1e-6:
                    return False
            except (TypeError, ValueError):
                return False
            continue
        if actual != expected:
            return False
    return True


def coerce_runtime_patch(values: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize form values from the native control window into config patch values."""

    rasr_api_key = str(values.get("rasr_api_key") or "").strip()
    if rasr_api_key == _API_KEY_MASK:
        rasr_api_key = ""
    return {
        "selected_model_label": str(values.get("selected_model_label") or "").strip(),
        "input_device": str(values.get("input_device") or "").strip(),
        "speaker_user_id": str(values.get("speaker_user_id") or "").strip() or "local-mic",
        "speaker_username": str(values.get("speaker_username") or "").strip() or "Local Mic",
        "engine": str(values.get("engine") or "").strip() or "aliyun_rasr",
        "rasr_model": str(values.get("rasr_model") or "").strip() or "fun-asr-realtime",
        "rasr_ws_url": str(values.get("rasr_ws_url") or "").strip()
        or "wss://dashscope.aliyuncs.com/api-ws/v1/inference/",
        "rasr_api_key_env": str(values.get("rasr_api_key_env") or "").strip() or "DASHSCOPE_API_KEY",
        "rasr_api_key": rasr_api_key,
        "rasr_audio_format": str(values.get("rasr_audio_format") or "").strip() or "pcm",
        "rasr_language_hint": str(values.get("rasr_language_hint") or "").strip() or "zh,en",
        "rasr_enable_intermediate_result": (
            True
            if values.get("rasr_enable_intermediate_result") is None
            else _coerce_bool(values.get("rasr_enable_intermediate_result"))
        ),
        "rasr_enable_punctuation_prediction": (
            True
            if values.get("rasr_enable_punctuation_prediction") is None
            else _coerce_bool(values.get("rasr_enable_punctuation_prediction"))
        ),
        "rasr_enable_inverse_text_normalization": (
            True
            if values.get("rasr_enable_inverse_text_normalization") is None
            else _coerce_bool(values.get("rasr_enable_inverse_text_normalization"))
        ),
        "rasr_max_sentence_silence_ms": max(1, _coerce_int(values.get("rasr_max_sentence_silence_ms"), 800)),
        "rasr_heartbeat": True if values.get("rasr_heartbeat") is None else _coerce_bool(values.get("rasr_heartbeat")),
        "rasr_route_partials_to_maibot": (
            False
            if values.get("rasr_route_partials_to_maibot") is None
            else _coerce_bool(values.get("rasr_route_partials_to_maibot"))
        ),
        "rasr_speech_noise_threshold": min(
            1.0, max(-1.0, _coerce_float(values.get("rasr_speech_noise_threshold"), 0.3))
        ),
        "rasr_disfluency_removal_enabled": (
            False
            if values.get("rasr_disfluency_removal_enabled") is None
            else _coerce_bool(values.get("rasr_disfluency_removal_enabled"))
        ),
        "sample_rate_hz": max(1, _coerce_int(values.get("sample_rate_hz"), 16000)),
        "channels": max(1, _coerce_int(values.get("channels"), 1)),
        "block_duration_ms": max(1, _coerce_int(values.get("block_duration_ms"), 100)),
        "sentence_postprocess_enabled": (
            True
            if values.get("sentence_postprocess_enabled") is None
            else _coerce_bool(values.get("sentence_postprocess_enabled"))
        ),
        "sentence_flush_inactivity_ms": max(1, _coerce_int(values.get("sentence_flush_inactivity_ms"), 700)),
        "sentence_force_emit_chars": max(1, _coerce_int(values.get("sentence_force_emit_chars"), 16)),
        "sentence_auto_punctuation": (
            True
            if values.get("sentence_auto_punctuation") is None
            else _coerce_bool(values.get("sentence_auto_punctuation"))
        ),
        "speech_vad_enabled": (
            True if values.get("speech_vad_enabled") is None else _coerce_bool(values.get("speech_vad_enabled"))
        ),
        "speech_noise_reduction_enabled": (
            True
            if values.get("speech_noise_reduction_enabled") is None
            else _coerce_bool(values.get("speech_noise_reduction_enabled"))
        ),
        "speech_vad_start_threshold": min(
            1.0, max(0.0, _coerce_float(values.get("speech_vad_start_threshold"), 0.024))
        ),
        "speech_vad_noise_ratio": max(1.0, _coerce_float(values.get("speech_vad_noise_ratio"), 4.0)),
        "speech_vad_hold_ms": max(0, _coerce_int(values.get("speech_vad_hold_ms"), 250)),
        "pre_speech_padding_ms": max(0, _coerce_int(values.get("pre_speech_padding_ms"), 160)),
        "speech_reset_on_silence": (
            True
            if values.get("speech_reset_on_silence") is None
            else _coerce_bool(values.get("speech_reset_on_silence"))
        ),
        "speech_noise_floor_adaptation": min(
            1.0, max(0.0, _coerce_float(values.get("speech_noise_floor_adaptation"), 0.05))
        ),
        "speech_noise_suppression_strength": min(
            1.0, max(0.0, _coerce_float(values.get("speech_noise_suppression_strength"), 0.8))
        ),
        "min_transcript_length": max(1, _coerce_int(values.get("min_transcript_length"), 1)),
        # Legacy sherpa keys are accepted only so stale UI/test patches do not crash config migration.
        "sherpa_model_type": str(values.get("sherpa_model_type") or "").strip() or "transducer",
        "sherpa_provider": str(values.get("sherpa_provider") or "").strip() or "cpu",
        "sherpa_num_threads": max(1, _coerce_int(values.get("sherpa_num_threads"), 1)),
        "sherpa_model_sample_rate_hz": max(1, _coerce_int(values.get("sherpa_model_sample_rate_hz"), 16000)),
        "sherpa_feature_dim": max(1, _coerce_int(values.get("sherpa_feature_dim"), 80)),
        "sherpa_decoding_method": str(values.get("sherpa_decoding_method") or "").strip() or "greedy_search",
        "sherpa_max_active_paths": max(1, _coerce_int(values.get("sherpa_max_active_paths"), 4)),
        "sherpa_hotwords_file": str(values.get("sherpa_hotwords_file") or "").strip(),
        "sherpa_hotwords_score": max(0.0, _coerce_float(values.get("sherpa_hotwords_score"), 1.5)),
        "sherpa_blank_penalty": max(0.0, _coerce_float(values.get("sherpa_blank_penalty"), 0.0)),
        "sherpa_enable_endpoint": _coerce_bool(values.get("sherpa_enable_endpoint")),
        "stable_emit_min_chars": max(1, _coerce_int(values.get("stable_emit_min_chars"), 1)),
        "sherpa_encoder": str(values.get("sherpa_encoder") or "").strip(),
        "sherpa_decoder": str(values.get("sherpa_decoder") or "").strip(),
        "sherpa_joiner": str(values.get("sherpa_joiner") or "").strip(),
        "sherpa_tokens": str(values.get("sherpa_tokens") or "").strip(),
    }


def _coerce_int(value: Any, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return fallback


def _coerce_float(value: Any, fallback: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return fallback


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_local_voice_help_text() -> str:
    lines: list[str] = []
    for section_title, items in _LOCAL_VOICE_HELP_SECTIONS:
        lines.append(section_title)
        lines.append("")
        for label, description in items:
            lines.append(f"{label}")
            lines.append(f"  {description}")
            lines.append("")
    return "\n".join(lines).strip()
