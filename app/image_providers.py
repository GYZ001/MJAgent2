"""图像供应商接入层。

图像比视频规范得多：``POST {base}/images/generations`` 加 ``{model, prompt, n,
size}`` 已经是事实标准，各家的差别集中在"参考图怎么传"这一件事上——Seedream 把
data URL 塞进非标的 ``image`` 字段，OpenAI 走 ``/images/edits`` 的 multipart。
所以这里不需要视频那样的全套适配器，只需要一个方言开关。
"""
from __future__ import annotations

from typing import Any

# 页面自建实例可选的接入协议 → 参考图方言。
IMAGE_PROTOCOLS: dict[str, str] = {
    # 火山 Seedream：参考图放进请求体的 image 字段（单张给字符串，多张给数组）。
    "seedream": "inline_image_field",
    # 标准 OpenAI 图像接口：生成不带参考图，参考图属于 /images/edits，
    # 这里明确不静默降级——带着参考图提交会直接报错，而不是悄悄丢掉一致性约束。
    "openai": "generations_only",
}

DEFAULT_PROTOCOL = "seedream"


def protocol_for_provider(provider: str) -> str:
    """解析某个 provider 实例声明的图像协议。"""
    import json

    from app.db import get_setting

    name = str(provider or "").strip()
    if not name.startswith("custom:"):
        return DEFAULT_PROTOCOL
    try:
        custom = json.loads(get_setting("custom_models") or "[]")
    except (TypeError, ValueError):
        return DEFAULT_PROTOCOL
    if not isinstance(custom, list):
        return DEFAULT_PROTOCOL
    item = next(
        (
            entry for entry in custom
            if isinstance(entry, dict) and entry.get("provider") == name
        ),
        None,
    )
    protocol = str((item or {}).get("protocol") or "").strip().lower()
    return protocol if protocol in IMAGE_PROTOCOLS else DEFAULT_PROTOCOL


def apply_reference_images(
    payload: dict[str, Any],
    prepared_inputs: list[str],
    *,
    protocol: str,
) -> None:
    """按协议方言把参考图挂到请求体上（原地修改）。"""
    from app.hiagent import ProviderError

    if not prepared_inputs:
        return
    dialect = IMAGE_PROTOCOLS.get(protocol, IMAGE_PROTOCOLS[DEFAULT_PROTOCOL])
    if dialect == "inline_image_field":
        payload["image"] = (
            prepared_inputs if len(prepared_inputs) > 1 else prepared_inputs[0]
        )
        return
    raise ProviderError(
        f"当前图像模型的接入协议（{protocol}）不支持参考图；"
        "请改用支持参考图的图像模型，或取消参考图后重试",
        retryable=False,
        delivery_state="not_sent",
    )
