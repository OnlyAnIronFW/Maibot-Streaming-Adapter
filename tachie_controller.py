"""LLM-driven tachi-e (立绘) expression selection and display control."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import re
import subprocess
import sys
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from src.config.config import config_manager
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.openai_compat import build_openai_compatible_client_config, split_openai_request_overrides

from .config import TachieConfig


@dataclass(frozen=True)
class TachieEntry:
    """One discovered tachi-e file."""
    pose: str
    emotion: str
    variant: int
    filename: str
    full_path: str


@dataclass(frozen=True)
class TachieSelection:
    """LLM-selected tachi-e result."""
    pose: str
    emotion: str
    variant: int
    filename: str
    full_path: str
    reason: str


_TPL_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>MaiBot Tachi-e</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;overflow:hidden;
  background:#00ff00}}
#tachie{{display:block;width:100%;height:100%;object-fit:contain;
  image-rendering:auto;transition:opacity 0.12s ease}}
#label{{position:fixed;left:12px;bottom:10px;color:rgba(255,255,255,0.55);
  font-family:"Microsoft YaHei UI",sans-serif;font-size:14px;
  pointer-events:none;text-shadow:0 1px 3px rgba(0,0,0,0.7)}}
</style></head><body>
<img id="tachie" src="{initial_src}" alt="tachie">
<div id="label"></div>
<script>
var evt=new EventSource("/events");
evt.onmessage=function(e){{
  var d=JSON.parse(e.data);
  var img=document.getElementById("tachie");
  img.style.opacity="0";
  setTimeout(function(){{
    img.src="/image?t="+Date.now();
    img.style.opacity="1";
    document.getElementById("label").textContent=
      d.pose+" / "+d.emotion+(d.reason?"  |  "+d.reason:"");
  }},80);
}};
evt.onerror=function(){{console.log("SSE reconnect...");}};
</script></body></html>"""


