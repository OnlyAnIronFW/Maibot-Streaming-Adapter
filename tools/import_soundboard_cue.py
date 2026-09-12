"""Import one soundboard cue with optional audio, optional media, and a usage hint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import argparse
import contextlib
import shutil
import subprocess
import sys

import tomlkit


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOUND_DATA_DIR = PLUGIN_ROOT / "data" / "soundboard"
DEFAULT_CUE_METADATA_NAME = "cue.toml"


@dataclass(frozen=True)
class CueImportRequest:
    cue_id: str
    label: str
    audio_source: Path | None
    media_source: Path | None
    usage_hint: str
    keywords: list[str]
    match_mode: str
    priority: int
    duration_ms: int
    cooldown_sec: float
    volume: float
    enabled: bool
    play_audio: bool
    show_effect: bool


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_audio_source = Path(args.audio).expanduser().resolve() if args.audio else None
    raw_media_source = Path(args.media).expanduser().resolve() if args.media else None
    audio_source, media_source = normalize_sources(raw_audio_source, raw_media_source)
    request = CueImportRequest(
        cue_id=normalize_cue_id(args.cue_id),
        label=str(args.label or args.cue_id).strip(),
        audio_source=audio_source,
        media_source=media_source,
        usage_hint=str(args.usage_hint or "").strip(),
        keywords=collect_keywords(args.keyword, args.keywords),
        match_mode=normalize_match_mode(args.match_mode),
        priority=int(args.priority),
        duration_ms=max(120, int(args.duration_ms)),
        cooldown_sec=max(0.0, float(args.cooldown_sec)),
        volume=min(2.0, max(0.0, float(args.volume))),
        enabled=bool(args.enabled),
        play_audio=bool(args.play_audio),
        show_effect=bool(args.show_effect),
    )
    result = import_soundboard_cue(
        soundboard_dir=Path(args.soundboard_dir).expanduser().resolve(),
        request=request,
    )
    print(f"Imported cue '{result['cue_id']}' into {result['cue_dir']}")
    print(f"Cue config updated: {result['cue_config_path']}")
    print(f"Audio: {result['audio_path'] or '(embedded in video or none)'}")
    if result["media_path"]:
        print(f"Media: {result['media_path']}")
    print(f"Keywords: {', '.join(result['keywords']) or '(none)'}")
    print(f"Usage hint: {result['usage_hint']}")
    print(f"Volume: {result['volume']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cue-id", required=True, help="Stable cue id, for example laugh or surprise.")
    parser.add_argument("--label", default="", help="Human-readable label shown in logs and the browser overlay.")
    parser.add_argument(
        "--audio",
        default="",
        help="Optional path to the cue audio file. If omitted, a video media file can provide browser-side audio.",
    )
    parser.add_argument("--media", default="", help="Optional GIF, image, or video file shown on the green-screen overlay.")
    parser.add_argument(
        "--usage-hint",
        required=True,
        help="Short prompt telling MaiBot when this cue should be used.",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="Add one trigger keyword. Repeat this flag to add multiple keywords.",
    )
    parser.add_argument(
        "--keywords",
        default="",
        help="Optional comma-separated trigger keywords.",
    )
    parser.add_argument("--match-mode", default="contains", help="Keyword match mode: contains, exact, or regex.")
    parser.add_argument("--priority", type=int, default=0, help="Higher priority wins if multiple cue keywords match.")
    parser.add_argument("--duration-ms", type=int, default=1600, help="Overlay display duration in milliseconds.")
    parser.add_argument("--cooldown-sec", type=float, default=4.0, help="Per-cue cooldown in seconds.")
    parser.add_argument("--volume", type=float, default=1.0, help="Per-cue volume multiplier.")
    parser.add_argument(
        "--soundboard-dir",
        default=str(DEFAULT_SOUND_DATA_DIR),
        help="Base directory for copied soundboard assets.",
    )
    parser.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--play-audio", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--show-effect", action=argparse.BooleanOptionalAction, default=True)
    return parser


def import_soundboard_cue(
    *,
    soundboard_dir: Path,
    request: CueImportRequest,
) -> dict[str, Any]:
    if not request.cue_id:
        raise ValueError("cue_id is empty after normalization")
    if not request.label:
        raise ValueError("label is required")
    if not request.usage_hint:
        raise ValueError("usage_hint is required")
    if request.audio_source is None and request.media_source is None:
        raise ValueError("at least one of audio_source or media_source is required")
    if request.audio_source is not None and not request.audio_source.exists():
        raise FileNotFoundError(f"audio file not found: {request.audio_source}")
    if request.media_source is not None and not request.media_source.exists():
        raise FileNotFoundError(f"media file not found: {request.media_source}")

    cue_dir = soundboard_dir / "cues" / request.cue_id
    cue_dir.mkdir(parents=True, exist_ok=True)

    copied_audio_path = None
    if request.audio_source is not None:
        copied_audio_path = copy_asset(request.audio_source, cue_dir / f"audio{request.audio_source.suffix.lower()}")
    copied_media_path = None
    if request.media_source is not None:
        copied_media_path = copy_asset(request.media_source, cue_dir / f"media{request.media_source.suffix.lower()}")
    if copied_audio_path is None and copied_media_path is not None and classify_asset_kind(copied_media_path) == "video":
        copied_audio_path = extract_video_audio(copied_media_path, cue_dir / "audio.wav")
    cue_config_path = cue_dir / DEFAULT_CUE_METADATA_NAME
    cue_document = build_cue_document(
        request,
        cue_dir=cue_dir,
        audio_path=copied_audio_path,
        media_path=copied_media_path,
    )
    cue_config_path.write_text(tomlkit.dumps(cue_document), encoding="utf-8", newline="\n")
    return {
        "cue_id": request.cue_id,
        "cue_dir": str(cue_dir),
        "cue_config_path": str(cue_config_path),
        "audio_path": str(copied_audio_path) if copied_audio_path is not None else "",
        "media_path": str(copied_media_path) if copied_media_path is not None else "",
        "keywords": list(request.keywords),
        "usage_hint": request.usage_hint,
        "volume": request.volume,
    }


def build_cue_document(
    request: CueImportRequest,
    *,
    cue_dir: Path,
    audio_path: Path | None,
    media_path: Path | None,
) -> Any:
    if (cue_dir / DEFAULT_CUE_METADATA_NAME).exists():
        with (cue_dir / DEFAULT_CUE_METADATA_NAME).open("r", encoding="utf-8") as file_handle:
            document = tomlkit.parse(file_handle.read())
    else:
        document = tomlkit.document()
    document["id"] = request.cue_id
    document["label"] = request.label
    document["keywords"] = request.keywords
    document["audio_path"] = cue_local_asset_path(cue_dir, audio_path)
    document["media_path"] = cue_local_asset_path(cue_dir, media_path) if media_path is not None else ""
    document["usage_hint"] = request.usage_hint
    document["match_mode"] = request.match_mode
    document["priority"] = request.priority
    document["duration_ms"] = request.duration_ms
    document["cooldown_sec"] = request.cooldown_sec
    document["volume"] = request.volume
    document["enabled"] = request.enabled
    document["play_audio"] = request.play_audio
    document["show_effect"] = request.show_effect
    return document


def copy_asset(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination.resolve()


def extract_video_audio(media_path: Path, destination: Path) -> Path | None:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path,
        "-y",
        "-i",
        str(media_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(destination),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 or not destination.exists():
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        return None
    return destination.resolve()


def cue_local_asset_path(cue_dir: Path, asset_path: Path | None) -> str:
    if asset_path is None:
        return ""
    return asset_path.resolve().relative_to(cue_dir.resolve()).as_posix()


def normalize_sources(
    audio_source: Path | None,
    media_source: Path | None,
) -> tuple[Path | None, Path | None]:
    if audio_source is None:
        return None, media_source
    if media_source is not None:
        return audio_source, media_source
    asset_kind = classify_asset_kind(audio_source)
    if asset_kind in {"video", "image"}:
        return None, audio_source
    return audio_source, media_source


def classify_asset_kind(path: Path | None) -> str:
    suffix = str(path.suffix if path is not None else "").lower()
    if suffix in {".mp4", ".webm", ".mov", ".m4v"}:
        return "video"
    if suffix in {".gif", ".png", ".jpg", ".jpeg", ".webp", ".avif"}:
        return "image"
    if suffix in {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}:
        return "audio"
    return ""


def collect_keywords(items: list[str], csv_text: str) -> list[str]:
    raw_keywords: list[str] = []
    for item in items:
        raw_keywords.extend(part.strip() for part in str(item or "").split(","))
    if csv_text:
        raw_keywords.extend(part.strip() for part in str(csv_text).split(","))
    result: list[str] = []
    seen: set[str] = set()
    for keyword in raw_keywords:
        if not keyword:
            continue
        if keyword in seen:
            continue
        seen.add(keyword)
        result.append(keyword)
    return result


def normalize_cue_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    normalized = "".join(char if char.isalnum() or char in {"_", "-", "."} else "_" for char in text)
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("._-")


def normalize_match_mode(value: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"contains", "exact", "regex"} else "contains"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
