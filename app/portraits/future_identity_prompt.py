"""未来章节身份候选解析——Schema 与消歧 Prompt 构造。

从 ``future_identity_resolution.py`` 拆出：原来内联在
``resolve_future_identity_candidates`` 尾部的一段（把分组/权威/证据/决议目录
投影成模型输入，拼出带规则说明的 Prompt 文案），本身不含判定逻辑，是纯粹的
「组装模型输入」阶段。
"""
from __future__ import annotations

import json
from typing import Any

from .constants import IDENTITY_NAME_FORM_RULE
from .identity_schemas import _future_identity_schema, _identity_strict_response_format


def _future_identity_prompt(
    group_specs: list[dict],
    *,
    group_keys: list[str],
    authority_projection: list[dict],
    decision_by_id: dict[str, dict],
    decision_ids_by_group: dict[str, list[str]],
    evidence_by_id: dict[str, dict],
    evidence_ids_by_group: dict[str, list[str]],
    source_text: str,
    future_label: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict], list[dict], list[dict], str]:
    identity_schema = _future_identity_schema(
        group_keys,
        decision_ids_by_group=decision_ids_by_group,
        evidence_ids_by_group=evidence_ids_by_group,
    )
    identity_response_format = _identity_strict_response_format(
        identity_schema,
        name="screenplay_future_identity_resolution_v10",
    )
    group_projection = [
        {
            "group_key": group["group_key"],
            "identity_group": group["identity_group"],
            "source_labels": group["labels"],
        }
        for group in group_specs
    ]
    decision_projection = [
        decision for decision in decision_by_id.values()
    ]
    evidence_projection = [
        evidence for evidence in evidence_by_id.values()
    ]
    current_boundary = str(source_text or "").strip()[-700:]
    prompt = f"""任务：只为当前集尚未确认的身份组做后续姓名消歧。
未决身份组（group_key 是唯一可输出的键）：
{json.dumps(group_projection, ensure_ascii=False, separators=(',', ':'))}
已有人物权威目录（只读）：
{json.dumps(authority_projection, ensure_ascii=False, separators=(',', ':'))}
后续证据目录（{future_label or '后续章节'}；evidence_id 对应未改写的原文连续片段）：
{json.dumps(evidence_projection, ensure_ascii=False, separators=(',', ':'))}
可选决议目录（已将 group/authority/evidence 组合绑定为不透明 decision_id）：
{json.dumps(decision_projection, ensure_ascii=False, separators=(',', ':'))}
当前章末交接上下文（仅供推理，绝不是可选 evidence_id）：
{current_boundary or '（无）'}
规则：
1. decisions/revealed_names/reveal_evidence_ids/revealed_name_kinds 四个对象都必须精确输出全部
   group_key，不得增删键。
2. 证据不足时选 F: 决议，这是合法终态；此时两个侧载字段都必须是空字符串。
3. 证据显示该组就是「已有人物权威目录」中的人时，只能选对应那名 authority 的 K: 决议；
   该 token 已绑定 authority 与原文证据，两个侧载字段都必须为空；
   若目录里没有为该组与该 authority 列出 K: 决议，则只能选 F: 决议。
   K: 成立的判据是证据原文把这个称谓和那个人写成同一个人：同一个动作或同一段
   经历换着两种称呼来写、有人点破「原来他就是某某」、或此人自述。称谓与那个
   人的名字同时出现在一段话里不算数——两人对答、一方提起另一方、并列出场、
   一方是另一方的亲属或下属，都是两个人同时在场，这种情况选 F: 决议。
4. 只有证据目录首次逐字揭示了不在已有权威目录中的稳定真名，才能选 N: 决议；
   revealed_names 写真名，reveal_evidence_ids 必须选一个该组证据目录里真实存在、且包含该真名的 evidence_id（不得为空字符串——空字符串只在该组选 F: 时合法），
   revealed_name_kinds 写 personal_name。
   {IDENTITY_NAME_FORM_RULE}
   「某师姐」「某爷」「某掌柜」这类姓氏或关系加称呼是 honorific，不是真名：
   这种情况选 F: 决议，四个对象里除 decisions 外都写空字符串。
   非 N: 决议的组，revealed_names/reveal_evidence_ids/revealed_name_kinds 三项都必须是空字符串。
5. 不得回抄或改写证据文本，不得为已有权威重新签发新名，不得输出只在后续出场的人。
只输出符合下列 Schema 的 JSON：
{json.dumps(identity_schema, ensure_ascii=False, separators=(',', ':'))}"""
    return (
        identity_schema,
        identity_response_format,
        group_projection,
        decision_projection,
        evidence_projection,
        prompt,
    )
