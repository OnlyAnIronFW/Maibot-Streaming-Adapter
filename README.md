# Maibot Streaming Adapter

> [!WARNING]
> **这是一个年久失修的毛坯房。**
> 本插件约 99% 的代码由 AI 生成，作者在自己的环境里跑通过完整流程，但没有精力持续维护和打磨。
> 它**不是开箱即用的成品**：配置项繁多、外部依赖众多，换个环境大概率需要修修补补。
> 预期的使用方式就是：**自己借助 AI 辅助阅读代码、排查报错、生成配置**，边跑边修。
> 如果你想找一个装完就能用的直播插件，这个项目目前不适合你。

一个 **Input-only（仅输入）** 的 MaiBot Bilibili 直播适配插件：从 B 站直播间接收弹幕注入 MaiBot 主链路，
再把 MaiBot 的回复在本机渲染成「语音 + 绿幕字幕 + Live2D / 立绘 / 音效板」等直播表现。**不把回复写回 B 站**。

- 插件 ID：`maibot.bilibili-live-adapter`（manifest v2）
- SDK 要求：`maibot-plugin-sdk>=2.3.0`
- 宿主要求：MaiBot 主仓环境（插件深度依赖宿主内部模块，无法独立 pip 安装运行，见下文）

---

## 效果展示

