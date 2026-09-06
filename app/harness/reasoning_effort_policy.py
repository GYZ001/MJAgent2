"""按调用阶段给思考型模型定思考档位（2026-09-06 用户：映射台单集 30+ 分钟，找根因）。

B 库三小时实测（provider_calls，status=OK）：映射台一集 24-32 次串行模型调用，供应商忙时只占
总时长的 1/3，其余在排队；而每次调用本身 17-36 秒、思考 1.7k-3.4k token——判「这个称谓是不是
新角色」「这个地点是不是已有场景」这类短 JSON 判定，思考量与写整段分镜（4.9k）一个量级。
30 集 × 25 次 × 24 秒挤 10 来个槽位 = 30 分钟，这就是根因。

档位是模型的开放集合（智谱 low/high/max，火山 seed minimal/low/medium/high），这里只按阶段
表态：短 JSON 判定用 low，写作类（分镜段落、节拍表、事件链）不表态沿用模型默认。优先级：
call_meta 显式 > 运维全局覆盖（settings.text_reasoning_effort）> 本表 > config 默认。

判据来源与验证口径：``scripts/inspect_mapping_stage.py`` 按阶段打印 p50 延迟与思考 token，
改档后对比同一张表；人物谱/场景库/物件库核查（映射台结束后必跑）盯质量回退。
"""
from __future__ import annotations

LOW_EFFORT_STAGES: frozenset[str] = frozenset({
    # 人物发现与身份判定（映射台里最多、最贵的一批）
    "discover_character_candidates", "screenplay_character_discovery", "assess_new_character",
    "screen_appearance_changes", "portraits_timeline_anchor", "portraits_card_merge_verdict",
    "未解析角色候选判别", "叙述向称谓归属", "episode_prep_pack_appellation_resolution",
    # 场景 / 道具判定
    "assess_new_scene", "assess_prop_appearance", "assess_new_scene_state",
    # 生成台的模式规划（选择题）
    "episode_video_mode_plan",
})
LOW_EFFORT = "low"
# 2026-09-06 实测：普通 chat 判定 low 把延迟/思考砍半（assess_new_character 17.8s→9.2s），但工具对话
# （身份调查 Phase A）带 low 后 p50 27.7s/思考 3.4k 几乎不变——需要 minimal（火山 seed 实测 minimal
# 才把 reasoning_tokens 压到 0）的子阶段登记在这里；子阶段键先于阶段键匹配。默认空：切之前要用
# 人物谱核查盯质量。
MINIMAL_EFFORT_SUBSTAGES: frozenset[str] = frozenset()
MINIMAL_EFFORT = "minimal"


def stage_reasoning_effort(call_meta: dict | None) -> str:
    """该调用按阶段应当使用的思考档位；不在表里返回空串（由调用方回落到默认）。"""
    meta = call_meta or {}
    substage = str(meta.get("substage") or "").strip()
    if substage and substage in MINIMAL_EFFORT_SUBSTAGES:
        return MINIMAL_EFFORT
    for key in ("stage", "stage_key", "purpose", "call_role_label"):
        value = str(meta.get(key) or "").strip()
        if value and value in LOW_EFFORT_STAGES:
            return LOW_EFFORT
    return ""