class TachieController:
    """Scans tachi-e files, uses an LLM to select expressions, and displays them in a desktop window."""

    def __init__(self, config: TachieConfig, *, plugin_dir: str = "", logger: Any = None) -> None:
        self.config = config
        self.plugin_dir = str(plugin_dir or "")
        self.logger = logger
        self._catalog: list[TachieEntry] = []
        self._by_key: dict[str, TachieEntry] = {}
        self._poses: list[str] = []
        self._emotions: list[str] = []
        self._last_reply_at: float = 0.0
        self._idle_reset_task: asyncio.Task | None = None
        self._catalog_built = False
        self._current_selection: TachieSelection | None = None
        self._sse_queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._http_runner: Any = None
        self._http_site: Any = None
        self._window_process: subprocess.Popen[Any] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    # ── catalog ──────────────────────────────────────────────

    def build_catalog(self) -> None:
        """Scan the tachi-e directory and build the file catalog."""
        self._catalog.clear()
        self._by_key.clear()
        tachie_dir = str(self.config.tachie_dir or "").strip()
        if not tachie_dir:
            self._log_warning("Tachi-e directory is empty, skipping catalog build.")
            self._catalog_built = True
            return
        dir_path = Path(tachie_dir).expanduser()
        if not dir_path.is_absolute():
            if self.plugin_dir:
                dir_path = Path(self.plugin_dir) / dir_path
            else:
                dir_path = Path(os.getcwd()) / dir_path
        if not dir_path.is_dir():
            self._log_warning(f"Tachi-e directory not found: {dir_path}")
            self._catalog_built = True
            return
        emotions_seen: set[str] = set()
        poses_seen: set[str] = set()
        for pose_dir in sorted(dir_path.iterdir()):
            if not pose_dir.is_dir():
                continue
            pose_name = pose_dir.name
            poses_seen.add(pose_name)
            for png_file in sorted(pose_dir.glob("*.png")):
                entry = _parse_tachie_filename(png_file.name, pose_name, str(png_file))
                if entry is not None:
                    self._catalog.append(entry)
                    self._by_key[f"{entry.pose}/{entry.emotion}/{entry.variant}"] = entry
                    self._by_key[f"{entry.pose}/{entry.emotion}"] = entry
                    emotions_seen.add(entry.emotion)
        self._poses = sorted(poses_seen)
        self._emotions = sorted(emotions_seen)
        self._catalog_built = True
        if self._catalog:
            self._log_info(
                f"Tachi-e catalog built: {len(self._catalog)} files, "
                f"{len(self._poses)} poses, {len(self._emotions)} emotions"
            )
        else:
            self._log_warning(f"Tachi-e catalog is empty for directory: {dir_path}")

    # ── lifecycle ────────────────────────────────────────────

    async def start(self) -> None:
        if not self._catalog_built:
            self.build_catalog()
        if self.config.display_window_enabled:
            await self._start_display_server()
            self._start_display_window()
        # show default expression right away
        await self.reset_to_default()

    async def stop(self) -> None:
        self._cancel_idle_reset()
        self._stop_display_window()
        await self._stop_display_server()

    # ── selection + switching ────────────────────────────────

    async def select_and_switch(self, *, reply_text: str, context_text: str = "") -> TachieSelection | None:
        """Select a tachi-e based on reply text and push to the display window.

        This runs in parallel with reply delivery - never blocks the main flow.
        """
        if not self.enabled or not self._catalog:
            return None
        if not reply_text.strip():
            return None
        self._last_reply_at = time.time()
        self._cancel_idle_reset()
        try:
            selection = await self._select_via_llm(reply_text=reply_text, context_text=context_text)
        except Exception as exc:
            self._log_warning(f"Tachi-e LLM selection failed: {exc}")
            return None
        if selection is None:
            return None
        self._current_selection = selection
        self._notify_display(selection)
        if self.config.idle_reset_after_sec > 0:
            self._schedule_idle_reset()
        return selection

    async def reset_to_default(self) -> bool:
        """Reset tachi-e to the default idle expression."""
        if not self.enabled:
            return False
        default_pose = str(self.config.default_pose or "hands down").strip()
        default_emotion = str(self.config.default_emotion or "idle").strip()
        entry = self._resolve_entry(pose=default_pose, emotion=default_emotion, variant=1)
        if entry is None and self._catalog:
            entry = self._catalog[0]
        if entry is None:
            return False
        selection = TachieSelection(
            pose=entry.pose,
            emotion=entry.emotion,
            variant=entry.variant,
            filename=entry.filename,
            full_path=entry.full_path,
            reason="idle reset",
        )
        self._current_selection = selection
        self._notify_display(selection)
        self._log_info(f"Tachi-e reset to default: {entry.pose}/{entry.emotion}")
        return True

    # ── LLM ──────────────────────────────────────────────────

    async def _select_via_llm(self, *, reply_text: str, context_text: str) -> TachieSelection | None:
        catalog_summary = self._build_catalog_summary()
        provider, model_identifier, model_extra_params = self._resolve_provider_and_model()
        client_config = build_openai_compatible_client_config(provider)
        request_overrides = split_openai_request_overrides(
            {
                **model_extra_params,
                "enable_thinking": bool(self.config.enable_thinking),
            }
        )
        client = AsyncOpenAI(
            api_key=client_config.api_key,
            base_url=client_config.base_url,
            timeout=self.config.timeout_sec,
            max_retries=provider.max_retry,
            default_headers=client_config.default_headers or None,
            default_query=client_config.default_query or None,
        )
        response = await client.chat.completions.create(
            model=model_identifier,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _build_selection_prompt(
                    reply_text=reply_text,
                    context_text=context_text,
                    catalog_summary=catalog_summary,
                )},
            ],
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            response_format={"type": "json_object"},
            extra_headers=request_overrides.extra_headers or None,
            extra_query=request_overrides.extra_query or None,
            extra_body=request_overrides.extra_body or None,
        )
        content = str(response.choices[0].message.content or "") if response.choices else ""
        return _parse_tachie_selection(content, resolver=self._resolve_entry)

    def _build_catalog_summary(self) -> list[dict[str, Any]]:
        poses_map: dict[str, list[str]] = {}
        for entry in self._catalog:
            if entry.pose not in poses_map:
                poses_map[entry.pose] = []
            variants = sorted(set(
                e.variant for e in self._catalog
                if e.pose == entry.pose and e.emotion == entry.emotion
            ))
            label = f"{entry.emotion}" + (f" (variants: {variants})" if len(variants) > 1 else "")
            if label not in poses_map[entry.pose]:
                poses_map[entry.pose].append(label)
        return [
            {"pose": pose, "emotions": emotions}
            for pose, emotions in sorted(poses_map.items())
        ]

    def _resolve_entry(self, *, pose: str, emotion: str, variant: int = 1) -> TachieEntry | None:
        key = f"{pose}/{emotion}/{variant}"
        entry = self._by_key.get(key)
        if entry is not None:
            return entry
        key_fallback = f"{pose}/{emotion}"
        entry = self._by_key.get(key_fallback)
        if entry is not None:
            return entry
        for e in self._catalog:
            if e.emotion == emotion:
                return e
        return None

    # ── display server (aiohttp + SSE) ───────────────────────

    async def _start_display_server(self) -> None:
        try:
            from aiohttp import web
        except ImportError:
            self._log_warning("aiohttp not available; tachi-e display server disabled.")
            return
        app = web.Application()
        app.router.add_get("/", self._handle_display_index)
        app.router.add_get("/events", self._handle_display_events)
        app.router.add_get("/image", self._handle_display_image)
        host = str(self.config.display_host or "127.0.0.1").strip() or "127.0.0.1"
        port = max(1, int(self.config.display_port or 18185))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        self._http_runner = runner
        self._http_site = site
        self._log_info(f"Tachi-e display server listening on http://{host}:{port}")

    async def _stop_display_server(self) -> None:
        # wake all SSE clients
        for q in self._sse_queues:
            q.put_nowait(None)
        self._sse_queues.clear()
        runner = self._http_runner
        self._http_runner = None
        self._http_site = None
        if runner is not None:
            with contextlib.suppress(Exception):
                await runner.cleanup()

    async def _handle_display_index(self, request: Any) -> Any:
        from aiohttp import web
        # serve initial fallback image
        if self._current_selection is not None and os.path.isfile(self._current_selection.full_path):
            initial_src = "/image"
        else:
            initial_src = ""
        html = _TPL_HTML.format(initial_src=initial_src)
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_display_events(self, request: Any) -> Any:
        from aiohttp import web
        response = web.StreamResponse(status=200, reason="OK")
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        response.headers["Access-Control-Allow-Origin"] = "*"
        await response.prepare(request)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=32)
        self._sse_queues.append(queue)
        try:
            # send current state immediately
            if self._current_selection is not None:
                payload = {
                    "pose": self._current_selection.pose,
                    "emotion": self._current_selection.emotion,
                    "reason": self._current_selection.reason,
                }
                await response.write(f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode())
            while True:
                data = await queue.get()
                if data is None:
                    break
                line = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                await response.write(line.encode())
        except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(ValueError):
                self._sse_queues.remove(queue)
        return response

    async def _handle_display_image(self, request: Any) -> Any:
        from aiohttp import web
        if self._current_selection is None or not os.path.isfile(self._current_selection.full_path):
            raise web.HTTPNotFound()
        return web.FileResponse(
            self._current_selection.full_path,
            headers={"Cache-Control": "no-cache"},
        )

    def _notify_display(self, selection: TachieSelection) -> None:
        payload = {
            "pose": selection.pose,
            "emotion": selection.emotion,
            "reason": selection.reason,
        }
        for q in self._sse_queues:
            if not q.full():
                q.put_nowait(payload)

    # ── display window (pywebview subprocess) ────────────────

    def _start_display_window(self) -> None:
        if self._window_process is not None and self._window_process.poll() is None:
            return
        host = str(self.config.display_host or "127.0.0.1").strip() or "127.0.0.1"
        port = max(1, int(self.config.display_port or 18185))
        url = f"http://{host}:{port}/"
        options = {
            "url": url,
            "title": "MaiBot Tachi-e Display",
            "transparent": bool(self.config.window_transparent),
            "frameless": bool(self.config.window_frameless),
            "on_top": bool(self.config.window_on_top),
            "click_through": bool(self.config.window_click_through),
            "width": int(self.config.window_width or 800),
            "height": int(self.config.window_height or 1000),
            "x": int(self.config.window_x or 100),
            "y": int(self.config.window_y or 40),
        }
        script_dir = Path(__file__).resolve().parent
        process_script = script_dir / "_tachie_window.py"
        if not process_script.exists():
            self._log_warning(f"Tachi-e window script not found: {process_script}")
            return
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._window_process = subprocess.Popen(
            [sys.executable, str(process_script), "--options-json", json.dumps(options, ensure_ascii=False)],
            cwd=str(script_dir),
            creationflags=creationflags,
        )
        self._log_info("Tachi-e display window launched.")

    def _stop_display_window(self) -> None:
        process = self._window_process
        self._window_process = None
        if process is None or process.poll() is not None:
            return
        hwnd = int(ctypes.windll.user32.FindWindowW(None, "MaiBot Tachi-e Display"))
        if hwnd != 0:
            with contextlib.suppress(Exception):
                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()

    # ── idle reset ───────────────────────────────────────────

    def _schedule_idle_reset(self) -> None:
        self._cancel_idle_reset()
        delay = self.config.idle_reset_after_sec
        if delay <= 0:
            return
        self._idle_reset_task = asyncio.ensure_future(self._idle_reset_after(delay))

    def _cancel_idle_reset(self) -> None:
        if self._idle_reset_task is not None:
            self._idle_reset_task.cancel()
            self._idle_reset_task = None

    async def _idle_reset_after(self, delay_sec: float) -> None:
        await asyncio.sleep(delay_sec)
        elapsed = time.time() - self._last_reply_at
        if elapsed >= delay_sec - 0.5:
            await self.reset_to_default()

    # ── provider ─────────────────────────────────────────────

    def _resolve_provider_and_model(self) -> tuple[APIProvider, str, dict[str, Any]]:
        model_config = config_manager.get_model_config()
        models_by_name = {model.name: model for model in model_config.models}
        providers_by_name = {provider.name: provider for provider in model_config.api_providers}

        model_info: ModelInfo | None = None
        if self.config.model_name:
            model_info = models_by_name.get(self.config.model_name)
            if model_info is None:
                raise RuntimeError(
                    f"Tachi-e model_name not found in model_config: {self.config.model_name}"
                )

        if model_info is not None:
            provider = providers_by_name.get(model_info.api_provider)
            if provider is None:
                raise RuntimeError(
                    f"Tachi-e provider not found in model_config: {model_info.api_provider}"
                )
            return provider, model_info.model_identifier, dict(model_info.extra_params or {})

        provider = providers_by_name.get(self.config.api_provider)
        if provider is None:
            raise RuntimeError(
                f"Tachi-e api_provider not found in model_config: {self.config.api_provider}"
            )
        model_identifier = str(self.config.model_identifier or "").strip()
        if not model_identifier:
            raise RuntimeError("Tachi-e model_identifier is empty.")
        return provider, model_identifier, {}

    def _log_warning(self, message: str) -> None:
        if self.logger is not None:
            self.logger.warning(f"[Tachie] {message}")

    def _log_info(self, message: str) -> None:
        if self.logger is not None:
            self.logger.info(f"[Tachie] {message}")


