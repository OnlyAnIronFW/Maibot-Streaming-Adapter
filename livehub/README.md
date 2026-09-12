# livehub — 多 AI 直播间共享枢纽

`livehub` 是 `maibot_bilibili_live_adapter` 插件的配套服务端：多个 MaiBot 实例（bot）通过它共享同一个 B 站直播间的弹幕流、互相感知发言并协调本地语音输出，避免每个 bot 各自直连 B 站。

原 livehub 代码已丢失，本目录为按插件侧客户端协议（`hub_input_client.py`、`plugin.py` hub 逻辑）逆向推导后重写的服务端。

## 功能

- **B 站弹幕采集**：直连 B 站直播间 WebSocket（`danmaku` / `super_chat` / `gift` / `guard`），多路并行 + 断线重连。
- **事件汇聚广播**：为每条事件分配递增 `seq`，通过 HTTP 轮询与 WebSocket 推送给所有接入的 bot。
- **参与者管理**：各 bot 通过 presence 心跳注册，超时自动离线。
- **语音互斥协调**：跨 bot 的说话权队列（`speak-request` / `speak-complete`），当前说话者超时自动释放。
- **回复转发**：bot 回复经 `/api/client/reply` 进入事件流，其他 bot 可感知为 `bot_reply` 事件。

## 快速开始

```bash
# 1. 配置直播间 ID（必填）
#    编辑 livehub/config.toml，设置 room_id = <B站直播间ID>，可按需修改 port / auth_token

# 2. 启动（在插件目录下）
python -m livehub
# 或指定配置文件 / 覆盖参数
python -m livehub --config livehub/config.toml --room-id 22637261 --port 18190
```

启动后监听 `http://127.0.0.1:18190`（默认），插件侧 `[hub_input]` / `[hub_output]` 默认值即指向该地址，无需改动。

> 若配置了 `auth_token`，插件侧 `hub_input.auth_token` / `hub_output.auth_token` 需保持一致。
> 不配置 `room_id` 时 livehub 仅作为事件汇聚/转发枢纽运行（不采集 B 站弹幕）。

## HTTP API

| 端点 | 方法 | 请求体 | 响应 |
| --- | --- | --- | --- |
| `/api/events?limit=N` | GET | — | `{events, participants, speaking, health}`（`limit` 限定返回事件条数，缺省用 `history_limit`） |
| `/api/health` | GET | — | `{success, config, hub}` |
| `/api/client/presence` | POST | `{client_id, bot_name, forward_user_id?, forward_username?}` | 精简状态 `{participants, speaking, health}` |
| `/api/client/speak-request` | POST | `{request_id, client_id, bot_name, text, expected_duration_ms, room_id, live_event_type}` | `{granted, speaking}` |
| `/api/client/speak-complete` | POST | `{request_id, client_id, bot_name, status}` | `{speaking}` |
| `/api/client/reply` | POST | 直接形态 `{client_id, bot_name, text, ...}` 或插件 JsonBridgeClient 包装形态 `{id, type, payload: {...}}` | `{success, seq}` |
| `/api/inject` | POST | `{text, username?, user_id?, summary?, origin?}` | `{success, seq}` |
| `/ws` | GET | — | WebSocket 事件流 |

错误响应统一为 `{success: false, error: "..."}` + 4xx 状态码（空 body / 非法 JSON / 缺失必填字段时返回 400）。
若配置了 `auth_token`，所有 `/api/*` 与 `/ws` 请求必须携带 `Authorization: Bearer <token>`，否则 401。

## WebSocket 协议

连接 `/ws` 后服务端立即推送一条 `snapshot`，此后实时推送 `event` 与 `health`：

```json
{"kind": "snapshot", "events": [...], "participants": [...], "speaking": {...}, "health": {...}}
{"kind": "event", "event": {"seq": 12, "event_id": "...", "type": "danmaku", "origin": "bilibili", "text": "...", "summary": "...", "user_id": "...", "username": "...", "timestamp": 1710000000.0, "raw": {...}}}
{"kind": "health", "participants": [...], "speaking": {...}, "health": {...}}
```

事件 `type`：`danmaku` / `super_chat` / `gift` / `guard`（`origin="bilibili"`）、`local_inject`、`bot_reply`。

## 语音互斥流程

1. bot 发言前 `POST /api/client/speak-request`：无人说话 → 立即 `granted=true`；有人说话 → 进入等待队列（`granted=false`）。
2. 等待中的 bot 监听 WS `health.speaking.current.request_id` 变为自己的 `request_id` 即获得说话权。
3. 说完后 `POST /api/client/speak-complete`（`status=completed|cancelled|failed`）释放；说话权自动移交给队首等待者。
4. 当前说话者超过 `expected_duration_ms + speech_timeout_grace_sec` 未释放时，livehub 自动超时释放。

## 目录结构

```
livehub/
├── __init__.py           # 包信息
├── __main__.py           # 命令行入口（python -m livehub）
├── config.py             # 配置模型（TOML 加载 + 范围校验）
├── config.toml           # 默认配置模板
├── bilibili_protocol.py  # B 站协议编解码/事件规范化/地址解析（与插件共享的单一实现）
├── capture.py            # B 站弹幕采集器（复用 bilibili_protocol）
├── hub.py                # 核心状态机：事件环 / 参与者 / 语音队列 / 广播
├── _utils.py             # 共享工具（normalize_text / normalize_duration_ms）
└── server.py             # aiohttp 服务：HTTP API + WebSocket
```

插件侧 `bilibili_codec.py` 已改为对 `bilibili_protocol` 的 re-export（API 兼容），
`bilibili_transport.py` 的服务器地址解析同样复用 `bilibili_protocol.select_ws_urls`——
协议级改动只需修改 `bilibili_protocol.py` 一处。

## 可靠性设计

- **多路采集去重**：多条并行 WS 连接会收到相同弹幕，按事件 `event_id` 在 10 秒窗口内去重后再汇入事件流，避免重复触发下游。
- **重连退避**：B 站采集断线重连按 `base * 2^(n-1)` 指数退避（封顶 60s），连接稳定后自动恢复；`receive` 超时随心跳间隔联动，避免冷清直播间误判断线。
- **慢客户端保护**：WS 广播并行发送并对每个客户端限时 5 秒，超时即断开；广播任务积压超过 32 个时丢弃事件推送（事件仍可经 `GET /api/events` 轮询获取）。
- **资源上限**：语音等待队列与参与者数量各限制 128，防止异常客户端拖垮内存。
- **配置校验**：监听端口 / 直播间 ID / 超时等配置在加载时钳制到合法范围，非法值回退默认并记录警告。
- **启动失败回滚**：服务启动任一步骤失败会自动清理已启动的组件。

## 依赖

- Python >= 3.12
- `aiohttp>=3.12.14`（与插件共享依赖）
