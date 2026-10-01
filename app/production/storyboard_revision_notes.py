"""人工复核在片段身份复核面板写下的单段修订意见（2026-09-29）——独立叶子模块，
供 ``storyboard_pack._generate_all_segment_prompts`` 在只重生成一段时把意见转成
这一段专属的正面陈述规则，不广播给其余段落。

两处消费方，同一个函数：① 单段重生成路径（``storyboard_identity_regenerate.
regenerate_identity_candidate``）——该路径把除目标段以外的全部段落放进
``reuse_segments``，``_generate_all_segment_prompts`` 对 reuse 命中的段落直接
continue、不进入下面构造 ``task_payload["rules"]`` 的分支（见该函数循环体），
因此函数参数 ``revision_notes`` 只要是一个不分段号的裸字符串，天然只会被拼进
真正发起模型调用的那一段，不需要 ``{segment_no: notes}`` 这种映射结构；
② 2026-10-01 边写边审的逐段循环内重写（``storyboard_prose_review.review_
segment_inline`` 判定有已核验违规后返回的意见文本）——这条路径不经过函数参数
``revision_notes``，而是循环体内局部变量 ``revision_text``（每段开始时重置为
``revision_notes``，复核判定需要重写时覆盖成复核意见），两者共用同一句拼接
逻辑、不新造第二套。``generate_storyboard_pack`` 的整集生成调用仍然从不传函数
参数 ``revision_notes``（用默认空串），①②两条路径互不冲突。
"""
from __future__ import annotations


def segment_rule_text(notes: str) -> list[str]:
    """空串/纯空白不产出规则（今天的默认行为不变）；非空时转成一条完整正面
    陈述，要求逐条落实、原文优先、缺口写进 degraded_capabilities——不是禁令，
    是把「怎么处理冲突」「没采纳时怎么留痕」都交代清楚（CLAUDE.md「写完整的
    正面陈述，不写禁令」）。"""
    cleaned = notes.strip()
    if not cleaned:
        return []
    return [
        "本段修订意见（人工复核提出，逐条落实到本段画面与动作里；与原文冲突时以原文为准，"
        f"并在 degraded_capabilities 里写明哪条没有采纳、为什么）：{cleaned}"
    ]
