"""Stable audio-separator bridge for RVC song vocal separation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import argparse
import logging


MDX23C_INSTVOC_HQ_MODEL = "MDX23C-8KFFT-InstVoc_HQ.ckpt"
CUSTOM_OUTPUT_NAMES = {"vocals": "vocals", "instrumental": "no_vocals"}
MDXC_PARAMS = {
    "segment_size": 256,
    "override_model_segment_size": False,
    "batch_size": 1,
    "overlap": 8,
    "pitch_shift": 0,
}


@dataclass(frozen=True)
class AudioSeparatorBridgeOptions:
    """Options for the audio-separator bridge."""

    input_path: Path
    output_dir: Path
    model_file_dir: Path
    model_filename: str = MDX23C_INSTVOC_HQ_MODEL
    output_format: str = "WAV"
    sample_rate: int = 44100
    use_autocast: bool = True


def run_audio_separator_bridge(
    options: AudioSeparatorBridgeOptions,
    *,
    separator_factory: Callable[..., Any] | None = None,
) -> tuple[Path, Path]:
    """Separate vocals and instrumental audio into deterministic paths."""

    input_path = Path(options.input_path).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"input audio not found: {input_path}")

    output_format = _normalize_output_format(options.output_format)
    stem_dir = Path(options.output_dir).expanduser() / (input_path.stem or "source")
    stem_dir.mkdir(parents=True, exist_ok=True)
    model_file_dir = Path(options.model_file_dir).expanduser()
    model_file_dir.mkdir(parents=True, exist_ok=True)

    if separator_factory is None:
        from audio_separator.separator import Separator

        separator_factory = Separator

    separator = separator_factory(
        log_level=logging.INFO,
        model_file_dir=str(model_file_dir),
        output_dir=str(stem_dir),
        output_format=output_format,
        sample_rate=int(options.sample_rate),
        use_autocast=_resolve_use_autocast(options.use_autocast),
        mdxc_params=dict(MDXC_PARAMS),
    )
    separator.load_model(str(options.model_filename))
    output_files = separator.separate(str(input_path), custom_output_names=dict(CUSTOM_OUTPUT_NAMES))

    vocals_path = stem_dir / f"vocals.{output_format.lower()}"
    instrumental_path = stem_dir / f"no_vocals.{output_format.lower()}"
    _ensure_expected_outputs(vocals_path, instrumental_path, output_files)
    return vocals_path, instrumental_path


def build_arg_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(description="Run MDX23C HQ separation with stable output names.")
    parser.add_argument("--input", dest="input_path", type=Path, required=True)
    parser.add_argument("--output-dir", dest="output_dir", type=Path, required=True)
    parser.add_argument("--model-file-dir", dest="model_file_dir", type=Path, required=True)
    parser.add_argument("--model-filename", default=MDX23C_INSTVOC_HQ_MODEL)
    parser.add_argument("--output-format", default="WAV")
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--no-autocast", dest="use_autocast", action="store_false", default=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the bridge from the command line."""

    args = build_arg_parser().parse_args(argv)
    vocals_path, instrumental_path = run_audio_separator_bridge(
        AudioSeparatorBridgeOptions(
            input_path=args.input_path,
            output_dir=args.output_dir,
            model_file_dir=args.model_file_dir,
            model_filename=args.model_filename,
            output_format=args.output_format,
            sample_rate=args.sample_rate,
            use_autocast=args.use_autocast,
        )
    )
    print(f"vocals={vocals_path}")
    print(f"instrumental={instrumental_path}")
    return 0


def _normalize_output_format(value: str) -> str:
    normalized = str(value or "WAV").strip().lstrip(".").upper()
    return normalized or "WAV"


def _resolve_use_autocast(requested: bool) -> bool:
    return bool(requested) and _torch_autocast_probe_available()


def _torch_autocast_probe_available() -> bool:
    try:
        from torch.amp import autocast_mode
    except Exception:
        return False
    return hasattr(autocast_mode, "is_autocast_available")


def _ensure_expected_outputs(vocals_path: Path, instrumental_path: Path, output_files: Any) -> None:
    missing = [path for path in (vocals_path, instrumental_path) if not path.exists()]
    if not missing:
        return
    returned = ", ".join(str(item) for item in (output_files or [])) or "<none>"
    missing_text = ", ".join(str(path) for path in missing)
    raise FileNotFoundError(f"audio-separator did not create expected output files: {missing_text}; returned: {returned}")


if __name__ == "__main__":
    raise SystemExit(main())
