"""文本 / 视觉理解的接入协议。

这几家都实现 OpenAI 兼容的 ``chat/completions``，差别只在少数几处：OpenRouter
用 ``reasoning.effort`` 表达推理强度，百炼要在同族模型间做回退，其余按标准协议
直发。所以这里不需要视频那样的完整适配器，只需要一个协议标识让分发选对分支。
"""
from __future__ import annotations

# 协议 → 展示名。键是存进模型库的 protocol 值。
TEXT_PROTOCOLS: dict[str, str] = {
    "openai": "OpenAI 兼容",
    "openrouter": "OpenRouter",
    "bailian": "阿里云百炼",
    "deepseek": "DeepSeek",
    "zhipu": "智谱",
}

DEFAULT_PROTOCOL = "openai"

# 旧的 provider 名 → 协议。迁移期用于把历史设置翻译成协议，
# 迁移完成后模型库条目自带 protocol，不再依赖这张表。
LEGACY_PROVIDER_PROTOCOLS: dict[str, str] = {
    "hiagent": "openai",
    "openrouter": "openrouter",
    "bailian": "bailian",
    "deepseek": "deepseek",
    "zhipu": "zhipu",
}


def protocol_for_provider(provider: str) -> str:
    """解析文本/视觉实例走哪套协议。"""
    from app import model_registry

    name = str(provider or "").strip()
    if name in LEGACY_PROVIDER_PROTOCOLS:
        return LEGACY_PROVIDER_PROTOCOLS[name]
    return model_registry.protocol_for_provider(
        name,
        allowed=TEXT_PROTOCOLS,
        default=DEFAULT_PROTOCOL,
    )
