"""Message conversion between Bilibili live events and MaiBot MessageDict."""

from __future__ import annotations

from typing import Any, Mapping

import math
import re
import time
from uuid import uuid4

from .config import LiveAdapterSettings
from .constants import PLATFORM_NAME

_LIVE_IDENTITY_OVERRIDES: dict[str, dict[str, str]] = {}


_MODEL_RESERVED_TOKEN_RE = re.compile(r"<[|｜][^<>\r\n]{0,128}[|｜]>")


def sanitize_model_reserved_tokens(text: str) -> str:
    """Remove LLM provider control tokens that make chat messages invalid."""

    sanitized = _MODEL_RESERVED_TOKEN_RE.sub(" ", str(text or ""))
    sanitized = re.sub(r"[ \t\f\v]+", " ", sanitized)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()


def build_message_dict(event: Mapping[str, Any], settings: LiveAdapterSettings, *, reason: str = "") -> dict[str, Any]:
    """Build a MaiBot MessageDict from a selected live event."""

    source_user_id = str(event.get("user_id") or "anonymous").strip()
    source_username = str(event.get("username") or source_user_id).strip()
    user_id, username = resolve_live_identity(
        user_id=source_user_id,
        username=source_username,
    )
    room_id = str(settings.bilibili.room_id)
    original_text = sanitize_model_reserved_tokens(str(event.get("text") or event.get("summary") or ""))

    # Smart mention detection — check original text before adding prefixes
    bot_names = _collect_bot_names(settings)
    is_mentioned = _detect_bot_mention(original_text, bot_names)

    # Build context danmaku prefix for referent resolution
    context_danmaku = event.get("context_danmaku")
    context_prefix = ""
    if isinstance(context_danmaku, list) and context_danmaku:
        context_lines: list[str] = []
        for ctx in context_danmaku:
            if not isinstance(ctx, Mapping):
                continue
            ctx_username = str(ctx.get("username") or "anonymous").strip()
            ctx_text = sanitize_model_reserved_tokens(str(ctx.get("text") or ""))
            if ctx_text:
                context_lines.append(f"[上文 {ctx_username}] {ctx_text}")
        if context_lines:
            context_prefix = "\n".join(context_lines) + "\n"

    # Add role label prefix to distinguish viewer danmaku from bot speech
    event_type = str(event.get("type") or "")
    if event_type in ("super_chat", "gift", "guard"):
        role_prefix = "[醒目留言] "
    elif event_type in ("idle_topic",):
        role_prefix = ""
    else:
        role_prefix = "[弹幕] "
    text = context_prefix + role_prefix + original_text

    message_id = str(event.get("event_id") or f"bilibili-live-{uuid4().hex}").strip()
    timestamp = _normalize_epoch_seconds(event.get("timestamp"))
    additional_config = {
        "platform_io_account_id": settings.identity.bot_user_id,
        "platform_io_scope": settings.route_scope(),
        "live_event_type": str(event.get("type") or ""),
        "live_selection_reason": reason,
        # 会话归属由宿主按 platform_io_* 路由键解析，不再自算 session hash
        "maibot_memory_platform": "qq",
        "maibot_memory_user_id": user_id,
        "maibot_memory_group_id": room_id,
        "maibot_local_render_only": True,
    }
    hub_other_bot_names = _extract_hub_other_bot_names(event)
    hub_active_bot_count = _extract_hub_active_bot_count(event)
    if hub_active_bot_count > 1:
        additional_config["hub_multi_ai"] = True
        additional_config["hub_active_bot_count"] = hub_active_bot_count
        if hub_other_bot_names:
            additional_config["hub_other_bot_names"] = hub_other_bot_names
    live2d_action = event.get("live2d_action")
    if isinstance(live2d_action, Mapping):
        additional_config["live2d_action"] = dict(live2d_action)
    if user_id != source_user_id:
        additional_config["live_source_user_id"] = source_user_id
    if username != source_username:
        additional_config["live_source_username"] = source_username
    if bool(event.get("_soundboard_request_detected")):
        additional_config["soundboard_request_detected"] = True
        request_mode = str(event.get("_soundboard_request_mode") or "").strip()
        request_text = sanitize_model_reserved_tokens(str(event.get("_soundboard_request_text") or "")).strip()
        requested_cue = str(event.get("_soundboard_requested_cue") or "").strip()
        if request_mode:
            additional_config["soundboard_request_mode"] = request_mode
        if request_text:
            additional_config["soundboard_request_text"] = request_text
        if requested_cue:
            additional_config["soundboard_requested_cue"] = requested_cue
    message = {
        "message_id": message_id,
        "timestamp": str(timestamp),
        "platform": PLATFORM_NAME,
        "message_info": {
            "user_info": {
                "user_id": user_id,
                "user_nickname": username,
                "user_cardname": None,
            },
            "group_info": {
                "group_id": room_id,
                "group_name": _build_live_group_name(room_id, hub_other_bot_names, hub_active_bot_count=hub_active_bot_count),
            },
            "additional_config": additional_config,
        },
        "raw_message": [{"type": "text", "data": text}],
        "is_mentioned": is_mentioned,
        "is_at": is_mentioned,
        "is_emoji": False,
        "is_picture": False,
        "is_command": original_text.startswith("/"),
        "is_notify": False,
        "session_id": "",
        "processed_plain_text": text,
        "display_message": text,
    }
    if isinstance(live2d_action, Mapping):
        message["live2d_action"] = dict(live2d_action)
    return message


