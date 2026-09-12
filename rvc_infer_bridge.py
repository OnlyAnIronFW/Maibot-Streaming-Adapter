"""Stable Python bridge for RVC inference."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import argparse
import contextlib
import os


@dataclass(frozen=True)
class RvcInferBridgeOptions:
    """Options for invoking RVC inference."""

    input_path: Path
    output_path: Path
    model_path: Path
    index_path: Path
    working_dir: Path | None = None
    pitch: int = 0
    filter_radius: int = 7
    index_rate: float = 0.35
    volume_envelope: int = 1
    protect: float = 0.5
    hop_length: int = 128
    f0_method: str = "rmvpe"
    split_audio: bool = True
    f0_autotune: bool = False
    f0_autotune_strength: float = 0.0
    clean_audio: bool = False
    clean_strength: float = 0.7
    export_format: str = "WAV"
    f0_file: str = ""
    embedder_model: str = "contentvec"
    embedder_model_custom: str | None = None


def run_rvc_infer_bridge(
    options: RvcInferBridgeOptions,
    *,
    run_infer: Callable[..., tuple[str, str]] | None = None,
) -> Path:
    """Run RVC inference through the Python API and require the output file."""

    input_path = Path(options.input_path).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"RVC input wav not found: {input_path}")
    model_path = Path(options.model_path).expanduser()
    if not model_path.exists():
        raise FileNotFoundError(f"RVC model not found: {model_path}")
    index_path = Path(options.index_path).expanduser()
    if not index_path.exists():
        raise FileNotFoundError(f"RVC index not found: {index_path}")

    output_path = Path(options.output_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    working_dir = Path(options.working_dir).expanduser() if options.working_dir is not None else _default_working_dir()
    if not working_dir.exists():
        raise FileNotFoundError(f"RVC working directory not found: {working_dir}")

    if run_infer is None:
        from rvc_cli.core import run_infer_script

        run_infer = run_infer_script

    with _pushd(working_dir):
        run_infer(
            pitch=int(options.pitch),
            filter_radius=int(options.filter_radius),
            index_rate=float(options.index_rate),
            volume_envelope=int(options.volume_envelope),
            protect=float(options.protect),
            hop_length=int(options.hop_length),
            f0_method=str(options.f0_method),
            input_path=str(input_path),
            output_path=str(output_path),
            pth_path=str(model_path),
            index_path=str(index_path),
            split_audio=bool(options.split_audio),
            f0_autotune=bool(options.f0_autotune),
            f0_autotune_strength=float(options.f0_autotune_strength),
            clean_audio=bool(options.clean_audio),
            clean_strength=float(options.clean_strength),
            export_format=str(options.export_format),
            f0_file=str(options.f0_file),
            embedder_model=str(options.embedder_model),
            embedder_model_custom=options.embedder_model_custom,
        )
    if not output_path.exists():
        raise FileNotFoundError(f"RVC output wav not found: {output_path}")
    return output_path


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the bridge CLI parser."""

    parser = argparse.ArgumentParser(description="Run RVC inference through the Python bridge.")
    parser.add_argument("--input", dest="input_path", type=Path, required=True)
    parser.add_argument("--output", dest="output_path", type=Path, required=True)
    parser.add_argument("--model-path", dest="model_path", type=Path, required=True)
    parser.add_argument("--index-path", dest="index_path", type=Path, required=True)
    parser.add_argument("--pitch", type=int, default=0)
    parser.add_argument("--filter-radius", type=int, default=7)
    parser.add_argument("--index-rate", type=float, default=0.35)
    parser.add_argument("--volume-envelope", type=int, default=1)
    parser.add_argument("--protect", type=float, default=0.5)
    parser.add_argument("--hop-length", type=int, default=128)
    parser.add_argument("--f0-method", default="rmvpe")
    parser.add_argument("--split-audio", dest="split_audio", action="store_true", default=True)
    parser.add_argument("--no-split-audio", dest="split_audio", action="store_false")
    parser.add_argument("--f0-autotune", dest="f0_autotune", action="store_true", default=False)
    parser.add_argument("--f0-autotune-strength", type=float, default=0.0)
    parser.add_argument("--clean-audio", dest="clean_audio", action="store_true", default=False)
    parser.add_argument("--clean-strength", type=float, default=0.7)
    parser.add_argument("--export-format", default="WAV")
    parser.add_argument("--f0-file", default="")
    parser.add_argument("--embedder-model", default="contentvec")
    parser.add_argument("--embedder-model-custom", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the bridge from the CLI."""

    args = build_arg_parser().parse_args(argv)
    output_path = run_rvc_infer_bridge(
        RvcInferBridgeOptions(
            input_path=args.input_path,
            output_path=args.output_path,
            model_path=args.model_path,
            index_path=args.index_path,
            working_dir=None,
            pitch=args.pitch,
            filter_radius=args.filter_radius,
            index_rate=args.index_rate,
            volume_envelope=args.volume_envelope,
            protect=args.protect,
            hop_length=args.hop_length,
            f0_method=args.f0_method,
            split_audio=args.split_audio,
            f0_autotune=args.f0_autotune,
            f0_autotune_strength=args.f0_autotune_strength,
            clean_audio=args.clean_audio,
            clean_strength=args.clean_strength,
            export_format=args.export_format,
            f0_file=args.f0_file,
            embedder_model=args.embedder_model,
            embedder_model_custom=args.embedder_model_custom,
        )
    )
    print(f"output={output_path}")
    return 0


def _default_working_dir() -> Path:
    return Path(__file__).resolve().parent / "data" / "rvc_runtime_py310" / "Lib" / "site-packages"


@contextlib.contextmanager
def _pushd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    raise SystemExit(main())
