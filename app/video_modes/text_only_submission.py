"""REFERENCE_IMAGE_MODE 没有参考图时的唯一裁决点。

门禁（app/media_exec/reference_pool_gate.py）裁定候选池本来就是空的——纯群演镜：人物
不在人物谱、场景不在场景库——会把外观描述写进 prompt（TEXT_ONLY_FALLBACK_NOTE_MARKER）
并标 ``reference_mode_text_only_fallback=True``。这种镜没有图可装，按纯文本出片；此外
任何"没有参考图"都是提交前的真实缺口，必须拦下。373a7c7 只加了门禁侧回退没改打包侧，
两条规则打架让一镜重试 5 次全部卡成待人工（2026-09-03 三国 ep1 第 1 镜）。
"""
from __future__ import annotations

from typing import Any

from app.hiagent import ProviderError


def empty_reference_submission(meta: dict[str, Any]) -> list[tuple[str, str]]:
    """门禁裁定的纯文本回退返回空输入；否则按原规则拒绝纯文本提交。"""
    if meta.get("reference_mode_text_only_fallback") is True:
        return []
    raise ProviderError("REFERENCE_IMAGE_MODE 缺少通过门禁的 reference_image，禁止纯文本提交")
