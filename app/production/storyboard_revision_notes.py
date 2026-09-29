"""人工复核在片段身份复核面板写下的单段修订意见（2026-09-29）——独立叶子模块，
供 ``storyboard_pack._generate_all_segment_prompts`` 在只重生成一段时把意见转成
这一段专属的正面陈述规则，不广播给其余段落。

只服务单段重生成路径（``storyboard_identity_regenerate.regenerate_identity_
candidate``）：该路径把除目标段以外的全部段落放进 ``reuse_segments``，
``_generate_all_segment_prompts`` 对 reuse 命中的段落直接 continue、不进入
下面构造 ``task_payload["rules"]`` 的分支（见该函数循环体）。因此
``revision_notes`` 只要是一个不分段号的裸字符串，天然只会被拼进真正发起模型
调用的那一段——不需要 ``{segment_no: notes}`` 这种映射结构。整集生成
（``generate_storyboard_pack``）从不传这个参数，用默认空串，行为不变。
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