# ── helpers ────────────────────────────────────────────────────


def _parse_tachie_filename(filename: str, pose: str, full_path: str) -> TachieEntry | None:
    """Parse a tachi-e filename like 'hands_down_happy1.png' or 'arm_crossed_angry1_nobg.png'."""
    name = Path(filename).stem
    name = re.sub(r"_nobg$", "", name, flags=re.IGNORECASE)
    parts = name.split("_")
    variant = 1
    match = re.search(r"(\d+)$", parts[-1]) if parts else None
    if match:
        variant = int(match.group(1))
        parts[-1] = re.sub(r"\d+$", "", parts[-1])
        if not parts[-1]:
            parts.pop()
    emotion_candidates = [
        "angry", "confused", "disgusting", "fear", "happy",
        "nervous", "sad", "shy", "idle", "surprised", "excited",
    ]
    emotion = "idle"
    for i in range(len(parts) - 1, -1, -1):
        candidate = parts[i].lower()
        if candidate in emotion_candidates:
            emotion = candidate
            break
        if candidate in {"surprise"}:
            emotion = "surprised"
            break
        if candidate in {"excite"}:
            emotion = "excited"
            break
    return TachieEntry(
        pose=pose,
        emotion=emotion,
        variant=variant,
        filename=filename,
        full_path=full_path,
    )


