"""叙事蓝图语义双审——独立审稿人 Prompt 构造。

从 ``blueprint_semantic_review.py`` 拆出：原来是 ``_semantic_review_narrative_
blueprint`` 单函数内联的一段约 80 行字面量文本拼接，本身不含任何分支逻辑，是纯
数据（Prompt 文案）而不是行为，独立成文件后编排函数不再被这段文本淹没。
"""
from __future__ import annotations

import json
from typing import Any


def _blueprint_semantic_review_prompt(
    *,
    node_reference_contract: dict[str, Any],
    source_reference_contract: dict[str, Any],
    projected_blueprint: dict[str, Any],
    projected_source: str,
    review_schema: dict[str, Any],
) -> str:
    return (
        "你是漫剧叙事蓝图的独立语义审稿人。只找会导致观众理解错误、"
        "人物瞬移、状态矛盾、因果跳跃或动机突变的可证实问题；不改稿，"
        "不评价题材或人物道德，不因为个人偏好要求美化原文。\n"
        "逐项检查：\n"
        "1. 回忆进入/退出、次日/当晚/数日后是否可识别，时间标签是否互相冲突；\n"
        "2. 人物、车辆、司机、行李、房间和关键物品的位置与行动是否闭环；\n"
        "3. 已建立的住宿、关系、知情状态等是否被后文无理由推翻，是否为了推进"
        "剧情临时发明满房、同谋、开放关系等便利条件；\n"
        "4. 重大决定是否有此前可见的压力、欲望和认知依据；\n"
        "5. 威胁、武器、醉酒或失去行动能力是否被错误改写为自主选择，约束解除"
        "是否真实发生；\n"
        "6. 后文引用的视觉事实是否此前真正给观众看见。\n"
        "7. 每个节点的三元叙事语义是否与来源职责一致：story 必须是可表演、"
        "可形成画面状态变化的故事语义；paratext 必须只做来源审计并使用"
        " connective+exclude_from_spine。不得按 SRC 编号、章节位置、人物是否"
        "为空或文本关键词判断，只能依据该段在叙事中的语义职责。\n"
        "8. 每个 projection=picture 的 quoted source unit 是否恰有一个"
        " source_unit_delivery；只有 spoken_dialogue/offscreen_voice 才能有"
        " usage=voice participant evidence，并通过 source_unit_keys 精确绑定"
        "且与 performer_key 一致；"
        "missing、多个 identity 或重复/冲突 claim 必须分别输出"
        " voice_identity_missing、voice_identity_ambiguous、"
        "voice_identity_conflict，不得拖到 SceneInput。quoted source unit 只以"
        "本轮来源合同 structured_source_units 中 projection=quoted 的机器事实"
        "为准；书页、信件、回忆引语、声音效果等非口播内容必须使用对应非声音"
        "delivery mode，不能为其伪造 speaker。story/picture中 projection=action "
        "的正文及 Blueprint 的 summary/"
        "action_logic 即使出现‘旁白’‘介绍’等自然语言，也不需要 voice，禁止"
        "将其提升为 dialogue 或要求伪造旁白 identity。\n"
        "9. 每个story/picture节点中 projection=action 的 prose source unit 必须拥有唯一"
        " exact-unit usage=state_subject evidence，或在 "
        "environment_source_unit_keys 中显式标记为纯环境。visible、"
        "scene roster、content_owner 不是主体证据；缺失、多主体或"
        "人物主体与环境标记冲突必须作为 must_fix 报告。"
        "若且仅若当前 environment_source_unit_keys 中的 action unit 在本轮"
        "完整语义中实际是人物的思考、反应、发问或动作，必须只输出"
        " code=state_subject_environment_misclassified；每条 issue 恰好引用"
        "一个 owning node，并在 source_unit_keys 中精确列出该 issue 涉及的"
        "全部 canonical exact units，在 source_segment_ids 中列出这些 units"
        "精确对应的 SRC。不得为真正的环境变化输出该 code，不得用文本关键词、"
        "姓名或内容列表判断。"
        "paratext/audit_only的quoted/action unit不适用delivery或state-subject要求，"
        "其所有剧情合同字段必须为空。\n"
        "连续剧可继承前序集已经建立的人物和关系；原文在当前节点明确揭示的"
        "既有关系，只要该节点先以可见/可听内容建立再引用，也不属于"
        " setup_missing。不得要求删除原文明确写出的关系来修复 setup。\n"
        "required_resolution 不得把无来源的便利设定伪装为原文事实；若只能通过"
        "改编补桥修复，必须明确要求 adaptation_kind=logic_bridge 及审计理由。"
        "每个问题必须引用本轮节点引用合同中的 canonical identity；node_keys"
        " 每项可直接使用 identity，或使用结构化 {\"ordinal\":正整数} /"
        " {\"identity\":\"canonical identity\"}。ordinal 从 1 开始，严格对应"
        " canonical_nodes 顺序。禁止根据文本相似度推断、拼接或改写 identity。"
        "发现确定问题后必须保留完整 issue；修正引用时不得删除该 issue。"
        "有直接原文依据时附 source_segment_ids。只输出 must_fix=true 的确定"
        "问题，禁止泛泛建议。"
        "\n\n本轮节点引用合同：\n"
        + json.dumps(
            node_reference_contract,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n本轮来源引用合同：\n"
        + json.dumps(
            source_reference_contract,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n蓝图：\n"
        + json.dumps(
            projected_blueprint,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n\n带稳定 ID 的原文：\n"
        + projected_source
        + "\n\n输出 Schema：\n"
        + json.dumps(
            review_schema,
            ensure_ascii=False,
        )
    )
