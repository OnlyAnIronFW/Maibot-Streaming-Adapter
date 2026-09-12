"""livehub 命令行入口：python -m livehub [--config path] [--host ...] [--port ...] [--room-id ...]

示例：
    python -m livehub --room-id 22637261
    python -m livehub --config livehub/config.toml --port 18190
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys

from . import __version__
from .config import CONFIG_FIELDS, LiveHubConfig, load_config
from .server import LiveHubServer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="livehub", description="MaiBot Bilibili live adapter 共享直播间枢纽")
    parser.add_argument("--config", default="", help="配置文件路径（默认 livehub/config.toml）")
    parser.add_argument("--host", default=None, help="监听地址（覆盖配置）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置）")
    parser.add_argument("--room-id", type=int, default=None, help="B 站直播间 ID（覆盖配置）")
    parser.add_argument("--log-level", default=None, help="日志级别（INFO/DEBUG/WARNING/ERROR，覆盖配置）")
    parser.add_argument("--version", action="version", version=f"livehub {__version__}")
    return parser.parse_args()


def _apply_overrides(config: LiveHubConfig, args: argparse.Namespace) -> LiveHubConfig:
    """将命令行参数覆盖到配置对象；未指定的参数（None）不参与覆盖。"""
    overrides = {
        key: value
        for key, value in {
            "host": args.host,
            "port": args.port,
            "room_id": args.room_id,
            "log_level": args.log_level,
        }.items()
        if value is not None
    }
    if not overrides:
        return config
    base = {key: getattr(config, key) for key in CONFIG_FIELDS if key not in overrides}
    return LiveHubConfig(**base, **overrides)


def _setup_logging(level_name: str) -> logging.Logger:
    level = getattr(logging, str(level_name or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("livehub")


async def _run_server(config: LiveHubConfig, logger: logging.Logger) -> None:
    server = LiveHubServer(config, logger=logger)
    await server.start()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        stop_event.set()

    # Windows 上 add_signal_handler 不可用时静默跳过（Ctrl+C 由 KeyboardInterrupt 处理）
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            asyncio.get_running_loop().add_signal_handler(sig, _request_stop)
    try:
        await stop_event.wait()
    finally:
        logger.info("livehub stopping ...")
        await server.stop()


def main() -> None:
    """入口函数。"""
    args = _parse_args()
    config = _apply_overrides(load_config(args.config), args)
    logger = _setup_logging(config.log_level)
    try:
        asyncio.run(_run_server(config, logger))
    except KeyboardInterrupt:
        logger.info("livehub interrupted")
    except Exception as exc:
        logger.error(f"livehub fatal error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
