"""livehub 测试公共配置：确保插件目录及其父目录可被导入。

- 插件目录（含 livehub 子包）加入 sys.path，供 ``from livehub...`` 导入。
- 插件目录的父目录（MaiBot 宿主根）加入 sys.path，供交叉一致性测试以包形式导入
  ``maibot_bilibili_live_adapter_copy.bilibili_codec``。
"""

from __future__ import annotations

import sys

from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parents[1]
for _candidate in (_PLUGIN_DIR, _PLUGIN_DIR.parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
