"""Five-vowel mouth calibration helper for the MaiBot -> VTS bridge.

Run this while VTube Studio and ``vtube_studio_bridge.py`` are running.
It sends direct parameter frames for A/E/I/O/U so the model can be checked
against the same kind of vowel poses VTS lip-sync setup uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import argparse
import asyncio
import json
import time
import uuid

from aiohttp import ClientSession, WSMsgType


DEFAULT_BRIDGE_URL = "ws://127.0.0.1:18081/live2d"


@dataclass(frozen=True)
class VowelPose:
    key: str
    open_value: float
    form_value: float
    smile_value: float = 0.0


VOWEL_POSES: tuple[VowelPose, ...] = (
    VowelPose("A", 1.00, 1.00, 0.00),
    VowelPose("E", 0.60, 0.60, 0.00),
    VowelPose("I", 0.20, 0.50, 0.00),
    VowelPose("O", 1.00, 0.00, 0.00),
    VowelPose("U", 0.30, 0.20, 0.00),
)


async def request_capabilities(url: str) -> dict[str, Any]:
    async with ClientSession() as session:
        async with session.ws_connect(url) as ws:
            await ws.send_json({"type": "live2d.capabilities.request"})
            message = await ws.receive(timeout=10)
            if message.type != WSMsgType.TEXT:
                raise RuntimeError(f"unexpected websocket response: {message.type}")
            payload = json.loads(message.data)
            return payload if isinstance(payload, dict) else {}


async def run_visual_calibration(args: argparse.Namespace) -> None:
    print("[vowels] Direct VTS-style parameter calibration")
    print(f"bridge URL: {args.url}")
    capabilities = await request_capabilities(args.url)
    parameter_ids = _parameter_ids(capabilities.get("parameters"))
    print(f"model: {capabilities.get('model_name') or '<unknown>'}")
    print(f"has ParamMouthOpenY: {'yes' if 'ParamMouthOpenY' in parameter_ids else 'no/fallback'}")
    print("Watch the model: A should be widest open, I should be widest horizontal, O/U should be round.")

    async with ClientSession() as session:
        async with session.ws_connect(args.url) as ws:
            await _hold_pose(ws, "close", _close_parameters(), hold_ms=max(300, args.close_ms))
            for pose in VOWEL_POSES:
                scaled_pose = _scale_pose(pose, args.open_scale, args.form_scale)
                print(
                    f"  {scaled_pose.key}: open={scaled_pose.open_value:.2f} "
                    f"form={scaled_pose.form_value:.2f} smile={scaled_pose.smile_value:.2f}"
                )
                await _hold_pose(
                    ws,
                    f"vowel-{scaled_pose.key.lower()}",
                    _pose_parameters(scaled_pose),
                    hold_ms=args.hold_ms,
                    interval_ms=args.interval_ms,
                )
                await _hold_pose(ws, "close", _close_parameters(), hold_ms=args.close_ms)
    print("Done. If A still barely opens, raise mouth_open_gain/open_max or check VTS MouthOpen mapping.")


async def run_timeline_calibration(args: argparse.Namespace) -> None:
    print("[timeline] Scheduled A/E/I/O/U timeline calibration")
    timeline_id = uuid.uuid4().hex
    duration_ms = len(VOWEL_POSES) * args.hold_ms
    async with ClientSession() as session:
        async with session.ws_connect(args.url) as ws:
            await ws.send_json(
                {
                    "type": "bot_reply.prepare",
                    "timeline_id": timeline_id,
                    "text": "a e i o u",
                    "estimated_duration_ms": duration_ms,
                    "segments": ["a", "e", "i", "o", "u"],
                    "prepare_ms": 180,
                }
            )
            await ws.send_json(
                {
                    "type": "bot_reply.start",
                    "timeline_id": timeline_id,
                    "text": "a e i o u",
                    "estimated_duration_ms": duration_ms,
                }
            )
            offset_ms = 180
            for pose in VOWEL_POSES:
                scaled_pose = _scale_pose(pose, args.open_scale, args.form_scale)
                for step in range(max(1, args.hold_ms // args.interval_ms)):
                    await ws.send_json(
                        {
                            "type": "live2d.timeline.frame",
                            "timeline_id": timeline_id,
                            "offset_ms": offset_ms + step * args.interval_ms,
                            "parameters": _pose_parameters(scaled_pose),
                        }
                    )
                offset_ms += args.hold_ms
            await ws.send_json(
                {
                    "type": "live2d.timeline.frame",
                    "timeline_id": timeline_id,
                    "offset_ms": offset_ms,
                    "parameters": _close_parameters(),
                }
            )
            await ws.send_json(
                {
                    "type": "bot_reply.end",
                    "timeline_id": timeline_id,
                    "offset_ms": offset_ms + args.close_ms,
                    "release_ms": 450,
                }
            )
            await asyncio.sleep((offset_ms + args.close_ms + 500) / 1000.0)
    print("Done.")


def print_recommended_config() -> None:
    print("[recommended live2d.sync]")
    print("mouth_update_interval_ms = 70")
    print("mouth_open_threshold = 0.035")
    print("mouth_open_gamma = 0.75")
    print("mouth_open_gain = 2.15")
    print("mouth_open_max = 1.0")
    print('mouth_sync_mode = "hybrid"')
    print("mouth_amplitude_mix = 0.45")
    print("mouth_viseme_lead_ms = 0")
    print("mouth_open_smoothing = 0.58")
    print("mouth_open_attack_smoothing = 0.16")
    print("mouth_open_release_smoothing = 0.62")
    print("mouth_open_min_delta = 0.03")
    for pose in VOWEL_POSES:
        key = pose.key.lower()
        print(f"mouth_vowel_{key}_open = {pose.open_value:.2f}")
        print(f"mouth_vowel_{key}_form = {pose.form_value:.2f}")


async def _hold_pose(
    ws: Any,
    timeline_id: str,
    parameters: list[dict[str, float | str]],
    *,
    hold_ms: int,
    interval_ms: int = 80,
) -> None:
    start = time.monotonic()
    repeats = max(1, hold_ms // max(20, interval_ms))
    for _ in range(repeats):
        await ws.send_json(
            {
                "type": "live2d.parameters",
                "timeline_id": timeline_id,
                "parameters": parameters,
                "duration_ms": max(80, interval_ms * 2),
                "easing": "easeOutQuad",
                "blend": "replace",
                "priority": 8,
            }
        )
        await asyncio.sleep(max(20, interval_ms) / 1000.0)
    remaining = hold_ms / 1000.0 - (time.monotonic() - start)
    if remaining > 0:
        await asyncio.sleep(remaining)


def _pose_parameters(pose: VowelPose) -> list[dict[str, float | str]]:
    return [
        {"id": "ParamMouthOpenY", "value": pose.open_value, "weight": 1.0},
        {"id": "ParamMouthForm", "value": pose.form_value, "weight": 1.0},
        {"id": "ParamMouthSmile", "value": pose.smile_value, "weight": 0.65},
    ]


def _close_parameters() -> list[dict[str, float | str]]:
    return [
        {"id": "ParamMouthOpenY", "value": 0.0, "weight": 1.0},
        {"id": "ParamMouthForm", "value": 0.0, "weight": 0.9},
        {"id": "ParamMouthSmile", "value": 0.0, "weight": 0.6},
    ]


def _scale_pose(pose: VowelPose, open_scale: float, form_scale: float) -> VowelPose:
    return VowelPose(
        key=pose.key,
        open_value=min(1.0, max(0.0, pose.open_value * max(0.0, open_scale))),
        form_value=min(1.0, max(-2.0, pose.form_value * max(0.0, form_scale))),
        smile_value=min(1.0, max(0.0, pose.smile_value * max(0.0, form_scale))),
    )


def _parameter_ids(parameters: Any) -> set[str]:
    if not isinstance(parameters, list):
        return set()
    return {
        str(parameter.get("id") or parameter.get("name") or "").strip()
        for parameter in parameters
        if isinstance(parameter, Mapping)
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calibrate Live2D mouth shapes through the MaiBot VTS bridge.")
    parser.add_argument("--url", default=DEFAULT_BRIDGE_URL, help="MaiBot Live2D bridge websocket URL.")
    parser.add_argument(
        "--mode",
        default="visual",
        choices=["visual", "timeline", "recommend"],
        help="visual sends held vowel poses, timeline schedules frames, recommend prints config.",
    )
    parser.add_argument("--hold-ms", type=int, default=1100, help="How long each vowel is held.")
    parser.add_argument("--close-ms", type=int, default=320, help="Close-mouth gap between vowels.")
    parser.add_argument("--interval-ms", type=int, default=70, help="Frame resend interval.")
    parser.add_argument("--open-scale", type=float, default=1.0, help="Scale all vowel mouth.open values.")
    parser.add_argument("--form-scale", type=float, default=1.0, help="Scale all vowel mouth.form values.")
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    if args.mode == "recommend":
        print_recommended_config()
        return
    if args.mode == "timeline":
        await run_timeline_calibration(args)
        return
    await run_visual_calibration(args)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
