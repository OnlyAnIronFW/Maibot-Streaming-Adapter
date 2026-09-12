"""Idle-time Bilibili video watch, offline analysis, and timed commentary support."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import shutil
import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

import httpx

from openai import AsyncOpenAI
from PIL import Image

from src.config.config import config_manager
from src.config.model_configs import APIProvider, ModelInfo
from src.llm_models.openai_compat import build_openai_compatible_client_config, split_openai_request_overrides

from .config import VideoWatchConfig, VideoWatchNamedSourceConfig
from .vision_tool import _message_content_to_text, compress_image_for_vision


_JSON_FENCE_RE = re.compile(r"^```(?:json)?|```$", re.IGNORECASE)


class _LoggerProtocol(Protocol):
    def info(self, message: str, *args: Any) -> None:
        ...

    def warning(self, message: str, *args: Any) -> None:
        ...

    def debug(self, message: str, *args: Any) -> None:
        ...


@dataclass(frozen=True)
class VideoMemoryEntry:
    title: str
    text: str
    tags: tuple[str, ...] = ()
    start_sec: float | None = None
    end_sec: float | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VideoMemoryEntry | None":
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        title = str(payload.get("title") or "").strip()
        tags = tuple(_normalize_string_list(payload.get("tags")))
        start_sec = _optional_float(payload.get("start_sec"))
        end_sec = _optional_float(payload.get("end_sec"))
        return cls(title=title, text=text, tags=tags, start_sec=start_sec, end_sec=end_sec)


@dataclass(frozen=True)
class VideoAnalysisCue:
    start_sec: float
    end_sec: float
    title: str
    description: str
    commentary_text: str
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "VideoAnalysisCue | None":
        commentary_text = str(payload.get("commentary_text") or "").strip()
        start_sec = _optional_float(payload.get("start_sec"))
        if start_sec is None or not commentary_text:
            return None
        end_sec = _optional_float(payload.get("end_sec"))
        if end_sec is None or end_sec < start_sec:
            end_sec = start_sec
        return cls(
            start_sec=max(0.0, start_sec),
            end_sec=max(start_sec, end_sec),
            title=str(payload.get("title") or "").strip(),
            description=str(payload.get("description") or "").strip(),
            commentary_text=commentary_text,
            tags=tuple(_normalize_string_list(payload.get("tags"))),
        )


@dataclass(frozen=True)
class VideoAnalysisResult:
    summary: str
    conversation_hooks: tuple[str, ...]
    memory_entries: tuple[VideoMemoryEntry, ...]
    timeline_cues: tuple[VideoAnalysisCue, ...]
    raw_payload: dict[str, Any]
    model_label: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, model_label: str) -> "VideoAnalysisResult | None":
        summary = str(payload.get("video_summary") or payload.get("summary") or "").strip()
        hooks = tuple(_normalize_string_list(payload.get("conversation_hooks")))
        memories = tuple(
            entry
            for entry in (
                VideoMemoryEntry.from_dict(item)
                for item in _as_mapping_sequence(payload.get("memory_entries"))
            )
            if entry is not None
        )
        cues = sorted(
            (
                cue
                for cue in (
                    VideoAnalysisCue.from_dict(item)
                    for item in _as_mapping_sequence(payload.get("timeline_cues"))
                )
                if cue is not None
            ),
            key=lambda item: item.start_sec,
        )
        if not summary and not memories and not cues and not hooks:
            return None
        return cls(
            summary=summary,
            conversation_hooks=hooks,
            memory_entries=memories,
            timeline_cues=tuple(cues),
            raw_payload=dict(payload),
            model_label=model_label,
        )


@dataclass(frozen=True)
class VideoWatchSourceSpec:
    source_type: str
    request_value: str
    page_url: str
    video_url: str
    title: str
    canonical_id: str
    up_name: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def memory_chat_id(self) -> str:
        return f"bilibili-video:{self.canonical_id}"


@dataclass(frozen=True)
class VideoWatchOfferState:
    offered_at: float
    prompt: str


@dataclass
class BrowserPlaybackHandle:
    page_url: str
    started_at: float
    close_callback: Callable[[], Awaitable[None]]

    async def close(self) -> None:
        await self.close_callback()


@dataclass
class ActiveVideoWatchSession:
    session_id: str
    source: VideoWatchSourceSpec
    local_video_path: Path
    analysis: VideoAnalysisResult
    playback: BrowserPlaybackHandle
    spoken_cue_indexes: set[int] = field(default_factory=set)

    def elapsed_sec(self) -> float:
        return max(0.0, time.monotonic() - self.playback.started_at)

    def next_cue(self) -> VideoAnalysisCue | None:
        for index, cue in enumerate(self.analysis.timeline_cues):
            if index not in self.spoken_cue_indexes:
                return cue
        return None


@dataclass(frozen=True)
class VideoWatchStartRequest:
    source_type: str
    value: str
    requested_by: str
    trigger: str


@dataclass(frozen=True)
class SampledVideoFrame:
    timestamp_sec: float
    image_path: Path


RouteReplyCallback = Callable[[str, str, Mapping[str, Any] | None], Awaitable[bool]]
MemoryIngestCallback = Callable[[VideoWatchSourceSpec, VideoAnalysisResult], Awaitable[None]]
BusyCheckCallback = Callable[[], bool]


class VideoWatchAnalysisClient:
    """Analyze a downloaded Bilibili video with a dedicated model."""

    def __init__(self, config: VideoWatchConfig, *, logger: Any = None) -> None:
        self.config = config
        self.logger = logger

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    async def analyze_video(
        self,
        *,
        source: VideoWatchSourceSpec,
        video_path: Path,
        work_dir: Path,
        ffmpeg_command: str,
    ) -> VideoAnalysisResult | None:
        provider, model_identifier, model_extra_params = self._resolve_provider_and_model()
        client_config = build_openai_compatible_client_config(provider)
        request_overrides = split_openai_request_overrides(
            {
                **model_extra_params,
                "enable_thinking": bool(self.config.analysis.enable_thinking),
            }
        )
        client = AsyncOpenAI(
            api_key=client_config.api_key,
            base_url=client_config.base_url,
            timeout=self.config.analysis.timeout_sec,
            max_retries=provider.max_retry,
            default_headers=client_config.default_headers or None,
            default_query=client_config.default_query or None,
        )
        prompt = build_video_analysis_prompt(source=source)
        payload = await self._request_json(
            client=client,
            model_identifier=model_identifier,
            prompt=prompt,
            request_overrides=request_overrides,
            video_path=video_path,
            work_dir=work_dir,
            ffmpeg_command=ffmpeg_command,
        )
        return VideoAnalysisResult.from_dict(payload or {}, model_label=self._configured_model_label())

    async def _request_json(
        self,
        *,
        client: AsyncOpenAI,
        model_identifier: str,
        prompt: str,
        request_overrides: Any,
        video_path: Path,
        work_dir: Path,
        ffmpeg_command: str,
    ) -> dict[str, Any]:
        direct_max_bytes = max(1, int(self.config.analysis.direct_video_max_mb)) * 1024 * 1024
        if video_path.exists() and video_path.stat().st_size <= direct_max_bytes:
            try:
                content = [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": _file_to_data_url(video_path)}},
                ]
                response = await client.chat.completions.create(
                    model=model_identifier,
                    messages=[
                        {"role": "system", "content": self.config.analysis.system_prompt},
                        {"role": "user", "content": content},
                    ],
                    temperature=self.config.analysis.temperature,
                    max_tokens=self.config.analysis.max_tokens,
                    response_format={"type": "json_object"},
                    extra_headers=request_overrides.extra_headers or None,
                    extra_query=request_overrides.extra_query or None,
                    extra_body=request_overrides.extra_body or None,
                )
                return _extract_json_dict(_message_content_to_text(response.choices[0].message.content if response.choices else ""))
            except Exception as exc:
                _log(self.logger, "warning", f"Direct video analysis failed, falling back to sampled frames: {exc}")
        sampled_frames = await extract_sampled_video_frames(
            video_path=video_path,
            work_dir=work_dir / "frames",
            ffmpeg_command=ffmpeg_command,
            frame_interval_sec=self.config.analysis.fallback_frame_interval_sec,
            max_frames=self.config.analysis.fallback_max_frames,
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for frame in sampled_frames:
            image = Image.open(frame.image_path)
            try:
                payload = compress_image_for_vision(image, max_image_edge_px=960, jpeg_quality=65)
            finally:
                image.close()
            content.append({"type": "text", "text": f"Frame timestamp: {frame.timestamp_sec:.2f}s"})
            content.append({"type": "image_url", "image_url": {"url": payload.data_url}})
        response = await client.chat.completions.create(
            model=model_identifier,
            messages=[
                {"role": "system", "content": self.config.analysis.system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=self.config.analysis.temperature,
            max_tokens=self.config.analysis.max_tokens,
            response_format={"type": "json_object"},
            extra_headers=request_overrides.extra_headers or None,
            extra_query=request_overrides.extra_query or None,
            extra_body=request_overrides.extra_body or None,
        )
        return _extract_json_dict(_message_content_to_text(response.choices[0].message.content if response.choices else ""))

    def _configured_model_label(self) -> str:
        return self.config.analysis.model_name or self.config.analysis.model_identifier

    def _resolve_provider_and_model(self) -> tuple[APIProvider, str, dict[str, Any]]:
        model_config = config_manager.get_model_config()
        models_by_name = {model.name: model for model in model_config.models}
        providers_by_name = {provider.name: provider for provider in model_config.api_providers}

        model_info: ModelInfo | None = None
        if self.config.analysis.model_name:
            model_info = models_by_name.get(self.config.analysis.model_name)
            if model_info is None:
                raise RuntimeError(
                    f"Video watch analysis model_name not found in model_config: {self.config.analysis.model_name}"
                )

        if model_info is not None:
            provider = providers_by_name.get(model_info.api_provider)
            if provider is None:
                raise RuntimeError(
                    f"Video watch analysis provider not found in model_config: {model_info.api_provider}"
                )
            return provider, model_info.model_identifier, dict(model_info.extra_params or {})

        provider = providers_by_name.get(self.config.analysis.api_provider)
        if provider is None:
            raise RuntimeError(
                f"Video watch analysis api_provider not found in model_config: {self.config.analysis.api_provider}"
            )
        model_identifier = str(self.config.analysis.model_identifier or "").strip()
        if not model_identifier:
            raise RuntimeError("Video watch analysis model_identifier is empty.")
        return provider, model_identifier, {}


class VideoWatchController:
    """Own the idle video-watch offer, analysis pipeline, and timed commentary."""

    def __init__(
        self,
        config: VideoWatchConfig,
        *,
        route_reply: RouteReplyCallback,
        memory_ingest: MemoryIngestCallback,
        commentary_lane_available: BusyCheckCallback,
        logger: Any = None,
        analysis_client: VideoWatchAnalysisClient | None = None,
        source_resolver: Callable[[VideoWatchStartRequest], Awaitable[VideoWatchSourceSpec]] | None = None,
        video_preparer: Callable[[VideoWatchSourceSpec], Awaitable[Path]] | None = None,
        playback_launcher: Callable[[VideoWatchSourceSpec], Awaitable[BrowserPlaybackHandle]] | None = None,
    ) -> None:
        self.config = config
        self.route_reply = route_reply
        self.memory_ingest = memory_ingest
        self.commentary_lane_available = commentary_lane_available
        self.logger = logger
        self.analysis_client = analysis_client or VideoWatchAnalysisClient(config, logger=logger)
        self._source_resolver = source_resolver
        self._video_preparer = video_preparer
        self._playback_launcher = playback_launcher
        self._offer_state: VideoWatchOfferState | None = None
        self._declined_until = 0.0
        self._preparing_request: VideoWatchStartRequest | None = None
        self._preparing_source: VideoWatchSourceSpec | None = None
        self._active_session: ActiveVideoWatchSession | None = None
        self._prepare_task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def should_block_idle_topic(self) -> bool:
        return self._offer_state is not None or self._preparing_request is not None or self._active_session is not None

    def is_waiting_for_consent(self) -> bool:
        return self._offer_state is not None

    def build_prompt_context(self) -> str:
        if self._active_session is not None:
            session = self._active_session
            elapsed = session.elapsed_sec()
            next_cue = session.next_cue()
            lines = [
                "额外视频上下文：当前直播正在展示并观看一个 B 站视频。",
                "这些信息不是观众弹幕，但如果观众的问题或当前回复合适，可以自然引用。",
                f"当前视频标题：{session.source.title or session.source.video_url}",
                f"视频来源：{session.source.video_url}",
            ]
            if session.source.up_name:
                lines.append(f"UP主：{session.source.up_name}")
            if session.analysis.summary:
                lines.append(f"视频整体解析：{session.analysis.summary}")
            lines.append(f"当前播放进度（约）：{elapsed:.1f} 秒")
            if next_cue is not None:
                lines.append(
                    f"下一个预定解说点：{next_cue.start_sec:.1f} 秒，主题={next_cue.title or next_cue.description or '未命名'}"
                )
            if session.analysis.conversation_hooks:
                lines.append(f"可复用话题钩子：{', '.join(session.analysis.conversation_hooks[:4])}")
            return "\n".join(lines).strip()
        if self._preparing_source is not None:
            source = self._preparing_source
            return (
                "额外视频上下文：当前正在后台解析一个准备观看的 B 站视频。\n"
                f"候选视频：{source.title or source.video_url}\n"
                f"来源：{source.video_url}\n"
                "在解析完成前，不要假装你已经看完了它。"
            )
        return ""

    async def stop(self) -> None:
        self._offer_state = None
        self._declined_until = 0.0
        self._preparing_request = None
        self._preparing_source = None
        task = self._prepare_task
        self._prepare_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        session = self._active_session
        self._active_session = None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.playback.close()

    async def maybe_offer(self) -> bool:
        if not (self.enabled and self.config.auto_offer_enabled):
            return False
        if self._offer_state is not None:
            return True
        if self._preparing_request is not None or self._active_session is not None:
            return True
        if time.time() < self._declined_until:
            return False
        prompt = str(self.config.offer_prompt or "").strip()
        if not prompt:
            return False
        accepted = await self.route_reply(
            prompt,
            "video_watch_offer",
            {"video_watch_offer": True},
        )
        if not accepted:
            return False
        self._offer_state = VideoWatchOfferState(offered_at=time.time(), prompt=prompt)
        return True

    async def handle_manual_command(
        self,
        *,
        command_text: str,
        requested_by: str,
        trigger: str,
    ) -> bool:
        if not self.enabled:
            return False
        action = parse_video_watch_command(
            command_text=command_text,
            prefix=self.config.command.prefix,
            stop_aliases=self.config.command.stop_aliases,
            status_aliases=self.config.command.status_aliases,
        )
        if action["action"] == "status":
            await self.route_reply(self.status_text(), "video_watch_status", {"video_watch_status": True})
            return True
        if action["action"] == "stop":
            await self.stop()
            await self.route_reply("视频观看模式已停止。", "video_watch_stop", {"video_watch_stop": True})
            return True
        request = VideoWatchStartRequest(
            source_type=str(action["source_type"] or "recommend"),
            value=str(action["value"] or ""),
            requested_by=requested_by,
            trigger=trigger,
        )
        return await self.start_request(request)

    async def handle_consent_reply(self, *, text: str, requested_by: str) -> bool:
        if not self.enabled or self._offer_state is None:
            return False
        normalized = normalize_watch_reply(text)
        if not normalized:
            return False
        yes_aliases = {item.casefold() for item in self.config.yes_aliases if item}
        no_aliases = {item.casefold() for item in self.config.no_aliases if item}
        token = normalized.casefold()
        if token in yes_aliases:
            self._offer_state = None
            request = VideoWatchStartRequest(
                source_type=str(self.config.sources.default_mode or "recommend"),
                value="",
                requested_by=requested_by,
                trigger="chat_consent",
            )
            return await self.start_request(request)
        if token in no_aliases:
            self._offer_state = None
            self._declined_until = time.time() + max(0.0, float(self.config.decline_cooldown_sec))
            await self.route_reply("收到，那这轮先不放视频，继续正常聊。", "video_watch_declined", {"video_watch_declined": True})
            return True
        return False

    async def start_request(self, request: VideoWatchStartRequest) -> bool:
        if not self.enabled:
            return False
        await self.stop()
        self._offer_state = None
        self._preparing_request = request
        await self.route_reply(
            "收到，我先去抓视频并解析，准备好后再开始放和解说。",
            "video_watch_prepare",
            {"video_watch_prepare": True, "video_watch_source_type": request.source_type},
        )
        self._prepare_task = asyncio.create_task(
            self._run_prepare_pipeline(request),
            name="maibot_bilibili_live_adapter.video_watch_prepare",
        )
        return True

    def status_text(self) -> str:
        if self._active_session is not None:
            session = self._active_session
            next_cue = session.next_cue()
            summary = [
                f"当前正在看《{session.source.title or session.source.video_url}》。",
                f"当前进度约 {session.elapsed_sec():.1f} 秒。",
            ]
            if next_cue is not None:
                summary.append(f"下一个解说点在 {next_cue.start_sec:.1f} 秒。")
            return " ".join(summary)
        if self._preparing_source is not None:
            return f"正在解析《{self._preparing_source.title or self._preparing_source.video_url}》，准备好后会开始播放。"
        if self._offer_state is not None:
            return "当前正在等弹幕决定要不要开启视频观看。"
        return "当前没有视频观看任务。"

    async def _run_prepare_pipeline(self, request: VideoWatchStartRequest) -> None:
        work_dir = self._work_dir()
        work_dir.mkdir(parents=True, exist_ok=True)
        playback: BrowserPlaybackHandle | None = None
        try:
            source = await self._resolve_source(request)
            self._preparing_source = source
            await self.route_reply(
                f"这轮准备看《{source.title or source.video_url}》，我先把内容过一遍再放。",
                "video_watch_prepare_source",
                {"video_watch_prepare_source": True, "video_url": source.video_url},
            )
            local_video_path = await self._prepare_video(source)
            analysis = await self.analysis_client.analyze_video(
                source=source,
                video_path=local_video_path,
                work_dir=work_dir,
                ffmpeg_command=self._ffmpeg_command(),
            )
            if analysis is None:
                raise RuntimeError("video analysis returned no usable summary or cues")
            await self.memory_ingest(source, analysis)
            if self._preparing_request != request:
                return
            await self.route_reply(
                f"《{source.title or source.video_url}》解析完了，开始播放并按时间轴解说。",
                "video_watch_playback_start",
                {"video_watch_playback_start": True, "video_url": source.video_url},
            )
            playback = await self._launch_playback(source)
            session = ActiveVideoWatchSession(
                session_id=f"video-watch-{int(time.time())}-{source.canonical_id}",
                source=source,
                local_video_path=local_video_path,
                analysis=analysis,
                playback=playback,
            )
            self._active_session = session
            self._preparing_request = None
            self._preparing_source = None
            await self._run_commentary_schedule(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(self.logger, "warning", f"Video watch pipeline failed: {exc}")
            await self.route_reply(
                f"这轮视频观看没准备起来：{exc}",
                "video_watch_error",
                {"video_watch_error": True},
            )
        finally:
            if self._active_session is not None and playback is not None and self._active_session.playback is playback:
                with contextlib.suppress(Exception):
                    await playback.close()
                self._active_session = None
            elif playback is not None:
                with contextlib.suppress(Exception):
                    await playback.close()
            self._preparing_request = None
            self._preparing_source = None
            if self._prepare_task is asyncio.current_task():
                self._prepare_task = None

    async def _run_commentary_schedule(self, session: ActiveVideoWatchSession) -> None:
        timeline_cues = list(session.analysis.timeline_cues)
        max_lateness = max(0.0, float(self.config.max_cue_lateness_sec))
        for index, cue in enumerate(timeline_cues):
            target = session.playback.started_at + cue.start_sec
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if self._active_session is not session:
                return
            lateness = time.monotonic() - target
            if lateness >= max_lateness:
                _log(
                    self.logger,
                    "info",
                    f"Video watch cue skipped because it is late: title={cue.title!r} lateness_sec={lateness:.2f}",
                )
                session.spoken_cue_indexes.add(index)
                continue
            if self.commentary_lane_available():
                _log(
                    self.logger,
                    "info",
                    f"Video watch cue skipped because live reply lane is busy: title={cue.title!r}",
                )
                session.spoken_cue_indexes.add(index)
                continue
            accepted = await self.route_reply(
                cue.commentary_text,
                "video_watch_commentary",
                {
                    "video_watch_commentary": True,
                    "video_url": session.source.video_url,
                    "video_title": session.source.title,
                    "video_cue_title": cue.title,
                    "video_cue_start_sec": cue.start_sec,
                },
            )
            if accepted:
                session.spoken_cue_indexes.add(index)
        duration_sec = _optional_float(session.source.metadata.get("duration_sec")) or 0.0
        trailing_sec = max(0.0, duration_sec - session.elapsed_sec())
        if trailing_sec > 0:
            await asyncio.sleep(trailing_sec)
        if self._active_session is session:
            self._active_session = None

    async def _resolve_source(self, request: VideoWatchStartRequest) -> VideoWatchSourceSpec:
        if self._source_resolver is not None:
            return await self._source_resolver(request)
        if request.source_type == "recommend":
            return await self._resolve_recommend_source(request)
        return await self._resolve_specific_source(request)

    async def _prepare_video(self, source: VideoWatchSourceSpec) -> Path:
        if self._video_preparer is not None:
            return await self._video_preparer(source)
        return await self._download_public_video(source)

    async def _launch_playback(self, source: VideoWatchSourceSpec) -> BrowserPlaybackHandle:
        if self._playback_launcher is not None:
            return await self._playback_launcher(source)
        return await self._open_playback_browser(source)

    async def _resolve_recommend_source(self, request: VideoWatchStartRequest) -> VideoWatchSourceSpec:
        del request
        payload = await self._scrape_page(self.config.sources.recommend_url)
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        if not candidates:
            raise RuntimeError("no Bilibili video candidates were found on the recommendation page")
        first = candidates[0]
        video_url = str(first.get("href") or "").strip()
        if not video_url:
            raise RuntimeError("recommendation candidate is missing href")
        return await self._resolve_specific_source(
            VideoWatchStartRequest(
                source_type="url",
                value=video_url,
                requested_by="auto_recommend",
                trigger="idle_auto_offer",
            )
        )

    async def _resolve_specific_source(self, request: VideoWatchStartRequest) -> VideoWatchSourceSpec:
        page_url = self._request_to_page_url(request)
        payload = await self._scrape_page(page_url)
        candidates = payload.get("candidates") if isinstance(payload.get("candidates"), list) else []
        playinfo = payload.get("playinfo")
        if (
            request.source_type in {"up", "fav", "playlist"}
            or (not playinfo and candidates)
        ):
            video_url = str((candidates[0] or {}).get("href") or "").strip()
            if video_url:
                return await self._resolve_specific_source(
                    VideoWatchStartRequest(
                        source_type="url",
                        value=video_url,
                        requested_by=request.requested_by,
                        trigger=request.trigger,
                    )
                )
        page_state = payload.get("page_state") if isinstance(payload.get("page_state"), Mapping) else {}
        title = str(page_state.get("title") or payload.get("title") or "").strip()
        canonical_url = str(payload.get("url") or page_url).strip() or page_url
        bvid = str(page_state.get("bvid") or payload.get("bvid") or "").strip()
        avid = str(page_state.get("avid") or "").strip()
        canonical_id = bvid or avid or _hash_text(canonical_url)[:16]
        up_name = str(page_state.get("up_name") or page_state.get("owner_name") or "").strip()
        metadata = {
            "cookies": payload.get("cookies") if isinstance(payload.get("cookies"), str) else "",
            "user_agent": payload.get("user_agent") if isinstance(payload.get("user_agent"), str) else "",
            "playinfo": dict(payload.get("playinfo") or {}) if isinstance(payload.get("playinfo"), Mapping) else {},
            "duration_sec": _optional_float(page_state.get("duration_sec") or payload.get("duration_sec")),
            "bvid": bvid,
            "avid": avid,
            "up_name": up_name,
            "source_payload": dict(page_state) if isinstance(page_state, Mapping) else {},
        }
        return VideoWatchSourceSpec(
            source_type=request.source_type,
            request_value=request.value,
            page_url=page_url,
            video_url=canonical_url,
            title=title or canonical_url,
            canonical_id=canonical_id,
            up_name=up_name,
            metadata=metadata,
        )

    def _request_to_page_url(self, request: VideoWatchStartRequest) -> str:
        source_type = str(request.source_type or "").strip().lower() or "recommend"
        value = str(request.value or "").strip()
        if source_type == "recommend":
            return self.config.sources.recommend_url
        if source_type == "url":
            if not value:
                raise RuntimeError("video URL is required")
            return value
        if source_type == "up":
            if value.startswith("http://") or value.startswith("https://"):
                return value
            return f"https://space.bilibili.com/{value}/video"
        if source_type in {"fav", "playlist"}:
            if value.startswith("http://") or value.startswith("https://"):
                return value
            named = self._resolve_named_source(value)
            if named is not None:
                return named.value
            raise RuntimeError(f"{source_type} requires a full URL or a configured named source alias")
        if source_type == "named":
            named = self._resolve_named_source(value)
            if named is None:
                raise RuntimeError(f"named video source not found: {value}")
            nested_request = VideoWatchStartRequest(
                source_type=named.source_type or "url",
                value=named.value,
                requested_by=request.requested_by,
                trigger=request.trigger,
            )
            return self._request_to_page_url(nested_request)
        raise RuntimeError(f"unsupported video-watch source_type: {source_type}")

    def _resolve_named_source(self, alias: str) -> VideoWatchNamedSourceConfig | None:
        normalized_alias = str(alias or "").strip().casefold()
        if not normalized_alias:
            return None
        for source in self.config.sources.named_sources:
            if str(source.name or "").strip().casefold() == normalized_alias:
                return source
        return None

    async def _scrape_page(self, url: str) -> dict[str, Any]:
        from playwright.async_api import async_playwright

        timeout_ms = int(max(5.0, float(self.config.sources.page_timeout_sec)) * 1000)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=not self.config.sources.visible_browser)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(1500)
                await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except Exception:
                # Some Bilibili pages keep long-lived connections and may never reach full network idle.
                pass
            payload = await page.evaluate(
                """(scanLimit) => {
                    const normalizeHref = (href) => {
                        if (!href) return "";
                        try {
                            return new URL(href, location.href).href;
                        } catch (_error) {
                            return href;
                        }
                    };
                    const anchors = Array.from(document.querySelectorAll('a[href*="/video/"]'));
                    const candidates = [];
                    const seen = new Set();
                    for (const anchor of anchors) {
                        const href = normalizeHref(anchor.href || anchor.getAttribute("href"));
                        if (!href || seen.has(href)) continue;
                        seen.add(href);
                        const title = (anchor.title || anchor.textContent || "").trim();
                        candidates.push({ href, title });
                        if (candidates.length >= scanLimit) break;
                    }
                    const initial = window.__INITIAL_STATE__ || {};
                    const playinfo = window.__playinfo__ || {};
                    const videoData = initial.videoData || {};
                    const owner = videoData.owner || initial.upData || {};
                    const durationSec =
                        Number(videoData.duration || 0) ||
                        Number((document.querySelector("video") || {}).duration || 0) ||
                        0;
                    return {
                        url: location.href,
                        title: document.title || "",
                        candidates,
                        user_agent: navigator.userAgent || "",
                        page_state: {
                            title: videoData.title || document.title || "",
                            bvid: videoData.bvid || "",
                            avid: videoData.aid ? String(videoData.aid) : "",
                            up_name: owner.name || owner.uname || "",
                            duration_sec: durationSec,
                        },
                        playinfo,
                    };
                }""",
                int(self.config.sources.candidate_scan_limit),
            )
            cookies = await context.cookies()
            payload["cookies"] = "; ".join(
                f"{str(item.get('name') or '').strip()}={str(item.get('value') or '').strip()}"
                for item in cookies
                if str(item.get("name") or "").strip()
            )
            await browser.close()
            return dict(payload or {})

    async def _download_public_video(self, source: VideoWatchSourceSpec) -> Path:
        work_dir = self._work_dir() / source.canonical_id
        work_dir.mkdir(parents=True, exist_ok=True)
        playinfo = source.metadata.get("playinfo") if isinstance(source.metadata.get("playinfo"), Mapping) else {}
        playinfo_data = {}
        if isinstance(playinfo, Mapping):
            playinfo_data = dict(playinfo.get("data") or playinfo.get("result") or playinfo)
        headers = {
            "User-Agent": str(source.metadata.get("user_agent") or ""),
            "Referer": source.page_url,
        }
        cookies = str(source.metadata.get("cookies") or "").strip()
        if cookies:
            headers["Cookie"] = cookies
        durl_list = playinfo_data.get("durl")
        if isinstance(durl_list, list) and durl_list:
            media_url = str((durl_list[0] or {}).get("url") or "").strip()
            if not media_url:
                raise RuntimeError("Bilibili progressive stream URL is missing")
            output_path = work_dir / f"{_safe_filename(source.canonical_id)}.mp4"
            await _download_binary(media_url, output_path, headers=headers)
            return output_path
        dash = playinfo_data.get("dash") if isinstance(playinfo_data.get("dash"), Mapping) else {}
        video_streams = dash.get("video") if isinstance(dash, Mapping) else []
        audio_streams = dash.get("audio") if isinstance(dash, Mapping) else []
        video_url = ""
        audio_url = ""
        if isinstance(video_streams, list) and video_streams:
            video_url = str((video_streams[0] or {}).get("baseUrl") or (video_streams[0] or {}).get("base_url") or "").strip()
        if isinstance(audio_streams, list) and audio_streams:
            audio_url = str((audio_streams[0] or {}).get("baseUrl") or (audio_streams[0] or {}).get("base_url") or "").strip()
        if not video_url:
            raise RuntimeError("Bilibili DASH video stream URL is missing")
        video_part = work_dir / "video_part.m4s"
        await _download_binary(video_url, video_part, headers=headers)
        if not audio_url:
            output_path = work_dir / f"{_safe_filename(source.canonical_id)}.mp4"
            shutil.copyfile(video_part, output_path)
            return output_path
        audio_part = work_dir / "audio_part.m4s"
        await _download_binary(audio_url, audio_part, headers=headers)
        output_path = work_dir / f"{_safe_filename(source.canonical_id)}.mp4"
        await _merge_dash_streams(
            ffmpeg_command=self._ffmpeg_command(),
            video_part=video_part,
            audio_part=audio_part,
            output_path=output_path,
        )
        return output_path

    async def _open_playback_browser(self, source: VideoWatchSourceSpec) -> BrowserPlaybackHandle:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=not self.config.sources.visible_browser)
        context = await browser.new_context()
        page = await context.new_page()
        timeout_ms = int(max(5.0, float(self.config.sources.page_timeout_sec)) * 1000)
        await page.goto(source.video_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1500)
        if self.config.sources.mute_playback:
            with contextlib.suppress(Exception):
                await page.evaluate(
                    """() => {
                        const video = document.querySelector("video");
                        if (video) {
                            video.muted = true;
                            video.volume = 0;
                        }
                    }"""
                )
        with contextlib.suppress(Exception):
            await page.evaluate(
                """() => {
                    const video = document.querySelector("video");
                    if (video) {
                        video.play().catch(() => {});
                    }
                }"""
            )
        started_at = time.monotonic()

        async def _close() -> None:
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()
            with contextlib.suppress(Exception):
                await playwright.stop()

        return BrowserPlaybackHandle(page_url=source.video_url, started_at=started_at, close_callback=_close)

    def _work_dir(self) -> Path:
        configured = str(self.config.work_dir or "").strip()
        if configured:
            return Path(configured)
        return Path(__file__).resolve().parent / "data" / "video_watch"

    def _ffmpeg_command(self) -> str:
        configured = str(self.config.ffmpeg_command or "").strip()
        return configured or "ffmpeg"


def parse_video_watch_command(
    *,
    command_text: str,
    prefix: str,
    stop_aliases: Sequence[str],
    status_aliases: Sequence[str],
) -> dict[str, str]:
    normalized = str(command_text or "").strip()
    if not normalized.startswith(prefix):
        return {"action": "", "source_type": "", "value": ""}
    remainder = normalized[len(prefix) :].strip()
    if not remainder:
        return {"action": "start", "source_type": "recommend", "value": ""}
    lowered = remainder.casefold()
    if lowered in {item.casefold() for item in stop_aliases if item}:
        return {"action": "stop", "source_type": "", "value": ""}
    if lowered in {item.casefold() for item in status_aliases if item}:
        return {"action": "status", "source_type": "", "value": ""}
    parts = remainder.split(maxsplit=1)
    head = parts[0].strip().casefold()
    tail = parts[1].strip() if len(parts) > 1 else ""
    if head == "recommend":
        return {"action": "start", "source_type": "recommend", "value": ""}
    if head == "up":
        return {"action": "start", "source_type": "up", "value": tail}
    if head in {"fav", "favorite", "favorites"}:
        return {"action": "start", "source_type": "fav", "value": tail}
    if head in {"playlist", "list", "collection"}:
        return {"action": "start", "source_type": "playlist", "value": tail}
    if head in {"url", "video"}:
        return {"action": "start", "source_type": "url", "value": tail}
    if head in {"source", "alias"}:
        return {"action": "start", "source_type": "named", "value": tail}
    if remainder.startswith("http://") or remainder.startswith("https://"):
        return {"action": "start", "source_type": "url", "value": remainder}
    return {"action": "start", "source_type": "named", "value": remainder}


def normalize_watch_reply(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    normalized = normalized.replace("，", " ").replace(",", " ").replace("。", " ").replace("!", " ").replace("！", " ")
    parts = [part for part in normalized.split() if part]
    if len(parts) == 1:
        return parts[0]
    return ""


def build_video_analysis_prompt(*, source: VideoWatchSourceSpec) -> str:
    payload = {
        "video_title": source.title,
        "video_url": source.video_url,
        "up_name": source.up_name,
        "video_id": source.canonical_id,
        "metadata": source.metadata,
    }
    return (
        "Analyze this Bilibili video for a livestream host and return strict JSON only.\n"
        "JSON keys:\n"
        "- video_summary: a practical overall summary for the host.\n"
        "- conversation_hooks: array of short chat hooks the host can reuse later.\n"
        "- memory_entries: array of objects with title, text, tags, start_sec, end_sec. "
        "Multiple entries are allowed. Extract jokes, lore, facts, recurring bits, terminology, and reusable references. "
        "These memories must stay associated with the video source instead of a livestream chat.\n"
        "- timeline_cues: array of objects with start_sec, end_sec, title, description, commentary_text, tags. "
        "commentary_text should be concise and directly speakable by the host during playback. "
        "Prefer one or two lively sentences, not essays.\n"
        "Requirements:\n"
        "- Keep timeline cues strongly grounded in what is on screen around that time.\n"
        "- commentary_text should sound like live commentary, not a formal summary.\n"
        "- If the video contains notable memes, bits, or weird callbacks, capture them in both memory_entries and relevant cues.\n"
        "- If there are useful facts or definitions, store them as reusable memory entries.\n"
        "- Use Chinese by default unless the video itself is clearly dominated by another language.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


async def extract_sampled_video_frames(
    *,
    video_path: Path,
    work_dir: Path,
    ffmpeg_command: str,
    frame_interval_sec: int,
    max_frames: int,
) -> list[SampledVideoFrame]:
    work_dir.mkdir(parents=True, exist_ok=True)
    frame_pattern = work_dir / "frame_%03d.jpg"
    fps_filter = f"fps=1/{max(1, int(frame_interval_sec))}"
    process = await asyncio.create_subprocess_exec(
        ffmpeg_command,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        fps_filter,
        "-frames:v",
        str(max(1, int(max_frames))),
        str(frame_pattern),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {stderr.decode('utf-8', errors='ignore').strip()}")
    frames: list[SampledVideoFrame] = []
    for index, path in enumerate(sorted(work_dir.glob("frame_*.jpg")), start=0):
        frames.append(SampledVideoFrame(timestamp_sec=float(index * max(1, int(frame_interval_sec))), image_path=path))
    return frames


async def _download_binary(url: str, output_path: Path, *, headers: Mapping[str, str]) -> None:
    timeout = httpx.Timeout(connect=20.0, read=120.0, write=120.0, pool=20.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=dict(headers)) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with output_path.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    if chunk:
                        handle.write(chunk)


async def _merge_dash_streams(
    *,
    ffmpeg_command: str,
    video_part: Path,
    audio_part: Path,
    output_path: Path,
) -> None:
    process = await asyncio.create_subprocess_exec(
        ffmpeg_command,
        "-y",
        "-i",
        str(video_part),
        "-i",
        str(audio_part),
        "-c",
        "copy",
        str(output_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg merge failed: {stderr.decode('utf-8', errors='ignore').strip()}")


def _file_to_data_url(path: Path) -> str:
    import base64

    mime_type = "video/mp4"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _extract_json_dict(text: str) -> dict[str, Any]:
    normalized = _JSON_FENCE_RE.sub("", str(text or "").strip()).strip()
    if not normalized:
        return {}
    try:
        payload = json.loads(normalized)
    except json.JSONDecodeError:
        start = normalized.find("{")
        end = normalized.rfind("}")
        if start < 0 or end <= start:
            raise
        payload = json.loads(normalized[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("video watch analysis response must be a JSON object")
    return payload


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        items = [value]
    elif isinstance(value, Sequence):
        items = list(value)
    else:
        items = [value]
    normalized: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text:
            normalized.append(text)
    return normalized


def _as_mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_filename(text: str) -> str:
    normalized = re.sub(r'[\\/:*?"<>|]+', "_", str(text or "").strip())
    normalized = re.sub(r"\s+", "_", normalized)
    return normalized[:80] or "video"


def _hash_text(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()


def _log(logger: Any, level: str, message: str) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None)
    if callable(method):
        method(message)
