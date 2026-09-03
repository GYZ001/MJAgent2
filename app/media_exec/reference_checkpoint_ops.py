"""WS11：从 ``input_reference.py`` 搬出的纯 meta 操作，逐行搬移、未重写。

``input_reference.py`` 的 ``_prepare_reference_mode_inputs`` 已在
``app/FILE_CONVENTIONS.toml`` 登记为单一大函数基线（零余量：line_count 与
function_lines 两个维度都刚好卡在基线值），本次需要给它接一条新能力（造型-
时间锚点告警写进 meta，见 ``app.media_exec.reference_pool_gate``），腾不出
行数——把这个自成一体、只捕获 ``meta`` 一个外部状态的嵌套闭包搬出来，原地
留一个两行的转发壳（保持全部既有调用点不变），换出的行数刚好够新能力用。
"""
from __future__ import annotations

from app import video_modes


def apply_reference_checkpoint_invalidation(meta: dict, reason: str) -> None:
    """作废本次参考图检查点：清空画廊/关键帧相关字段，写明作废原因。

    逐字对齐搬移前 ``input_reference.py`` 里 ``_invalidate_reference_
    checkpoint`` 闭包的实现——不是重写，行为不得有任何变化。
    """
    meta["stale_reference_reason"] = reason
    meta["stale_keyframe_prompt_contract_version"] = meta.get("keyframe_prompt_contract_version")
    meta["keyframe_prompt_contract_version"] = video_modes.KEYFRAME_PROMPT_CONTRACT_VERSION
    meta["reference_input_policy_version"] = video_modes.REFERENCE_INPUT_POLICY_VERSION
    meta.pop("keyframe_contract_fingerprint", None)
    meta["reference_images"] = []
    meta["reference_slots"] = {}
    meta.pop("keyframe_sequence", None)
    meta["reference_manifest_frozen"] = False
    meta["reference_manifest_asset_stale"] = True
    meta["reference_generation_complete"] = False
    meta["reference_static_ready"] = False
    meta["continuity_anchor_ready"] = False
    meta["reference_group_gate_passed"] = False
    meta["video_input_manifest_frozen"] = False
    meta.pop("narrative_keyframe_missing", None)
    # 新画廊不得沿用旧 fingerprint/refset，否则 reference_store 会早返并指回旧图。
    for stale_key in (
        "reference_set_id", "reference_gallery_fingerprint", "reference_gallery_revision",
        "reference_gallery_source_version_id", "reference_gallery_edited",
        "reference_gallery_contract_override", "video_input_fingerprint",
    ):
        meta.pop(stale_key, None)


__all__ = ["apply_reference_checkpoint_invalidation"]
