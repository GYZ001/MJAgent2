"""场景反应式发现的唯一模型调用编排（``assess_new_scene``）。

从 ``app.scenes`` 挪到这里：``app/scenes.py`` 行数已顶在
``app/FILE_CONVENTIONS.toml`` 的棘轮基线上（该基线只降不升，见该文件顶部
说明），新逻辑要放新模块，不能再往那个文件里加。``app.scenes`` 保留同名
再导出（``from app.production.scene_discovery_assess import assess_new_scene
as assess_new_scene``），既有调用方/测试用的 ``scenes.assess_new_scene``
引用（含 ``monkeypatch.setattr(scenes, "assess_new_scene", ...)``）不受影响。

新增职责——场景卡与道具卡的边界执行（《顾念长安》第1集真实缺陷：剧情道具
「小木星星」的外观细节被写进了「顾屿家客房」的 scene_canonical，场景定场图
因此把单颗星星画成一串花环）：``scene_canonical`` 一旦复述了本次映射同时
产出（或跨集已登记）的道具卡名称/别名，说明模型把剧情道具误写进了空间描述
——补一次纠正重试（至多两次模型调用），仍未清干净就放行但留下可见日志信号
（advisory，口径同 ``app.production.prep_pack.discovery._discover_new_props``
对映射台发布不阻断的既有做法）。不做无界重试——CLAUDE.md「修补器与校验器
死锁」的教训是双方反复否决彼此，这里只给一次机会，判据本身（结构字符串
包含检查）与生成不是同一个模型调用，不存在双方互相绕过对方意图的空间。
"""
from __future__ import annotations

import logging

from app.harness import model_gateway
from app.production.scene_evidence import candidate_block, structural_scene_candidates
from app.production.scene_granularity import (
    resolve_scene_granularity_verdict,
    scene_canonical_prop_leak,
    scene_granularity_prompt,
)
from app.refs import SCENE_CANONICAL_MAX_CHARS, SCENE_CANONICAL_MIN_CHARS
from app.scene_contract import SCENE_SAME_LOCATION_MATCH_RULE
from app.schemas import Scene, extract_json
from app.visual_styles import is_photographic_style_prompt

log = logging.getLogger(__name__)


def _prop_leak_retry_prompt(base_prompt: str, leaked: list[str]) -> str:
    names = "、".join(leaked)
    return (
        f"{base_prompt}\n\n上一次尝试写出的 scene_canonical 复述了本集另外建卡的"
        f"道具名称/别名（{names}）：场景卡只描述空间本身，这些物件各自已经有独立"
        "的道具参考图供分镜取用，不需要在场景描述里再复述它们的外观。请重写"
        "scene_canonical，去掉对它们的描述，只保留空间的布局、材质、固定陈设"
        "与光线基调。"
    )


async def _assess_scene_once(prompt: str, *, label: str, spatial_context: str, stage: str):
    raw = await model_gateway.chat(
        [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=600,
        call_meta={"stage": stage, "scene_label": label},
    )
    return resolve_scene_granularity_verdict(
        extract_json(raw), label=label, spatial_context=spatial_context,
        canonical_min=SCENE_CANONICAL_MIN_CHARS, canonical_max=SCENE_CANONICAL_MAX_CHARS,
    )


async def assess_new_scene(label: str, spatial_context: str, *, style: str,
                           known_scenes: list[Scene], ep_label: str,
                           known_prop_labels: list[str] = ()) -> dict:
    """把已确认剧本场次解析为新场景或已有场景别名，并产出粒度判定字段（location_key/
    role/era_anchor/anchor_phrase，判据见 app.production.scene_granularity）。
    ``known_prop_labels``：本次映射同时产出（含跨集已登记）的道具卡名称/别名，
    用于场景卡与道具卡的边界核验（见模块 docstring）。"""
    scene_canonical_style_rule = (
        f"必须贴合画风「{style}」，是照片级摄影质感的实景环境描述，允许并鼓励真实材质、自然光影与摄影级细节。"
        if is_photographic_style_prompt(style)
        else f"必须贴合画风「{style}」，是 CG/动画/漫画类非真人渲染场景，严禁真人实拍/实景照片描述。"
    )
    prompt = scene_granularity_prompt(
        label, spatial_context, style=style, style_rule=scene_canonical_style_rule,
        known_scenes=[(s.name, s.scene_canonical) for s in known_scenes],
        ep_label=ep_label, canonical_min=SCENE_CANONICAL_MIN_CHARS, canonical_max=SCENE_CANONICAL_MAX_CHARS,
        same_location_match_rule=SCENE_SAME_LOCATION_MATCH_RULE,
        candidates_block=candidate_block(structural_scene_candidates(label, known_scenes)),  # 字面互含的既有场景连同原文摘录做选择题
        known_prop_labels=list(known_prop_labels),
    )
    verdict = await _assess_scene_once(prompt, label=label, spatial_context=spatial_context, stage="assess_new_scene")
    leaked = scene_canonical_prop_leak(verdict.scene_canonical, known_prop_labels) if verdict.important else []
    if not leaked:
        return verdict.as_dict()
    retry_verdict = await _assess_scene_once(
        _prop_leak_retry_prompt(prompt, leaked), label=label, spatial_context=spatial_context,
        stage="assess_new_scene_prop_leak_retry",
    )
    still_leaked = (
        scene_canonical_prop_leak(retry_verdict.scene_canonical, known_prop_labels)
        if retry_verdict.important else []
    )
    if still_leaked:
        log.warning(
            "场景「%s」scene_canonical 重试后仍复述道具卡名称/别名 %s，按既有软检查口径"
            "放行，建议人工核对场景描述是否混入了道具外观", label, still_leaked,
        )
    return retry_verdict.as_dict()
