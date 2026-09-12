# maibot_bilibili_live_adapter

MaiBot 的 Bilibili 直播适配插件（旧版本，从 MaiBot 主仓 `plugins/` 目录拷贝出来独立维护）。

一个 **Input-only（仅输入）** 的 Bilibili 直播适配器：从 B 站直播间接收弹幕事件注入 MaiBot 主链路，
同时将 MaiBot 的回复在本机渲染为「语音 + 绿幕字幕 + Live2D/立绘/音效」等直播表现，但**不把回复写回 B 站**。

- 插件 ID：`maibot.bilibili-live-adapter`（manifest v2）
- SDK 要求：`maibot-plugin-sdk>=2.3.0`
- 宿主要求：MaiBot 主仓环境（依赖 `src.*` 内部模块，见下文）

---

## 1. 架构总览

插件整体呈「两条链路 + 一个编排核心」结构：

```
┌───────────────────────────── 输入链路（Input-only） ─────────────────────────────┐
│                                                                                   │
│  Bilibili WS 弹幕 ──┐                                                            │
│  Hub 输入事件 ──────┼─→ transport / hub_input_client / local_voice / vision        │
│  本地语音 (ASR) ────┤       │  (规范化 live event)                                  │
│  视觉 / 视频观看 ───┘       ▼                                                     │
│                        event_router（去重 / 筛选 / 窗口缓冲 / 空闲话题规划）        │
│                              │                                                    │
│                        interaction_planner（弹幕采样与优先级选择）                 │
│                              │                                                    │
│                        message_codec（→ MaiBot MessageDict）                      │
│                              │                                                    │
│                        ctx.gateway.route_message → MaiBot 主链路                   │
└───────────────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────── 输出链路（本地表现，不回写 B 站） ────────────────────┐
│                                                                                   │
│  MaiBot 回复 ──→ handle_bilibili_gateway（duplex gateway）                        │
│                        │                                                          │
│              _deliver_text_reply_serialized（串行化投递 + 打断/等待协调）           │
│                        ├─→ TTS 合成 → 本地音频播放（sounddevice）                 │
│                        ├─→ 字幕 WebUI（绿幕抠像，供 OBS 使用）+ 双语翻译          │
│                        ├─→ Live2D 口型 / 表情 / 动作（SoulLink / VTS / 具身化）    │
│                        ├─→ 音效板触发（关键词 / LLM 自动选择）                    │
│                        ├─→ 立绘（tachi-e）表情切换                                │
│                        └─→ Hub 输出转发（多 AI 直播间的发言协调）                  │
└───────────────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────── 编排核心 ─────────────────────────────┐
│  plugin.py：BilibiliLiveAdapterPlugin                               │
│  生命周期（on_load / on_unload / on_config_update → _restart_runtime）│
│  装配 transport / router / planner / live2d / tts / soundboard 等   │
└─────────────────────────────────────────────────────────────────────┘
```

核心设计原则：

- **输入链路**：高频采集、低频理解、事件驱动注入；弹幕经去重/筛选/窗口缓冲后批量注入，避免刷屏。
- **输出链路**：回复投递全程**串行化**（`_run_serialized_delivery`），支持打断、语音租约（Hub 多 AI 协调）、分段渲染。
- **Input-only**：`handle_bilibili_gateway` 是 duplex 网关但从不把回复发回 B 站，只驱动本地表现。

---

## 2. 模块清单

### 2.1 入口与生命周期

| 模块 | 职责 |
| --- | --- |
| `plugin.py` | 主插件类 `BilibiliLiveAdapterPlugin`（约 7700 行）：gateway / hooks / 渲染投递编排 / Hub 语音协调 / 音效板 / Live2D 命令 / 视觉命令 / 游戏桥 / RVC 点歌 / 运行时装配与重启 |
| `__init__.py` | 包入口，容错导出（无宿主环境时可轻量导入） |
| `_manifest.json` | 插件清单（manifest v2） |

### 2.2 配置与常量