🎥 **[国产Neuro 但是炫压抑夏亚在直播间（Bilibili 演示视频）](https://www.bilibili.com/video/BV1ak9qBoExY/)**

视频为作者实际直播录屏：弹幕实时接入 → MaiBot 生成回复 → 本地 TTS 语音 / 字幕 / Live2D / 音效板协同演出。

## 功能一览

### 直播输入

- **B 站弹幕实时接入**：并行 WebSocket 采集（默认 4 路）、跨源去重、窗口缓冲与弹幕采样，支持礼物 / SC / 上舰事件路由
- **空闲话题**：弹幕冷场时让 bot 主动找话题接住直播间
- **多 AI 协同直播**：`livehub` 独立采集服务端 + 语音互斥租约，多个 AI bot 同台直播不抢话
- **本地麦克风语音输入**（可选）：sherpa-onnx 流式 ASR，主播说话直接进 MaiBot

### 直播表现（本地渲染，不回写 B 站）

- **TTS 语音**：GPT-SoVITS v2 API 合成，本地播放，与 Live2D 口型联动
- **绿幕字幕 WebUI**：供 OBS 浏览器源抠像叠加，支持中英/中日双语翻译字幕
- **Live2D 控制**：口型 / 表情 / 动作参数控制（SoulLink、VTube Studio 桥、具身化运行时三种方案）、鼠标跟随、自动眨眼与 wink
- **立绘表情切换**：LLM 根据回复内容选择立绘姿态与情绪
- **音效板**：关键词 / LLM 自动选梗触发 + WebUI 手动点按，绿幕视频/GIF 素材支持

### 扩展玩法

- **桌面视觉**：`/vision` 让 bot 定时看主播桌面截图，对游戏画面实时吐槽
- **视频一起看**：冷场时邀请观众开 B 站视频，离线解析后按时间轴吐槽
- **RVC 点歌**：网易云 OpenAPI / Audius 检索 → 人声分离 → RVC 换声翻唱 → 重新混音
- **STS2 游戏实况**：通过 MCP 操控《文明6》并生成实时解说（依赖 STS2-Agent MCP 服务，可选）

## 工作原理

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
│  MaiBot 回复 ──→ 串行化投递（支持打断 / 语音租约 / 分段渲染）                       │
│        ├─→ TTS 合成 → 本地音频播放                                                 │
│        ├─→ 字幕 WebUI（绿幕抠像）+ 双语翻译                                        │
│        ├─→ Live2D 口型 / 表情 / 动作                                               │
│        ├─→ 音效板触发 / 立绘表情切换                                               │
│        └─→ Hub 输出转发（多 AI 直播间发言协调）                                     │
└───────────────────────────────────────────────────────────────────────────────────┘
```

- **输入链路**：高频采集、低频理解；弹幕经去重/筛选/窗口缓冲后批量注入，避免刷屏。
- **输出链路**：回复投递全程串行化，支持打断、语音租约（多 AI 协调）、分段渲染。
- **Input-only**：gateway 从不把回复发回 B 站，只驱动本地表现——安全，也不会刷屏直播间。

## 使用方式

> 再说一遍：以下步骤在你的环境里不一定一次跑通。卡住了就把报错和 `config.example.toml` 丢给 AI，
> 让它帮你定位问题、生成适合你环境的配置——这正是本项目的预期用法。

### 1. 前置要求

| 依赖 | 说明 |
| --- | --- |
| MaiBot 主仓环境 | 必需。插件直接引用宿主内部模块（`maibot_sdk`、`src.*`），需在 MaiBot 主仓的插件机制下运行 |
| Python ≥ 3.12 | 必需（跟随 MaiBot 主环境） |
| [GPT-SoVITS v2](docs/gpt_sovits_v2_tts.md) | TTS 必需。本地起 API 服务（默认 `http://127.0.0.1:9880`），参考音频自备 |
| ffmpeg / ffprobe | 视频观看、RVC 点歌功能需要，加入 PATH 或在配置里写绝对路径 |
| [sherpa-onnx](https://pypi.org/project/sherpa-onnx/) | 可选。本地麦克风语音输入的流式 ASR 运行时 |
| [VTube Studio](docs/vtube_studio_testing.md) | 可选。想用 VTS 渲染 Live2D 时才需要 |

### 2. 安装

把本插件放进 MaiBot 主仓的 `plugins/` 目录，然后安装 Python 依赖（优先 uv）：

```bash
uv sync
# 或
pip install -r requirements.txt

# 如需本地麦克风语音输入，额外安装可选依赖组：
pip install -e ".[local-voice]"
```

### 3. 配置

```bash
cp config.example.toml config.toml
```

示例文件里**所有密钥均为空、所有个人路径已清空**，需要你按自己的环境填写。配置块很多，
建议让 AI 辅助你按需生成——最起码要填这些：

| 配置块 | 要填什么 |
| --- | --- |
| `[bilibili]` | `room_id` 你的 B 站直播间号 |
| `[interaction.topic_extension]` 等 LLM 段 | `api_provider` / `model_identifier`：使用 MaiBot 主配置里已有的模型供给 |
| `[tts]` | `base_url`（GPT-SoVITS API 地址）、`ref_audio_path` / `aux_ref_audio_paths` / 权重路径（参考音频自备） |
| `[live2d.*]` | 用哪套 Live2D 方案就开哪段，不用的保持 `enabled = false` |
| `[vision]` / `[video_watch]` / `[sts2]` / `[song_request]` / `[local_voice]` | 各扩展玩法独立开关，不用就不开 |

`config.toml` 已被 `.gitignore` 排除，不会被提交。运行期会在插件目录下自动创建 `data/`（凭据缓存、状态、素材）与 `logs/`。

`livehub`（多 AI 协同采集服务端）是独立组件，配置见 `livehub/config.example.toml`；单 bot 直播可以不用它。

### 4. 运行与验证

启动 MaiBot 后插件自动加载。本机会起几个本地服务：

- 字幕绿幕页：`http://127.0.0.1:18182`（OBS 里添加浏览器源，勾选绿幕抠像）
- 音效板 WebUI：`http://127.0.0.1:18184`
- 立绘显示窗：`http://127.0.0.1:18185`

### 5. 弹幕命令（管理员）

下列命令默认只对配置里 `authorized_identities` / `admin_user_ids` 中的用户生效：

| 命令 | 作用 |
| --- | --- |
| `/vision` | 开关桌面视觉吐槽 |
| `/watchvideo` | 开关 B 站视频一起看 |
| `/soundboard on\|off\|status` | 音效板总开关与状态 |
| `/sts2start` / `/sts2stop` / `/sts2status` | STS2 游戏实况控制 |
| `/l2d ...` | Live2D 调试命令（表情 / 动作 / 特殊演出） |

## 模块清单

<details>
<summary>点开查看代码结构（面向二次开发）</summary>

### 入口与配置

| 模块 | 职责 |
| --- | --- |
| `plugin.py` | 主插件类 `BilibiliLiveAdapterPlugin`：生命周期、渲染投递编排、Hub 语音协调、各命令路由、运行时装配 |
| `config.py` | 约 50 个 pydantic 配置模型，对应 `config.toml` 各配置块 |
| `constants.py` / `runtime_state.py` | 协议常量、网关状态上报 |

### 输入链路

| 模块 | 职责 |
| --- | --- |
| `bilibili_transport.py` / `bilibili_codec.py` | 并行 WebSocket 弹幕传输与 B 站协议编解码 |
| `event_router.py` | 事件去重 / 清洗 / 窗口缓冲 / 空闲话题规划 |
| `interaction_planner.py` / `message_codec.py` | 弹幕采样选择、事件 → MaiBot `MessageDict` 转换 |
| `hub_input_client.py` | Hub 输入订阅（多 AI 协同） |

### 输出链路（本地渲染）

| 模块 | 职责 |
| --- | --- |
| `tts_provider.py` / `audio_output.py` | GPT-SoVITS 合成与本地播放 |
| `subtitle_*.py` / `translation_client.py` | 绿幕字幕 WebUI、原生运行时、双语翻译 |
| `live2d_adaptive/` / `live2d_shell_*.py` | 自适应 Live2D 参数控制与 SoulLink Shell 桌面窗口 |
| `live2d_soullink_vendor/` | [SoulLink_Live2D](https://github.com/nanlingyin/SoulLink_Live2D) vendored 快照（MIT，见目录内说明） |
| `tachie_controller.py` / `_tachie_window.py` | 立绘表情选择与显示 |
| `soundboard.py` / `soundboard_selection_client.py` | 音效板运行时与自动选梗 |

### 扩展与基础设施

| 模块 | 职责 |
| --- | --- |
| `vision_tool.py` / `video_watch.py` | 桌面视觉、B 站视频一起看 |
| `netease_client.py` / `audius_client.py` / `rvc_*.py` / `song_request_*.py` | RVC 点歌系统 |
| `sts2_*.py` | STS2 游戏实况控制 |
| `livehub/` | 多 AI 协同采集服务端（独立进程） |
| `tools/` | VTube Studio 校准、音效导入（`import_soundboard_cue.bat`）、弹幕注入等辅助脚本 |
| `tests/` | pytest 测试集 |
| `docs/` | GPT-SoVITS 接入、VTube Studio 测试、音效 cue 导入等文档 |

</details>

## 开发与测试

```bash
# 依赖安装（优先 uv）
uv sync

# 运行测试（tests/_host_bootstrap.py 会注入宿主核心仓库；默认寻找上一级 MaiBot-r-dev，
# 也可用环境变量 MAIBOT_CORE_REPO 指定 MaiBot 主仓路径）
uv run pytest tests/
```

测试注意：`tests/_host_bootstrap.py` 会向 `sys.path` 注入宿主仓库并生成临时配置文件，属于本插件的宿主引导逻辑。

### 维护约定

- 注释 / 日志 / WebUI 展示语言优先简体中文。
- import 顺序：标准库与第三方（`from ... import ...` 在前、`import ...` 在后，各自按字母序）→ 本地模块。
- 依赖以 `pyproject.toml` 为准，修改后同步更新 `requirements.txt`。

## 许可

- 本插件整体遵循 `GPL-3.0-or-later`（见 `_manifest.json`）。
- `live2d_soullink_vendor/` 内的代码来自 [SoulLink_Live2D](https://github.com/nanlingyin/SoulLink_Live2D)，按其声明的 MIT License 再分发，详见目录内 [LICENSE](live2d_soullink_vendor/LICENSE)。
- `data/` 中的音效、立绘、Live2D 模型等素材不在本仓库分发范围内，使用时请自行准备并确认素材授权。