_SYSTEM_PROMPT = (
    "You are a tachi-e (standing picture / 立绘) expression selector for a livestream AI assistant. "
    "Your job is to pick the most fitting character expression and pose based on what the AI is about to say. "
    "Return strict JSON only. Never invent poses or emotions - only use what is available in the catalog."
)


def _build_selection_prompt(
    *,
    reply_text: str,
    context_text: str,
    catalog_summary: list[dict[str, Any]],
) -> str:
    payload = {
        "reply_text": reply_text,
        "context": context_text or "(no additional context)",
        "available": catalog_summary,
    }
    return (
        "Pick the best matching tachi-e expression for this reply.\n"
        "Return strict JSON with keys: pose, emotion, variant, reason.\n"
        "Rules:\n"
        "- pose must match one of the available poses exactly.\n"
        "- emotion must match one of the available emotions for that pose.\n"
        "- variant should be 1 unless the emotion has multiple variants and you have a specific preference.\n"
        "- reason is a short Chinese explanation of why you chose this expression.\n"
        "- Match the emotion to the tone of the reply: happy for positive/cheerful, sad for downbeat/empathetic, "
        "angry for tsundere/frustrated, shy for embarrassed/cute, nervous for anxious/uncertain, "
        "fear for scared/shocked, confused for puzzled, disgusting for grossed out, idle for neutral.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def _parse_tachie_selection(
    response_text: str,
    *,
    resolver: Any,
) -> TachieSelection | None:
    payload = _extract_json_object(response_text)
    pose = str(payload.get("pose") or "").strip()
    emotion = str(payload.get("emotion") or "").strip()
    variant_val = payload.get("variant", 1)
    try:
        variant = int(variant_val)
    except (TypeError, ValueError):
        variant = 1
    reason = str(payload.get("reason") or "").strip()
    if not pose or not emotion:
        return None
    entry = resolver(pose=pose, emotion=emotion, variant=variant)
    if entry is None:
        return None
    return TachieSelection(
        pose=entry.pose,
        emotion=entry.emotion,
        variant=entry.variant,
        filename=entry.filename,
        full_path=entry.full_path,
        reason=reason,
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip()
    if normalized.startswith("```"):
        normalized = re.sub(r"^```(?:json)?", "", normalized.strip(), flags=re.IGNORECASE).strip()
        normalized = re.sub(r"```$", "", normalized).strip()
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        start = normalized.find("{")
        end = normalized.rfind("}")
        if start < 0 or end <= start:
            return {}
        payload = json.loads(normalized[start : end + 1])
    if not isinstance(payload, dict):
        return {}
    return payload