| 模块 | 职责 |
| --- | --- |
| `config.py` | 约 50 个 pydantic 配置模型：`BilibiliConfig` / `IdentityConfig` / `FilterConfig` / `InteractionConfig` / `TopicExtensionConfig` / `NapCatControlConfig` / `HubInputConfig` / `HubOutputConfig` / `Live2D*` 系列（Adaptive/Sync/Override/Debug/Blink/Wink/Embodied/SoulLink/SoulLinkShell）/ `GameConfig` / `VisionConfig` / `VideoWatch*` / `SoundboardConfig` / `TTSConfig` / `STS2Config` / `WebUIConfig` / `LocalVoiceConfig` / `RvcSongRequestConfig` 等 |
| `constants.py` | 平台常量、B 站协议 op 码、默认 URL、配置版本号 |
| `runtime_state.py` | 通过 `gateway.update_state` 向宿主上报网关就绪状态 |

### 2.3 Bilibili 输入链路

| 模块 | 职责 |
| --- | --- |
| `bilibili_transport.py` | `BilibiliDanmakuTransport`：并行 WebSocket 弹幕传输（默认 4 路）、心跳、历史记录轮询补齐、跨源去重、身份富化 |
| `bilibili_codec.py` | B 站 WebSocket 包编解码（zlib 解压、op 码分发）与事件规范化 |
| `event_router.py` | `LiveEventRouter`：事件去重 / 清洗 / 窗口缓冲 / 批量注入网关；空闲话题（idle topic）规划与快照持久化；STS2 / Live2D debug / 付费事件处理 |
| `interaction_planner.py` | 弹幕采样与优先级选择，决定注入哪些弹幕进 MaiBot |
| `message_codec.py` | 外部事件 ↔ MaiBot `MessageDict` 转换；直播身份解析（`SessionUtils`） |
| `hub_input_client.py` | Hub 输入订阅：把共享 hub 事件镜像为直播事件（多 AI 协同直播） |

### 2.4 回复输出链路（本地渲染）

| 模块 | 职责 |
| --- | --- |
| `tts_provider.py` | TTS 提供者协议 + GPT-SoVITS 实现（`SynthesizedSpeech`） |
| `audio_output.py` | 本地 wav 播放（sounddevice 选定输出设备） |
| `local_voice_controller.py` / `local_voice_input.py` / `local_voice_native_runtime.py` / `local_voice_state.py` | 本地麦克风语音输入（连续采集 + 实时 ASR 后端）与本地回显控制窗口 |
| `subtitle_webui.py` / `subtitle_native_runtime.py` / `subtitle_native_segments.py` / `subtitle_native_state.py` | 绿幕字幕 WebUI 与原生运行时（供 OBS 抠像叠加） |
| `translation_client.py` | 字幕双语翻译（OpenAI 客户端） |
| `sts2_controller.py` / `sts2_llm_client.py` / `sts2_mcp_client.py` / `sts2_logging.py` | STS2 游戏控制器：决策客户端、MCP 工具、日志会话 |
| `webui/` | 前端静态资源（字幕绿幕 / 音效板 / SoulLink Shell） |

### 2.5 Live2D / 视觉表现

