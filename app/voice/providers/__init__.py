"""声音生成协议注册表：把「接哪一家」收敛成一个按协议名解析的入口。

与 ``app.video_providers`` 同一形状：``VOICE_PROTOCOLS`` 只登记协议名 ->
适配器模块的 dotted path，真正 import 推迟到 ``adapter_for()``，避免 import
本模块时把每一家供应商的 httpx 客户端代码都拉起来（理由与
``app.video_providers._BUILTIN_FACTORIES`` 相同）。

新增一家只需要实现一个模块（暴露 ``design_voice(conn, req)``/``probe(conn)``
两个 async 函数）并在 ``VOICE_PROTOCOLS`` 里登记一行——这就是「以后可以随意
切换音频模型」的落点：换绑定即换 protocol/model_id，
``app.voice.providers.dispatch.design_voice()`` 不用改一行。
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

# 协议名（存进模型库 protocol 字段的值）-> 适配器模块的完整 dotted path。
VOICE_PROTOCOLS: dict[str, str] = {
    "qwen_voice_design": "app.voice.providers.qwen_voice_design",
    "minimax_voice_design": "app.voice.providers.minimax_voice_design",
}

# 给前端「添加模型」弹窗的中文名与填写示例；清单只在后端维护一份，前端不再
# 各自抄一遍协议说明（避免两侧漂移）。
PROTOCOL_HINTS: dict[str, dict[str, str]] = {
    "qwen_voice_design": {
        "label": "千问声音设计（阿里百炼）",
        "base_url_example": "https://dashscope.aliyuncs.com/api/v1",
        "model_example": "qwen3-tts-vd-2026-01-26",
        "note": "模型标识填声音设计的目标合成模型",
    },
    "minimax_voice_design": {
        "label": "MiniMax 声音设计",
        "base_url_example": "https://api.minimaxi.com",
        "model_example": "voice_design",
        "note": "声音设计接口不区分模型型号，模型标识只作本条目的标识；国内站填 api.minimaxi.com，国际站填 api.minimax.io",
    },
}

_CACHE: dict[str, Any] = {}


def adapter_for(protocol: str) -> Any | None:
    """按协议名取适配器模块；未知协议返回 None，不猜、不回落到别的协议。"""
    key = str(protocol or "").strip().lower()
    module_path = VOICE_PROTOCOLS.get(key)
    if not module_path:
        return None
    if key not in _CACHE:
        _CACHE[key] = import_module(module_path)
    return _CACHE[key]
