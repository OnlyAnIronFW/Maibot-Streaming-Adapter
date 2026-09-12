# live2d_soullink_vendor

本目录是 [SoulLink_Live2D](https://github.com/nanlingyin/SoulLink_Live2D)（LLM 驱动的 Live2D 表情控制系统，作者 nanlingyin）的 vendored 快照，经适配层（adapter shims）供本插件内嵌使用，不再跟随上游更新。

## 内容构成

- `src/` — 上游 Python 后端的历史快照子集（`config/` 配置模型、`generators/` 表情生成），通过本插件的 shim 模块调用。
- `frontend_legacy/` — 上游已移除的旧版前端（Live2D 参数控制、TTS、WebSocket 服务等 JS），由 `live2d_shell_runtime.py` 以静态资源方式对外提供。

## 许可

上游项目在 README 中声明采用 **MIT License**（截至快照时尚未随附正式 LICENSE 文本）。本目录按 MIT 条款再分发，详见 [LICENSE](./LICENSE)。

相对上游快照的本地改动仅限与 MaiBot 插件宿主的对接（导入路径、配置注入等），核心控制逻辑保持上游原样。
