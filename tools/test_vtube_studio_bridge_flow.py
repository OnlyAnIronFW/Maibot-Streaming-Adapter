"""Step-by-step smoke tests for the MaiBot -> VTS Live2D bridge."""

from __future__ import annotations

from typing import Any, Mapping

import argparse
import asyncio
import json
import time
import uuid

from aiohttp import ClientSession, WSMsgType


DEFAULT_BRIDGE_URL = "ws://127.0.0.1:18081/live2d"


async def request_capabilities(url: str) -> None:
    print("\n[1] Capabilities: requesting Live2D parameter profile from bridge...")
    async with ClientSession() as session:
        async with session.ws_connect(url) as ws:
            await ws.send_json({"type": "live2d.capabilities.request"})
            message = await ws.receive(timeout=10)
            if message.type != WSMsgType.TEXT:
                raise RuntimeError(f"unexpected websocket response: {message.type}")
            payload = json.loads(message.data)
            parameters = payload.get("parameters") if isinstance(payload, Mapping) else []
            groups = payload.get("groups") if isinstance(payload, Mapping) else {}
            print(f"    response.type = {payload.get('type')}")
            print(f"    model_name    = {payload.get('model_name') or '<unknown>'}")
            print(f"    parameters    = {len(parameters) if isinstance(parameters, list) else 0}")
            print(f"    groups        = {json.dumps(groups, ensure_ascii=False)}")
            print("    sample ids    =", _sample_parameter_ids(parameters))
    print("    OK: bridge responded. If parameter count is 0, check VTS model loading.")


