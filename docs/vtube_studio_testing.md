# VTube Studio 接入测试流程

这个测试桥负责把 MaiBot 插件的 Live2D JSON 协议转换成 VTube Studio Public API。

链路：

```text
MaiBot 插件
  -> ws://127.0.0.1:18081/live2d
      -> tools/vtube_studio_bridge.py
          -> ws://127.0.0.1:8002
              -> VTube Studio
```

## 1. 打开 VTube Studio

1. 打开 VTube Studio。
2. 加载要测试的 Live2D 模型。
3. 在 VTube Studio 设置中开启 `Allow Plugin API access`。
4. 确认 VTS API 地址是默认的 `ws://127.0.0.1:8002`。如果你改了端口，启动桥时传 `--vts-url`。

注意：`0.0.0.0` 是服务端监听所有网卡的绑定地址，不是客户端连接地址。MaiBot 插件和桥接器参数里用于连接时请写 `127.0.0.1` 或你的局域网 IP，不要写 `0.0.0.0`。

## 2. 启动本地桥

在仓库根目录运行：

```powershell
python .\plugins\maibot_bilibili_live_adapter\tools\vtube_studio_bridge.py --log-level INFO
```

首次运行时，VTube Studio 会弹出插件授权窗口。点击 `Allow`。桥会把 token 保存到：

```text
plugins/maibot_bilibili_live_adapter/data/vts_auth_token.json
```

如果你撤销了 VTS 插件授权，删除这个 token 文件后重新运行桥即可。

端口角色不要混用：

- VTube Studio API：默认 `ws://127.0.0.1:8002`
- 本地 MaiBot 桥：默认 `ws://127.0.0.1:18081/live2d`

如果你的 VTube Studio API 已经改成了 `18081`，请二选一：

1. 把 VTube Studio API 改回 `8002`，继续用桥默认端口 `18081`。
2. 保持 VTube Studio API 为 `18081`，但把桥改到别的端口，例如：

```powershell
python .\plugins\maibot_bilibili_live_adapter\tools\vtube_studio_bridge.py --listen-port 18082 --vts-url ws://127.0.0.1:18081 --log-level INFO
```

此时插件配置里的 `live2d.websocket_url` 要写：

```toml
websocket_url = "ws://127.0.0.1:18082/live2d"
```

## 3. 修改插件配置

编辑：

```text
plugins/maibot_bilibili_live_adapter/config.toml
```

把 Live2D 段改成：

```toml
[live2d]
enabled = true
driver = "json"
http_url = ""
websocket_url = "ws://127.0.0.1:18081/live2d"
auth_token = ""
connect_timeout_sec = 10.0
send_bot_replies = true
forward_inbound_danmaku = false

[live2d.sync]
enabled = true
chars_per_second = 7.5
prepare_ms = 180
release_ms = 600
mouth_update_interval_ms = 80
parameter_keepalive_ms = 650
```

`driver = "json"` 很重要。不要把插件的 `websocket_url` 直接写成 VTS 的 `ws://127.0.0.1:8002`，因为插件输出的是自定义桥协议，不是 VTS 原生 API。

## 4. 先测几个基础动作

优先测试这些参数，通常 VTube Studio 默认模型已经有映射：

- `ParamAngleX/Y/Z` -> `FaceAngleX/Y/Z`
- `ParamMouthOpenY` -> `MouthOpen`
- `ParamMouthSmile` -> `MouthSmile`
- `ParamEyeLOpen/ParamEyeROpen` -> `EyeOpenLeft/EyeOpenRight`
- `ParamEyeBallX/Y` -> `EyeLeftX/EyeRightX/EyeLeftY/EyeRightY`

如果模型没有动，先在 VTS 模型设置里确认输入参数到输出 Live2D 参数的映射存在。

也可以直接运行分步测试脚本：

```powershell
python .\plugins\maibot_bilibili_live_adapter\tools\test_vtube_studio_bridge_flow.py --interactive
```

默认会依次执行：

1. `capabilities`：请求模型参数画像。
2. `basic`：头部转向和嘴型开合。
3. `expression`：眼睛/微笑/脸红参数。
4. `timeline`：模拟 MaiBot 回复同步时间轴。

单独测试某一步：

```powershell
python .\plugins\maibot_bilibili_live_adapter\tools\test_vtube_studio_bridge_flow.py --step 1
python .\plugins\maibot_bilibili_live_adapter\tools\test_vtube_studio_bridge_flow.py --step 2
python .\plugins\maibot_bilibili_live_adapter\tools\test_vtube_studio_bridge_flow.py --step 3
python .\plugins\maibot_bilibili_live_adapter\tools\test_vtube_studio_bridge_flow.py --step 4
```

测试配饰参数：

```powershell
python .\plugins\maibot_bilibili_live_adapter\tools\test_vtube_studio_bridge_flow.py --step accessory --accessory glasses
```

配饰测试只会发送 `ParamAccessoryGlasses` 参数。要看到模型变化，需要先在 VTube Studio 模型设置里把这个 custom input 映射到实际配饰输出参数。

## 5. 配饰和特殊表情

配饰、脸红、特殊眼笑等不是 VTS 默认输入参数时，桥会自动创建同名或安全命名的 custom parameter。

创建后，你需要在 VTube Studio 模型设置里手动加映射：

```text
Input parameter: ParamAccessoryGlasses
Output Live2D parameter: ParamAccessoryGlasses
```

然后插件的 `toggle_accessory` 才会真正控制模型配饰。

## 6. 自定义映射

如果你的模型或 VTS 配置使用了不同输入参数名，可以创建一个 JSON 映射文件：

```json
{
  "ParamAngleX": "FaceAngleX",
  "ParamMouthOpenY": "MouthOpen",
  "ParamAccessoryGlasses": "MyGlassesInput",
  "ParamEyeBallX": ["EyeLeftX", "EyeRightX"]
}
```

启动桥时传：

```powershell
python .\plugins\maibot_bilibili_live_adapter\tools\vtube_studio_bridge.py --mapping-file .\my_vts_mapping.json
```

## 7. 常见问题

- 桥连不上 VTS：确认 VTube Studio 已开启 `Allow Plugin API access`，端口是 8002，没有被防火墙拦截。
- URL 显示 `ws://0.0.0.0:...`：这是监听地址，不是连接地址；客户端配置请改成 `ws://127.0.0.1:...`。
- VTS 弹窗不出现：确认桥的 `pluginName/pluginDeveloper` 没有超过 VTS 要求，或者删除旧 token 后重试。
- 参数注入成功但模型不动：通常是 VTS 模型设置中输入参数没有绑定到输出 Live2D 参数，或者该参数被 expression/motion/physics 覆盖。
- 嘴型动一下就停：确认插件配置里 `parameter_keepalive_ms` 小于 1000，默认 650ms。
- 配饰参数不动：先确认 custom parameter 已创建，再手动绑定到模型的配饰输出参数。
