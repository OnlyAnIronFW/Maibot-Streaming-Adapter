from __future__ import annotations

import contextlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from aiohttp import WSMsgType, web

from .live2d_shell_window import SoulLinkShellWindowHost, SoulLinkShellWindowOptions


class SoulLinkShellRuntime:
    """Hosts the local SoulLink shell server and optional native window."""

    def __init__(self, *, config: Any, logger: Any = None) -> None:
        self.config = config
        self.logger = logger
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._subscribers: set[web.WebSocketResponse] = set()
        self._window_host: SoulLinkShellWindowHost | None = None
        self._routes_ready = False
        self.shell_url = ""
        self._interactive = bool(getattr(config, "start_interactive", False))
        self._click_through = bool(getattr(config, "click_through_default", True))
        self._model_root: Path | None = None
        self._sticky_payloads: dict[str, dict[str, Any]] = {}
        if self._interactive:
            self._click_through = False

    async def start(self, *, start_window: bool = True) -> None:
        if self._runner is not None:
            return
        self._ensure_routes()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, str(self.config.host), int(self.config.port))
        await self._site.start()
        self.shell_url = f"http://{self._client_host()}:{int(self.config.port)}/shell"
        if start_window:
            self._start_window_host()

    async def stop(self) -> None:
        for ws in tuple(self._subscribers):
            try:
                await ws.close()
            except Exception:
                self._log_debug("Ignoring shell websocket close failure.")
        self._subscribers.clear()

        site = self._site
        self._site = None
        if site is not None:
            await site.stop()

        runner = self._runner
        self._runner = None
        if runner is not None:
            await runner.cleanup()

        window_host = self._window_host
        self._window_host = None
        if window_host is not None:
            self._save_window_geometry(window_host)
            window_host.stop()

        self.shell_url = ""

    async def broadcast(self, payload: dict[str, Any]) -> None:
        self._remember_payload(payload)
        dead: list[web.WebSocketResponse] = []
        for ws in tuple(self._subscribers):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._subscribers.discard(ws)

    def build_control_surface_payload(self) -> dict[str, Any]:
        actions: list[str]
        if self._window_host is None:
            actions = ["reopen_window"]
        else:
            actions = [
                "toggle_click_through",
                "unlock_drag",
                "reset_position",
                "grow_window",
                "shrink_window",
                "minimize_window",
                "open_settings",
                "close_window",
            ]
        return {
            "enabled": True,
            "interactive": bool(self._interactive),
            "click_through": bool(self._click_through),
            "actions": actions,
        }

    def register_model_file(self, model_path: str | Path) -> str | None:
        candidate = Path(model_path).expanduser().resolve()
        if candidate.is_dir():
            model_files = list(candidate.glob("*.model3.json"))
            if not model_files:
                return None
            model_file = model_files[0]
            model_root = candidate
        elif candidate.is_file():
            model_file = candidate
            model_root = candidate.parent
        else:
            return None
        self._model_root = model_root
        relative_path = quote(model_file.relative_to(model_root).as_posix())
        return f"http://{self._client_host()}:{int(self.config.port)}/shell/model/{relative_path}"

    async def update_control_state(
        self,
        *,
        click_through: bool | None = None,
        interactive: bool | None = None,
    ) -> dict[str, Any]:
        if click_through is not None:
            self._click_through = bool(click_through)
            if self._click_through:
                self._interactive = False
        if interactive is not None:
            self._interactive = bool(interactive)
            if self._interactive:
                self._click_through = False
        window_host = self._window_host
        if window_host is not None:
            window_host.apply_click_through(self._click_through)
        await self.broadcast({"type": "set_interactive", "interactive": bool(self._interactive)})
        await self.broadcast({"type": "shell_controls", **self.build_control_surface_payload()})
        if not self._interactive:
            await self.broadcast({"type": "toggle_menu", "open": False})
        return self.build_control_surface_payload()

    async def dispatch_control_action(self, action: str) -> dict[str, Any]:
        normalized = str(action or "").strip().lower()
        if normalized == "toggle_click_through":
            return await self.update_control_state(
                click_through=not self._click_through,
                interactive=self._click_through,
            )
        if normalized == "unlock_drag":
            return await self.update_control_state(click_through=False, interactive=True)
        if normalized == "reset_position":
            if self._window_host is not None:
                self._window_host.reset_position()
            return self.build_control_surface_payload()
        if normalized == "open_settings":
            if self._window_host is not None:
                self._window_host.focus()
            payload = self.build_control_surface_payload()
            await self.broadcast({"type": "shell_controls", **payload})
            return payload
        if normalized == "grow_window":
            if self._window_host is not None:
                self._window_host.resize_by(120, 120)
            payload = self.build_control_surface_payload()
            await self.broadcast({"type": "shell_controls", **payload})
            return payload
        if normalized == "shrink_window":
            if self._window_host is not None:
                self._window_host.resize_by(-120, -120)
            payload = self.build_control_surface_payload()
            await self.broadcast({"type": "shell_controls", **payload})
            return payload
        if normalized == "minimize_window":
            if self._window_host is not None:
                self._window_host.minimize()
            payload = self.build_control_surface_payload()
            await self.broadcast({"type": "shell_controls", **payload})
            return payload
        if normalized == "close_window":
            if self._window_host is not None:
                self._save_window_geometry(self._window_host)
                self._window_host.stop()
                self._window_host = None
            payload = self.build_control_surface_payload()
            await self.broadcast({"type": "shell_controls", **payload})
            return payload
        if normalized == "reopen_window":
            if self._window_host is None and self.shell_url:
                self._start_window_host()
            payload = self.build_control_surface_payload()
            await self.broadcast({"type": "shell_controls", **payload})
            return payload
        raise ValueError(f"unsupported shell control action: {action}")

    def _ensure_routes(self) -> None:
        if self._routes_ready:
            return
        asset_root = self._asset_root()
        vendor_root = self._vendor_root()
        self._app.router.add_get("/shell", self._handle_shell_index)
        self._app.router.add_get("/shell/", self._handle_shell_index)
        self._app.router.add_get("/shell/ws", self._handle_ws)
        self._app.router.add_get("/shell/model/{asset_path:.*}", self._handle_model_asset)
        self._app.router.add_static("/shell/assets", str(asset_root))
        self._app.router.add_static("/live2d_soullink_vendor/frontend_legacy", str(vendor_root))
        self._app.router.add_get(
            r"/{asset_name:index\.html|shell\.css|shell\.js|shell-legacy-compat\.js}",
            self._handle_shell_asset,
        )
        self._routes_ready = True

    async def _handle_shell_index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(self._asset_root() / "index.html")

    async def _handle_shell_asset(self, request: web.Request) -> web.FileResponse:
        asset_name = request.match_info["asset_name"]
        return web.FileResponse(self._asset_root() / asset_name)

    async def _handle_model_asset(self, request: web.Request) -> web.StreamResponse:
        model_root = self._model_root
        if model_root is None:
            raise web.HTTPNotFound()
        asset_path = str(request.match_info.get("asset_path") or "").strip("/")
        if not asset_path:
            raise web.HTTPNotFound()
        candidate = (model_root / asset_path).resolve()
        try:
            candidate.relative_to(model_root.resolve())
        except ValueError as exc:
            raise web.HTTPForbidden() from exc
        if not candidate.exists() or not candidate.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(candidate)

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._subscribers.add(ws)
        try:
            await ws.send_json({"type": "set_interactive", "interactive": bool(self._interactive)})
            await ws.send_json({"type": "shell_controls", **self.build_control_surface_payload()})
            for payload in self._replay_payloads():
                await ws.send_json(payload)
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
                if message.type == WSMsgType.CLOSE:
                    break
                if message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if str(payload.get("type") or "").strip().lower() == "control_action":
                        action = str(payload.get("action") or "").strip()
                        if action:
                            with contextlib.suppress(Exception):
                                await self.dispatch_control_action(action)
        finally:
            self._subscribers.discard(ws)
        return ws

    def _start_window_host(self) -> None:
        geometry = self._load_window_geometry()
        options = SoulLinkShellWindowOptions(
            url=self.shell_url,
            on_top=bool(getattr(self.config, "always_on_top", True)),
            click_through=bool(self._click_through),
            open_devtools=bool(getattr(self.config, "open_devtools", False)),
            width=int(geometry.get("width", 960)),
            height=int(geometry.get("height", 1080)),
            x=int(geometry.get("x", 240)),
            y=int(geometry.get("y", 120)),
        )
        self._window_host = SoulLinkShellWindowHost(options=options, logger=self.logger)
        self._window_host.start()
        self._remember_payload({"type": "shell_controls", **self.build_control_surface_payload()})

    def _asset_root(self) -> Path:
        return Path(__file__).resolve().parent / "webui" / "soullink_shell"

    def _vendor_root(self) -> Path:
        return Path(__file__).resolve().parent / "live2d_soullink_vendor" / "frontend_legacy"

    def _geometry_store_path(self) -> Path:
        return Path(__file__).resolve().parent / "data" / "soullink_shell_window_geometry.json"

    def _load_window_geometry(self) -> dict[str, int]:
        if not bool(getattr(self.config, "remember_window_geometry", True)):
            return {}
        path = self._geometry_store_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}
        geometry: dict[str, int] = {}
        for key in ("x", "y", "width", "height"):
            try:
                geometry[key] = int(payload[key])
            except (KeyError, TypeError, ValueError):
                continue
        return geometry

    def _save_window_geometry(self, window_host: SoulLinkShellWindowHost) -> None:
        if not bool(getattr(self.config, "remember_window_geometry", True)):
            return
        capture = getattr(window_host, "capture_geometry", None)
        if not callable(capture):
            return
        geometry = capture()
        if not isinstance(geometry, dict):
            return
        normalized: dict[str, int] = {}
        for key in ("x", "y", "width", "height"):
            try:
                normalized[key] = int(geometry[key])
            except (KeyError, TypeError, ValueError):
                return
        path = self._geometry_store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")

    def _client_host(self) -> str:
        host = str(getattr(self.config, "host", "") or "").strip()
        if not host:
            return "127.0.0.1"
        candidate = host.strip("[]")
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            return host
        if parsed.is_unspecified:
            return "::1" if parsed.version == 6 else "127.0.0.1"
        return host

    def _remember_payload(self, payload: Mapping[str, Any] | dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        payload_type = str(payload.get("type") or "").strip().lower()
        if payload_type in {"load_model", "expression", "tts_motion_frame", "reset", "toggle_menu", "shell_controls"}:
            self._sticky_payloads[payload_type] = dict(payload)

    def _replay_payloads(self) -> list[dict[str, Any]]:
        ordered_types = ("load_model", "reset", "expression", "tts_motion_frame", "toggle_menu")
        payloads: list[dict[str, Any]] = []
        for payload_type in ordered_types:
            payload = self._sticky_payloads.get(payload_type)
            if isinstance(payload, dict):
                payloads.append(dict(payload))
        return payloads

    def _log_debug(self, message: str) -> None:
        if self.logger is not None and hasattr(self.logger, "debug"):
            self.logger.debug(message)