async def basic_motion(url: str, repeats: int = 12) -> None:
    print("\n[2] Basic motion: head turn + mouth open, then reset...")
    print("    Watch VTube Studio: head should look sideways and mouth should open.")
    async with ClientSession() as session:
        async with session.ws_connect(url) as ws:
            for _ in range(repeats):
                await _send_parameters(
                    ws,
                    [
                        {"id": "ParamAngleX", "value": 18, "weight": 0.9},
                        {"id": "ParamAngleY", "value": -8, "weight": 0.8},
                        {"id": "ParamMouthOpenY", "value": 0.9, "weight": 1.0},
                        {"id": "ParamMouthSmile", "value": 0.7, "weight": 0.8},
                    ],
                    duration_ms=220,
                    timeline_id="manual-basic-motion",
                )
                await asyncio.sleep(0.12)
            for _ in range(max(6, repeats // 2)):
                await _send_parameters(
                    ws,
                    [
                        {"id": "ParamAngleX", "value": 0, "weight": 0.8},
                        {"id": "ParamAngleY", "value": 0, "weight": 0.8},
                        {"id": "ParamMouthOpenY", "value": 0, "weight": 1.0},
                        {"id": "ParamMouthSmile", "value": 0, "weight": 0.8},
                    ],
                    duration_ms=220,
                    timeline_id="manual-basic-motion",
                )
                await asyncio.sleep(0.12)
    print("    OK: basic motion frames sent.")


async def eye_and_expression(url: str, repeats: int = 10) -> None:
    print("\n[3] Eye/expression: blink-ish eye close + blush/smile pulse...")
    print("    Watch VTube Studio: eyes may blink/soften, smile should change if mapped.")
    async with ClientSession() as session:
        async with session.ws_connect(url) as ws:
            for index in range(repeats):
                eye_open = 0.08 if index % 4 in {1, 2} else 1.0
                await _send_parameters(
                    ws,
                    [
                        {"id": "ParamEyeLOpen", "value": eye_open, "weight": 0.85},
                        {"id": "ParamEyeROpen", "value": eye_open, "weight": 0.85},
                        {"id": "ParamMouthSmile", "value": 0.75, "weight": 0.85},
                        {"id": "ParamCheek", "value": 0.6, "weight": 0.65},
                    ],
                    duration_ms=180,
                    timeline_id="manual-eye-expression",
                )
                await asyncio.sleep(0.16)
            await _send_parameters(
                ws,
                [
                    {"id": "ParamEyeLOpen", "value": 1.0, "weight": 0.85},
                    {"id": "ParamEyeROpen", "value": 1.0, "weight": 0.85},
                    {"id": "ParamMouthSmile", "value": 0.0, "weight": 0.75},
                    {"id": "ParamCheek", "value": 0.0, "weight": 0.65},
                ],
                duration_ms=300,
                timeline_id="manual-eye-expression",
            )
    print("    OK: eye/expression frames sent.")


async def speech_timeline(url: str, *, scheduled: bool = False) -> None:
    print("\n[4] Speech timeline: prepare/start/frame/end with mouth frames...")
    print("    Watch VTube Studio: mouth should pulse over ~3 seconds.")
    if scheduled:
        print("    Mode: scheduled offsets. Frames are sent immediately and the bridge schedules them.")
    else:
        print("    Mode: realtime. Frames are sent every 80ms for easier visual testing.")
    timeline_id = uuid.uuid4().hex
    text = "你好，直播测试开始啦！"
    start = time.monotonic()
    async with ClientSession() as session:
        async with session.ws_connect(url) as ws:
            await ws.send_json(
                {
                    "type": "bot_reply.prepare",
                    "timeline_id": timeline_id,
                    "text": text,
                    "estimated_duration_ms": 2400,
                    "segments": ["你好，", "直播测试开始啦！"],
                    "prepare_ms": 180,
                }
            )
            await ws.send_json(
                {
                    "type": "bot_reply.start",
                    "timeline_id": timeline_id,
                    "text": text,
                    "estimated_duration_ms": 2400,
                }
            )
            if scheduled:
                await _send_scheduled_timeline_frames(ws, timeline_id)
            else:
                await _send_realtime_timeline_frames(ws, timeline_id, start)
            await ws.send_json(
                {
                    "type": "bot_reply.end",
                    "timeline_id": timeline_id,
                    "offset_ms": 2600,
                    "release_ms": 600,
                }
            )
            await asyncio.sleep(max(0.0, 3.3 - (time.monotonic() - start)))
    print("    OK: synchronized speech timeline sent.")


async def _send_scheduled_timeline_frames(ws: Any, timeline_id: str) -> None:
    for index in range(28):
        await ws.send_json(_timeline_frame_payload(timeline_id, index, offset_ms=180 + index * 80))


async def _send_realtime_timeline_frames(ws: Any, timeline_id: str, start: float) -> None:
    await asyncio.sleep(0.18)
    for index in range(28):
        elapsed_ms = int((time.monotonic() - start) * 1000)
        await ws.send_json(_timeline_frame_payload(timeline_id, index, offset_ms=elapsed_ms))
        await asyncio.sleep(0.08)


def _timeline_frame_payload(timeline_id: str, index: int, *, offset_ms: int) -> dict[str, Any]:
    mouth_open = 0.18 + (index % 4) * 0.18
    return {
        "type": "live2d.timeline.frame",
        "timeline_id": timeline_id,
        "offset_ms": offset_ms,
        "parameters": [
            {"id": "ParamMouthOpenY", "value": mouth_open, "weight": 1.0},
            {"id": "ParamMouthSmile", "value": 0.45, "weight": 0.85},
            {"id": "ParamAngleX", "value": 12 if index < 14 else -10, "weight": 0.7},
            {"id": "ParamAngleY", "value": -5 if index < 10 else 4, "weight": 0.55},
        ],
    }


async def accessory_toggle(url: str, name: str) -> None:
    print(f"\n[5] Accessory: toggling parameterized accessory '{name}'...")
    print("    This only moves the model if the VTS custom input is mapped to an output Live2D parameter.")
    parameter_id = f"ParamAccessory{_pascal_case(name)}"
    async with ClientSession() as session:
        async with session.ws_connect(url) as ws:
            for value in (1.0, 0.0):
                for _ in range(5):
                    await _send_parameters(
                        ws,
                        [{"id": parameter_id, "value": value, "weight": 1.0}],
                        duration_ms=240,
                        timeline_id=f"manual-accessory-{name}",
                    )
                    await asyncio.sleep(0.12)
    print(f"    OK: sent {parameter_id}=1 then 0.")


async def run_selected_steps(args: argparse.Namespace) -> None:
    steps = _resolve_steps(args.step)
    if "capabilities" in steps:
        await request_capabilities(args.url)
        _pause_if_needed(args)
    if "basic" in steps:
        await basic_motion(args.url, repeats=args.repeats)
        _pause_if_needed(args)
    if "expression" in steps:
        await eye_and_expression(args.url, repeats=args.repeats)
        _pause_if_needed(args)
    if "timeline" in steps:
        await speech_timeline(args.url, scheduled=args.scheduled_timeline)
        _pause_if_needed(args)
    if "accessory" in steps:
        await accessory_toggle(args.url, args.accessory)


async def _send_parameters(ws: Any, parameters: list[dict[str, float | str]], *, duration_ms: int, timeline_id: str) -> None:
    await ws.send_json(
        {
            "type": "live2d.parameters",
            "timeline_id": timeline_id,
            "parameters": parameters,
            "duration_ms": duration_ms,
            "easing": "easeOutQuad",
            "blend": "replace",
            "priority": 5,
        }
    )


def _resolve_steps(step: str) -> list[str]:
    aliases = {
        "1": ["capabilities"],
        "capabilities": ["capabilities"],
        "2": ["basic"],
        "basic": ["basic"],
        "basic-motion": ["basic"],
        "3": ["expression"],
        "expression": ["expression"],
        "eyes": ["expression"],
        "4": ["timeline"],
        "timeline": ["timeline"],
        "speech": ["timeline"],
        "5": ["accessory"],
        "accessory": ["accessory"],
        "all": ["capabilities", "basic", "expression", "timeline"],
        "full": ["capabilities", "basic", "expression", "timeline", "accessory"],
    }
    normalized = str(step or "all").strip().lower()
    if normalized not in aliases:
        raise ValueError(f"unknown step: {step}")
    return aliases[normalized]


def _sample_parameter_ids(parameters: Any) -> str:
    if not isinstance(parameters, list):
        return "<none>"
    ids = []
    for item in parameters:
        if isinstance(item, Mapping):
            parameter_id = str(item.get("id") or item.get("name") or "").strip()
            if parameter_id:
                ids.append(parameter_id)
    return ", ".join(ids[:12]) if ids else "<none>"


def _pause_if_needed(args: argparse.Namespace) -> None:
    if args.interactive:
        input("\nPress Enter for next step...")


def _pascal_case(value: str) -> str:
    parts = [part for part in "".join(char if char.isalnum() else " " for char in value).split() if part]
    return "".join(part[:1].upper() + part[1:] for part in parts) or "Glasses"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run step-by-step tests against the MaiBot Live2D VTS bridge.")
    parser.add_argument("--url", default=DEFAULT_BRIDGE_URL, help="MaiBot Live2D bridge websocket URL.")
    parser.add_argument(
        "--step",
        default="all",
        help="1/capabilities, 2/basic, 3/expression, 4/timeline, 5/accessory, all, or full.",
    )
    parser.add_argument("--repeats", type=int, default=12, help="Number of repeated frames for motion tests.")
    parser.add_argument("--accessory", default="glasses", help="Accessory name for --step accessory/full.")
    parser.add_argument("--interactive", action="store_true", help="Pause after each step.")
    parser.add_argument(
        "--scheduled-timeline",
        action="store_true",
        help="Send timeline frames all at once and let the bridge schedule offsets.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print("MaiBot Live2D bridge test")
    print(f"bridge URL: {args.url}")
    asyncio.run(run_selected_steps(args))
    print("\nDone.")


if __name__ == "__main__":
    main()