| 模块 | 职责 |
| --- | --- |
| `live2d_adaptive/` | 自适应 Live2D 参数控制：`bridge.py`（内存/JSON 桥）、`controller.py`、`embodied.py`（具身化运行时）、`soullink.py`、`capability_probe.py`（能力探测）、`semantic_mapper.py`（语义→参数映射）、`speech_timeline.py`（口型时间线）、`local_lipsync.py`、`mouse_follow.py`、`profile.py` |
| `live2d_control_state.py` | Live2D 控制状态持久化 |
| `live2d_shell_protocol.py` / `live2d_shell_runtime.py` / `live2d_shell_window.py` / `live2d_shell_window_process.py` | SoulLink Shell 桌面窗口运行时 |
| `live2d_soullink_vendor/` | [SoulLink_Live2D](https://github.com/nanlingyin/SoulLink_Live2D) 的 vendored 快照（MIT，见目录内 README/LICENSE） |
| `tachie_controller.py` / `_tachie_window.py` | LLM 驱动的立绘表情选择与显示 |
| `soundboard.py` / `soundboard_selection_client.py` | 音效板运行时 + 绿幕 WebUI 服务 + 音效自动选择客户端 |

### 2.6 视觉 / 视频观看

| 模块 | 职责 |
| --- | --- |
| `vision_tool.py` | 桌面截图 + 视觉模型总结（`VisionDesktopInspector`） |
| `video_watch.py` | 空闲时 B 站视频观看、离线分析、定时解说、记忆摄入 |

### 2.7 唱歌（RVC）系统

| 模块 | 职责 |
| --- | --- |
| `netease_client.py` / `audius_client.py` / `music_source_provider.py` | 音乐源（网易云 OpenAPI / Audius）与统一提供契约 |
| `audio_separator_bridge.py` | 人声分离 |
| `rvc_infer_bridge.py` / `rvc_song_pipeline.py` | RVC 推理桥与歌曲转换管线 |
| `song_request_service.py` / `song_request_console.py` / `manual_rvc_song_request.py` | 点歌队列、控制台会话、手动 CLI |

### 2.8 基础设施与辅助

| 模块 | 职责 |
| --- | --- |
| `bridge_client.py` | 通用 JSON bridge 客户端（游戏 / 显示集成） |
| `topic_extension_client.py` / `topic_state.py` | 话题扩展与空闲话题持久化 |
| `tools/` | 辅助脚本：VTube Studio 校准、音效导入、B 站弹幕注入/观看 |
| `tests/` | pytest 测试集（`_host_bootstrap.py` 负责引导宿主测试环境） |
| `docs/` | 文档（GPT-SoVITS 接入、VTube Studio 测试、音效板 cue 导入、插件总览） |

---

## 3. 宿主环境依赖

插件运行时需要 MaiBot 主仓环境，直接引用以下内部模块（无法通过 pip 安装）：

- `maibot_sdk`（`API` / `HookHandler` / `MaiBotPlugin` / `MessageGateway` / `PluginConfigBase` / `Tool`）
- `src.config.config.global_config` / `config_manager`
- `src.config.model_configs`（`APIProvider` / `ModelInfo`）
- `src.common.utils.utils_session.SessionUtils`
- `src.A_memorix.host_service.a_memorix_host_service`

`tools/` 下的脚本另依赖 `maim-message`（`==0.6.8`）。

## 4. 配置与运行

1. 复制 `config.example.toml` 为 `config.toml`，按需填写直播间号、模型标识与各服务密钥（示例文件中所有密钥均为空）。`config.toml` 已被 `.gitignore` 排除，不会被提交。
2. 运行期会自动在插件目录下创建 `data/`（凭据缓存、状态、音效素材）与 `logs/`，两者均不入库。
3. 本地语音输入（`local_voice`）依赖 [sherpa-onnx](https://pypi.org/project/sherpa-onnx/) 流式 ASR，为可选依赖：

```bash
pip install -e ".[local-voice]"
```

---

## 5. 开发与测试

```bash
# 依赖安装（优先 uv）
uv sync
# 或
pip install -r requirements.txt

# 运行测试（tests/_host_bootstrap.py 会注入宿主核心仓库；默认寻找上一级 MaiBot-r-dev，
# 也可用环境变量 MAIBOT_CORE_REPO 指定 MaiBot 主仓路径）
uv run pytest tests/
```

测试注意：`tests/_host_bootstrap.py` 会向 `sys.path` 注入宿主仓库并生成临时配置文件，属于本插件的宿主引导逻辑。

---

## 6. 许可

- 本插件整体遵循 `GPL-3.0-or-later`（见 `_manifest.json`）。
- `live2d_soullink_vendor/` 内的代码来自 [SoulLink_Live2D](https://github.com/nanlingyin/SoulLink_Live2D)，按其声明的 MIT License 再分发，详见目录内 [LICENSE](live2d_soullink_vendor/LICENSE)。
- `data/` 中的音效、立绘等素材不在本仓库分发范围内，使用时请自行准备并确认素材授权。

### 维护约定

- 注释 / 日志 / WebUI 展示语言优先简体中文。
- import 顺序：标准库与第三方（`from ... import ...` 在前、`import ...` 在后，各自按字母序）→ 本地模块（同目录相对导入，跨目录以 `from src` 绝对导入）。
- 依赖以 `pyproject.toml` 为准，修改后同步更新 `requirements.txt`。
- 配置文件改动只改模板并递增版本号，不直接改运行配置；`legacy_migration` 禁止改动。