def build_sts2_message_dict(
    settings: LiveAdapterSettings,
    *,
    text: str,
    event_type: str,
    decision_id: str = "",
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a high-priority STS2 system message for MaiBot narration."""

    normalized_text = sanitize_model_reserved_tokens(str(text or ""))
    normalized_event_type = str(event_type or "sts2").strip() or "sts2"
    message_id = f"bilibili-live-{normalized_event_type}-{uuid4().hex}"
    room_id = str(settings.bilibili.room_id)
    additional_config: dict[str, Any] = {
        "platform_io_account_id": settings.identity.bot_user_id,
        "platform_io_scope": settings.route_scope(),
        "live_event_type": normalized_event_type,
        "live_selection_reason": "sts2_priority",
        # 会话归属由宿主按 platform_io_* 路由键解析，不再自算 session hash
        "maibot_memory_platform": "qq",
        "maibot_memory_user_id": "sts2-player",
        "maibot_memory_group_id": room_id,
        "maibot_local_render_only": True,
        "sts2_priority": True,
        "sts2_payload": dict(payload or {}),
    }
    normalized_decision_id = str(decision_id or "").strip()
    if normalized_decision_id:
        additional_config["sts2_decision_id"] = normalized_decision_id
    return {
        "message_id": message_id,
        "timestamp": str(time.time()),
        "platform": PLATFORM_NAME,
        "message_info": {
            "user_info": {
                "user_id": "sts2-player",
                "user_nickname": "STS2",
                "user_cardname": None,
            },
            "group_info": {
                "group_id": room_id,
                "group_name": f"bilibili_live_{room_id}",
            },
            "additional_config": additional_config,
        },
        "raw_message": [{"type": "text", "data": normalized_text}],
        "is_mentioned": True,
        "is_at": True,
        "is_emoji": False,
        "is_picture": False,
        "is_command": False,
        "is_notify": False,
        "session_id": "",
        "processed_plain_text": normalized_text,
        "display_message": normalized_text,
    }


def build_local_voice_message_dict(
    settings: LiveAdapterSettings,
    *,
    text: str,
    event_id: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a direct-injection MessageDict for local microphone transcripts."""

    local_voice = settings.local_voice
    normalized_text = sanitize_model_reserved_tokens(str(text or ""))
    normalized_event_id = str(event_id or f"local-voice-{uuid4().hex}").strip()
    event = {
        "event_id": normalized_event_id,
        "type": "local_voice",
        "text": normalized_text,
        "summary": normalized_text,
        "user_id": local_voice.speaker_user_id,
        "username": local_voice.speaker_username,
        "timestamp": time.time(),
    }
    message = build_message_dict(event, settings, reason="local_voice_priority")
    prompt_text = build_local_voice_self_judgment_prompt(settings, normalized_text)
    message["raw_message"] = [{"type": "text", "data": prompt_text}]
    additional_config = message["message_info"]["additional_config"]
    additional_config["local_voice_input"] = True
    additional_config["local_voice_priority"] = True
    additional_config["local_voice_self_judgment"] = True
    additional_config["local_voice_original_text"] = normalized_text
    if metadata:
        additional_config["local_voice_metadata"] = dict(metadata)
    return message


def build_local_voice_self_judgment_prompt(settings: LiveAdapterSettings, text: str) -> str:
    """Wrap local microphone speech in a lightweight instruction so MaiBot can decide whether to reply."""

    normalized_text = sanitize_model_reserved_tokens(str(text or ""))
    direct_names = _collect_local_voice_direct_names(settings)
    direct_name_hint = ""
    if direct_names:
        direct_name_hint = f"例如明确叫你 {', '.join(direct_names[:4])}。"
    return (
        "这是主播的实时口播，不一定是在对你说。\n"
        "你必须先判断这句话是不是明确在对你说：\n"
        f"- 只有在主播明确点名你、直接问你、要求你回应时，才回复。{direct_name_hint}\n"
        "- 如果明确是在和直播间观众或弹幕说话、控场、念流程、测试设备、复述内容，或者只是自言自语，就不要回复。\n"
        "- 如果无法确定，也不要回复。\n"
        "- 当你决定不回复时，直接不产生任何回复内容，不要调用 reply，也不要输出任何说明、状态描述或占位文本。\n"
        f"主播原话：{normalized_text}"
    ).strip()


def _collect_local_voice_direct_names(settings: LiveAdapterSettings) -> list[str]:
    return _collect_bot_names(settings)


def _extract_hub_other_bot_names(event: Mapping[str, Any]) -> list[str]:
    raw_names = event.get("hub_other_bot_names")
    if not isinstance(raw_names, list):
        return []
    normalized_names: list[str] = []
    seen: set[str] = set()
    for item in raw_names:
        name = sanitize_model_reserved_tokens(str(item or "")).strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized_names.append(name)
    return normalized_names


def _extract_hub_active_bot_count(event: Mapping[str, Any]) -> int:
    try:
        count = int(event.get("hub_active_bot_count") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def _build_live_group_name(room_id: str, other_bot_names: list[str], *, hub_active_bot_count: int) -> str:
    base_name = f"bilibili_live_{room_id}"
    if hub_active_bot_count <= 1:
        return base_name
    if not other_bot_names:
        return f"{base_name} [shared_ai_room]"
    visible_names = ", ".join(other_bot_names[:2])
    remaining = max(0, len(other_bot_names) - 2)
    if remaining > 0:
        visible_names = f"{visible_names} +{remaining}"
    return f"{base_name} [shared_ai_with: {visible_names}]"


def _normalize_epoch_seconds(value: Any) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return time.time()
    if not math.isfinite(timestamp) or timestamp <= 0:
        return time.time()
    while timestamp > 32_503_680_000:
        timestamp /= 1000.0
    return timestamp


def _detect_bot_mention(text: str, bot_names: list[str]) -> bool:
    """Check if the danmaku text mentions the bot by name or alias."""
    if not text or not bot_names:
        return False
    normalized = text.casefold()
    for name in bot_names:
        if not name:
            continue
        if name.casefold() in normalized:
            return True
    return False


def _collect_bot_names(settings: LiveAdapterSettings) -> list[str]:
    """Collect all bot names/aliases from settings for mention detection."""
    names: list[str] = []
    candidates = [
        *(settings.interaction.bot_names or []),
        str(settings.identity.bot_nickname or "").strip(),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(normalized)
    return names


def resolve_live_identity(*, user_id: str, username: str) -> tuple[str, str]:
    normalized_user_id = str(user_id or "").strip()
    normalized_username = str(username or normalized_user_id or "anonymous").strip()
    override = _LIVE_IDENTITY_OVERRIDES.get(normalized_user_id)
    if override:
        return (
            str(override.get("user_id") or normalized_user_id or "anonymous").strip(),
            str(override.get("username") or normalized_username).strip(),
        )
    return normalized_user_id or "anonymous", normalized_username


def extract_text_from_message(message: Mapping[str, Any]) -> str:
    """Extract displayable text from a MaiBot MessageDict-like mapping."""

    raw_message = message.get("raw_message", [])
    parts: list[str] = []
    if isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, Mapping):
                continue
            segment_type = str(segment.get("type") or "").strip()
            data = segment.get("data")
            if segment_type == "text":
                parts.append(str(data or ""))
            elif isinstance(data, Mapping) and "text" in data:
                parts.append(str(data.get("text") or ""))
            elif isinstance(data, str):
                parts.append(data)
    if not parts:
        for key in ("processed_plain_text", "display_message", "plain_text"):
            value = message.get(key)
            if value:
                parts.append(str(value))
                break
    return "".join(parts).strip()


def extract_live_output_text_from_message(message: Mapping[str, Any]) -> str:
    """Extract speech-safe text for subtitle/TTS/Live2D output."""

    raw_message = message.get("raw_message", [])
    parts: list[str] = []
    reply_targets: list[str] = []
    has_reply_segment = False
    if isinstance(raw_message, list):
        for segment in raw_message:
            if not isinstance(segment, Mapping):
                continue
            segment_type = str(segment.get("type") or "").strip()
            data = segment.get("data")
            if segment_type == "reply":
                has_reply_segment = True
                if isinstance(data, Mapping):
                    target_content = str(data.get("target_message_content") or "").strip()
                    if target_content:
                        reply_targets.append(target_content)
                continue
            if segment_type == "text":
                parts.append(str(data or ""))
            elif isinstance(data, Mapping) and "text" in data:
                parts.append(str(data.get("text") or ""))
            elif isinstance(data, str):
                parts.append(data)
    if parts:
        return _sanitize_live_output_text("".join(parts).strip())
    for key in ("processed_plain_text", "display_message", "plain_text"):
        value = message.get(key)
        if value:
            return _sanitize_live_output_text(
                str(value),
                reply_targets=reply_targets,
                has_reply_context=has_reply_segment,
            )
    return ""


def _sanitize_live_output_text(
    text: str,
    *,
    reply_targets: list[str] | None = None,
    has_reply_context: bool = False,
) -> str:
    normalized = _sanitize_legacy_bilibili_reply_tokens(text).strip()
    normalized = _strip_source_rendered_reply_wrappers(normalized)
    if has_reply_context:
        normalized = _strip_leading_reply_target(normalized, reply_targets or [])
    return normalized.strip()


def _sanitize_legacy_bilibili_reply_tokens(text: str) -> str:
    sanitized = str(text or "")
    while True:
        start = sanitized.find("[引用回复](bilibili-history-")
        if start < 0:
            break
        end = sanitized.find(")", start)
        if end < 0:
            break
        sanitized = f"{sanitized[:start]}[引用回复]{sanitized[end + 1:]}"
    while True:
        start = sanitized.find("(bilibili-history-")
        if start < 0:
            break
        end = sanitized.find(")", start)
        if end < 0:
            break
        sanitized = f"{sanitized[:start]}[引用回复]{sanitized[end + 1:]}"
    return sanitized


def _strip_source_rendered_reply_wrappers(text: str) -> str:
    normalized = text.strip()
    while normalized.startswith("["):
        prefix = _split_leading_bracket_token(normalized)
        if prefix is None:
            break
        token, remainder = prefix
        if not _is_source_rendered_reply_token(token):
            break
        normalized = remainder.lstrip()
    return normalized


def _split_leading_bracket_token(text: str) -> tuple[str, str] | None:
    if not text.startswith("["):
        return None
    end = text.find("]")
    if end <= 0:
        return None
    return text[1:end], text[end + 1 :]


def _is_source_rendered_reply_token(token: str) -> bool:
    normalized = token.strip()
    if normalized in {"引用回复", "回复了一条消息，但原消息已无法访问"}:
        return True
    if normalized.startswith("回复消息: "):
        return True
    return normalized.startswith("回复了") and "的消息: " in normalized


def _strip_leading_reply_target(text: str, reply_targets: list[str]) -> str:
    normalized = text.lstrip()
    for target in reply_targets:
        candidate = str(target or "").strip()
        if not candidate:
            continue
        if normalized == candidate:
            return ""
        if not normalized.startswith(candidate):
            continue
        remainder = normalized[len(candidate) :]
        if not remainder:
            return ""
        leading = remainder[0]
        if leading.isspace() or leading in "，。,.!！?？:：;；)]】>》」』":
            return remainder.lstrip(" \t\r\n，。,.!！?？:：;；)]】>》」』")
    return normalized
