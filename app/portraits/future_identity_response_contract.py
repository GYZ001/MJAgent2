"""未来章节身份候选解析——结构化响应的归一改写与硬校验。

从 ``future_identity_resolution.py`` 拆出两段原来内联在
``resolve_future_identity_candidates`` 里、作为 ``_identity_structured_with_
resample`` 回调传入的闭包（``normalize_identity_payload``/``validate_
response``），提升为顶层函数。两者都只读同一批"决议目录构造阶段"产出的只读
数据（``group_keys``/``authority_by_id``/``evidence_ids_by_group``/
``evidence_by_id``/``named_authorities_by_identity_group``/``group_specs``/
``future_text``），打包进 ``_FutureIdentityResolutionContext`` 按值传递。

``decision_by_id`` 是这批只读数据里唯一的例外：``normalize_identity_payload``
会在其中原地新增归一决议（``decision_by_id[reissue_id] = {...}``）——这是字典
项赋值（mutate），不是重新绑定整个字典，调用方持有的同一个对象立刻可见，行为
与原来的闭包捕获完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    IDENTITY_NAME_FORM_PERSONAL,
    IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH,
    REISSUE_KNOWN_RESOLUTION_KIND,
)
from ._identity_tokens import _identity_source_label_has_list_separator
from .identity_schemas import FutureIdentityCandidateResponse


@dataclass
class _FutureIdentityResolutionContext:
    """一次 ``resolve_future_identity_candidates`` 调用共享的只读判定数据。"""

    group_specs: list[dict]
    group_keys: list[str]
    decision_by_id: dict[str, dict]
    evidence_by_id: dict[str, dict]
    evidence_ids_by_group: dict[str, list[str]]
    authority_by_id: dict[str, dict]
    named_authorities_by_identity_group: dict[str, set[str]]
    future_text: str


def _normalize_future_identity_payload(
    payload: dict,
    context: _FutureIdentityResolutionContext,
) -> dict:
    """Route a NEW answer to the decision its own evidence supports.

    Two deterministic rewrites, both before validation:

    * a NEW whose declared form is not a personal name is demoted to this
      group's functional decision -- 真名 > 尊称 > 代称, and only a real
      name may mint a new authority; and

    * a NEW that actually names an already-registered person is rewritten
      onto that person's own backend decision.

    The provider sometimes expresses "this group is that already-registered
    person" with the N token plus that person's existing canonical name.
    When the backend has itself already minted a K decision for the exact
    same (group, authority, evidence) tuple, the answer carries every fact
    the K decision requires and differs only in which token was written --
    so it is canonicalised onto the backend's own token instead of failing
    the episode.  Without a matching backend decision nothing is rewritten
    and the NEW rule stays fail-closed: this can never bind an authority
    the backend has not already anchored in this group's own evidence.
    """
    decision_by_id = context.decision_by_id
    authority_by_id = context.authority_by_id
    future_text = context.future_text
    if not isinstance(payload, dict):
        return payload
    decisions = payload.get("decisions")
    revealed_names = payload.get("revealed_names")
    reveal_evidence_ids = payload.get("reveal_evidence_ids")
    if not (
        isinstance(decisions, dict)
        and isinstance(revealed_names, dict)
        and isinstance(reveal_evidence_ids, dict)
    ):
        return payload
    name_kinds = payload.get("revealed_name_kinds")
    if not isinstance(name_kinds, dict):
        name_kinds = {}
    rewritten: dict[str, str] = {}
    for group_key in context.group_keys:
        selected = decision_by_id.get(str(decisions.get(group_key) or ""))
        if (
            selected is None
            or str(selected.get("resolution_kind") or "") != "new_named"
        ):
            continue
        if str(
            name_kinds.get(group_key) or ""
        ) != IDENTITY_NAME_FORM_PERSONAL:
            # 真名 > 尊称 > 代称：只有真名可以签发新的人物权威。尊称与代称
            # （以及没有明确声明形态的情况）确定性降级为功能身份，本组仍然是
            # 一个独立身份，等真名出现在证据里再由 K 决议认领同一个人。
            functional_id = f"F:{group_key}"
            if functional_id in decision_by_id:
                rewritten[group_key] = functional_id
            continue
        canonical_name = str(
            revealed_names.get(group_key) or ""
        ).strip()
        evidence_id = str(
            reveal_evidence_ids.get(group_key) or ""
        ).strip()
        if not canonical_name or not evidence_id:
            continue
        # An ambiguous name matches no single authority: fail closed.
        authority_ids = [
            authority_id
            for authority_id, authority in authority_by_id.items()
            if str(
                authority.get("canonical_name") or ""
            ).strip() == canonical_name
        ]
        if len(authority_ids) != 1:
            continue
        known = next(
            (
                decision for decision in decision_by_id.values()
                if str(decision.get("group_key") or "") == group_key
                and str(
                    decision.get("resolution_kind") or ""
                ) == "known_named"
                and str(
                    decision.get("authority_id") or ""
                ) == authority_ids[0]
                and evidence_id in (decision.get("evidence_ids") or [])
            ),
            None,
        )
        if known is not None:
            rewritten[group_key] = str(known["decision_id"])
            continue
        # 归一规则（真实第26轮 EP5 回归 ERR-20260824-88ece5）：门禁立意
        # （防重复铸造身份）本身没错，错的是对"多报"的响应形态。
        # authority_ids 唯一命中只证明"这个真名字符串对应项目里唯一
        # 一个已有身份"，还不足以证明"这个 group 真的是那个人"——两个
        # 回归夹具（"三哥"被声称是"陈三"、"小胖子"被声称是"李富贵"，
        # 但各自的 future_text 里那个真名压根不存在，是纯粹的臆断/
        # 嫁接昵称）证明 authority_ids 唯一命中单独作为归一条件太松，
        # 必须补一条真正的"确定性一致性比对"：真名整体是否至少在这个
        # 组能看到的完整 future_text 里逐字出现过——不要求命中后端
        # 预先按 proof_anchors 筛出的那个更窄的 evidence 子集（那正是
        # 要豁免的负担：已知身份的锚点在它初次签发时已经验过，不需要
        # 精确复现是哪一条 evidence_id 命中的），只要求这个名字本身
        # 真的出现在模型能看到的原文里，不是凭一个昵称/称谓单方面嫁接。
        # 按门禁不对称教义（缺失致命、冗余归一、矛盾致命）：
        #   - authority_ids 唯一命中 + 真名整体逐字出现在 future_text
        #     = 冗余，归一为对既有身份的引用（见 REISSUE_KNOWN_
        #     RESOLUTION_KIND 分支在 validate_response/response_
        #     decisions 里的处理）；
        #   - authority_ids 命中多个（len!=1，上面已经 continue 掉）
        #     = 矛盾，同一个真名字符串被项目内不同的 authority_id
        #     分别持有，无法确定性判断该并入哪一个；
        #   - authority_ids 唯一命中但真名整体不在 future_text 里
        #     = 同样按矛盾/不相容处理，维持原始 NEW 校验路径不动
        #     （不重写，交给下面 validate_response 的既有 NEW 规则
        #     去拦——它本就会因为"不得重新签发已有 authority"报错）。
        if canonical_name not in future_text:
            continue
        reissue_id = f"{REISSUE_KNOWN_RESOLUTION_KIND}:{group_key}:{authority_ids[0]}"
        if reissue_id not in decision_by_id:
            reissue_authority = authority_by_id.get(authority_ids[0], {})
            decision_by_id[reissue_id] = {
                "decision_id": reissue_id,
                "group_key": group_key,
                "resolution_kind": REISSUE_KNOWN_RESOLUTION_KIND,
                "authority_id": authority_ids[0],
                "canonical_name": canonical_name,
                "materialization_compatible": bool(
                    reissue_authority.get("materialization_compatible")
                ),
            }
        rewritten[group_key] = reissue_id
    if not rewritten:
        return payload
    return {
        **payload,
        "decisions": {**decisions, **rewritten},
        "revealed_names": {
            **revealed_names,
            **{group_key: "" for group_key in rewritten},
        },
        "reveal_evidence_ids": {
            **reveal_evidence_ids,
            **{group_key: "" for group_key in rewritten},
        },
        "revealed_name_kinds": {
            **name_kinds,
            **{group_key: "" for group_key in rewritten},
        },
    }


def _validate_future_identity_response(
    value: FutureIdentityCandidateResponse,
    context: _FutureIdentityResolutionContext,
) -> list[str]:
    group_keys = context.group_keys
    decision_by_id = context.decision_by_id
    authority_by_id = context.authority_by_id
    evidence_ids_by_group = context.evidence_ids_by_group
    evidence_by_id = context.evidence_by_id
    named_authorities_by_identity_group = context.named_authorities_by_identity_group
    group_specs = context.group_specs
    future_text = context.future_text

    errors: list[str] = []
    expected_keys = set(group_keys)
    maps = {
        "decisions": value.decisions,
        "revealed_names": value.revealed_names,
        "reveal_evidence_ids": value.reveal_evidence_ids,
        "revealed_name_kinds": value.revealed_name_kinds,
    }
    for field_name, values in maps.items():
        actual_keys = set(values)
        if actual_keys != expected_keys:
            errors.append(
                f"future identity {field_name} keys 不闭合"
            )
    existing_identity_names = {
        name
        for authority in authority_by_id.values()
        for name in (
            str(authority.get("canonical_name") or ""),
            *[
                str(value)
                for value in authority.get("aliases") or []
            ],
        )
        if name
    }
    for group_key in group_keys:
        selected_id = str(value.decisions.get(group_key) or "")
        selected = decision_by_id.get(selected_id)
        if (
            selected is None
            or str(selected.get("group_key") or "") != group_key
        ):
            errors.append(
                f"future identity decision_id 越界：{group_key}"
            )
            continue
        canonical_name = str(
            value.revealed_names.get(group_key) or ""
        )
        evidence_id = str(
            value.reveal_evidence_ids.get(group_key) or ""
        )
        resolution_kind = str(selected.get("resolution_kind") or "")
        declared_form = str(
            value.revealed_name_kinds.get(group_key) or ""
        )
        if resolution_kind != "new_named":
            if canonical_name or evidence_id or declared_form:
                errors.append(
                    "future identity 非 NEW 决议侧载必须为空："
                    f"{group_key}"
                )
            if resolution_kind == "known_named":
                authority = authority_by_id.get(
                    str(selected.get("authority_id") or "")
                )
                selected_evidence_ids = [
                    str(value)
                    for value in selected.get("evidence_ids") or []
                ]
                proof_kind = str(selected.get("proof_kind") or "")
                proof_anchors = [
                    str(value)
                    for value in selected.get("proof_anchors") or []
                    if str(value)
                ]
                same_group_authority = str(
                    selected.get("authority_id") or ""
                ) in named_authorities_by_identity_group.get(
                    str(
                        next(
                            (
                                group.get("identity_group")
                                for group in group_specs
                                if group.get("group_key") == group_key
                            ),
                            "",
                        )
                    ),
                    set(),
                )
                if (
                    authority is None
                    or proof_kind not in {
                        "registered_alias",
                        "canonical_name",
                        "same_group_authority",
                    }
                    or (
                        proof_kind == "same_group_authority"
                        and not same_group_authority
                    )
                    or not proof_anchors
                    or not selected_evidence_ids
                    or any(
                        value not in evidence_ids_by_group.get(
                            group_key, []
                        )
                        for value in selected_evidence_ids
                    )
                    or not any(
                        anchor
                        and anchor in str(
                            evidence_by_id.get(value, {}).get("text")
                            or ""
                        )
                        for value in selected_evidence_ids
                        for anchor in proof_anchors
                    )
                ):
                    errors.append(
                        "future identity known 缺少后端登记的权威锚点："
                        f"{group_key}"
                    )
            elif resolution_kind == REISSUE_KNOWN_RESOLUTION_KIND:
                # 归一分支（第26轮 ERR-20260824-88ece5，见
                # normalize_identity_payload 上方完整说明）：已知身份
                # 不需要重新锚定真名——锚点在它初次签发时已经验过。
                # 只做最基本的健全性检查：authority_id 必须真的存在于
                # 权威目录（防止归一逻辑自身出 bug 生造一个不存在的
                # authority_id），不重新要求 proof_anchors/evidence_id
                # 命中——那正是这条归一规则要豁免的负担。
                if str(selected.get("authority_id") or "") not in authority_by_id:
                    errors.append(
                        "future identity reissue 指向不存在的 authority："
                        f"{group_key}"
                    )
            continue
        errors.extend(_future_identity_new_named_errors(
            group_key,
            canonical_name=canonical_name,
            evidence_id=evidence_id,
            declared_form=declared_form,
            existing_identity_names=existing_identity_names,
            evidence_ids_by_group=evidence_ids_by_group,
            evidence_by_id=evidence_by_id,
            future_text=future_text,
        ))
    return errors


def _future_identity_new_named_errors(
    group_key: str,
    *,
    canonical_name: str,
    evidence_id: str,
    declared_form: str,
    existing_identity_names: set[str],
    evidence_ids_by_group: dict[str, list[str]],
    evidence_by_id: dict[str, dict],
    future_text: str,
) -> list[str]:
    """校验一个被选中 N: 决议的 group：真名形态与逐字锚点的全部判据。

    从 ``_validate_future_identity_response`` 拆出（棘轮基线只降不升，见
    ``FILE_CONVENTIONS.toml``），本身不含新判据，只是把原来内联在主循环
    尾部、只在 ``resolution_kind == "new_named"`` 时才执行的一段独立校验
    提升为具名函数。
    """
    errors: list[str] = []
    if canonical_name != canonical_name.strip() or not canonical_name:
        errors.append(f"future identity NEW 真名无效：{group_key}")
    if len(canonical_name) > IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH:
        errors.append(f"future identity NEW 真名过长：{group_key}")
    if _identity_source_label_has_list_separator(canonical_name):
        errors.append(
            f"future identity NEW 真名不得包含身份列表分隔符：{group_key}"
        )
    if declared_form != IDENTITY_NAME_FORM_PERSONAL:
        errors.append(
            "future identity NEW 只能签发真名（真名 > 尊称 > 代称）："
            f"{group_key}"
        )
    if canonical_name in existing_identity_names:
        # 真实事故 ERR-20260831-45404d（EP1 run_c14d8e02d220）：这条本身是
        # 正确的 fail-closed（同一个真名不得被两次签发新 authority），但
        # 原始报错没说清楚正确的修复入口在哪——真正该做的合并动作属于
        # CURRENT 身份识别阶段的 K 决议 absorbed_functional_keys（见
        # identity_schemas.CurrentKnownIdentityDecision），不是这里能做
        # 的事，FUTURE 阶段对这个 group 只能改选 F: 决议占位。
        errors.append(
            "future identity NEW 不得重新签发已有 authority："
            f"{group_key} 真名={canonical_name!r} 已在本集权威目录中登记；"
            "同一个人的其它称谓要在 CURRENT 身份识别阶段用 K 决议的 "
            "absorbed_functional_keys 吸收，这里只能改选 F: 决议"
        )
    if evidence_id not in evidence_ids_by_group.get(group_key, []):
        # 真实事故 ERR-20260831-45404d：provider 严格模式下 "" 对
        # reveal_evidence_ids 始终是 schema 合法值（见
        # identity_schemas._future_identity_schema 里 reveal_evidence_ids
        # 的注释），选 N: 却把它留空正是那次事故的直接触发点——分清"为空"
        # 与"填了但不在本组证据目录里"，帮下一个读到这条报错的人一眼看出
        # 是哪一种。
        reason = "为空" if not evidence_id else "越界"
        errors.append(
            f"future identity NEW evidence_id {reason}：{group_key}"
            "（选 N: 必须填一个该组证据目录里真实存在的 evidence_id，"
            "空字符串只在选 F: 时合法）"
        )
        return errors
    evidence = evidence_by_id.get(evidence_id, {})
    evidence_text = str(evidence.get("text") or "")
    if (
        evidence.get("origin") != "future"
        or evidence_text not in future_text
        or canonical_name not in evidence_text
    ):
        errors.append(f"future identity NEW 缺少逐字真名锚点：{group_key}")
    return errors
