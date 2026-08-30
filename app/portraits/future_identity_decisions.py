"""未来章节身份候选解析——铸造每个分组可选的 F:/K:/N: 决议目录。

从 ``future_identity_resolution.py`` 拆出：原来内联在
``resolve_future_identity_candidates`` 里的决议铸造循环，把「已有证据窗口」
兑现成模型可以选择的不透明 decision_id（F: 证据不足、K: 就是某个已登记权威、
N: 首次揭示新真名），并强制要求 K: 决议必须有本组证据窗口里的逐字锚点撑腰。
"""
from __future__ import annotations

from app.evidence import repository as evidence_repository

from .constants import FUTURE_IDENTITY_DECISION_VERSION


def _future_identity_decision_catalog(
    group_specs: list[dict],
    *,
    fallback_evidence_group_keys: set[str],
    authority_by_id: dict[str, dict],
    named_authorities_by_identity_group: dict[str, set[str]],
    episode_named_authorities: set[str],
    evidence_ids_by_group: dict[str, list[str]],
    evidence_by_id: dict[str, dict],
) -> tuple[dict[str, dict], dict[str, list[str]]]:
    decision_by_id: dict[str, dict] = {}
    decision_ids_by_group: dict[str, list[str]] = {}
    for group in group_specs:
        group_key = str(group["group_key"])
        functional_id = f"F:{group_key}"
        decision_by_id[functional_id] = {
            "decision_id": functional_id,
            "group_key": group_key,
            "resolution_kind": "functional",
        }
        decision_ids = [functional_id]
        # 兜底证据不得为 K 决议背书（见上面 fallback_evidence_group_keys 的
        # 注释）：这个组的证据窗口和它的标签毫无逐字关联，窗口里出现的任何
        # 已登记权威的别名/真名都只是巧合共现，不是"这个组就是那个人"的
        # 证据。可选项在这里被硬性收窄到只剩 F:（证据不足）与下面的 N:
        # （若窗口内确实首次揭示了新真名）——不做成"允许但弱置信度标注"，
        # 因为一旦选项出现在 schema 枚举里，模型就可能选中它，且后续任何
        # 环节都无法再用"这是不是兜底窗口"这条信息去否决一个已经铸造出的
        # decision_id。
        group_label_texts = [
            str(value).strip()
            for value in group.get("labels") or []
            if str(value).strip()
        ]
        if group_key not in fallback_evidence_group_keys:
            for authority_id, authority in authority_by_id.items():
                canonical_name = str(
                    authority.get("canonical_name") or ""
                ).strip()
                # K decisions need an anchor the backend can bind to this
                # group's own evidence: a registered non-canonical alias, an
                # authority already bound to this exact current identity
                # group, or -- for an authority not otherwise present in this
                # episode -- its canonical name.  Excluding the canonical name
                # outright was the production defect: every Bible-seeded
                # authority starts with an empty alias list, so no K decision
                # was ever minted, "this group is an already-registered
                # person" became unrepresentable, and the run died on rule 5
                # instead.
                registered_aliases = [
                    str(value).strip()
                    for value in authority.get("aliases") or []
                    if str(value).strip()
                    and str(value).strip() != canonical_name
                ]
                same_group_authority = authority_id in (
                    named_authorities_by_identity_group.get(
                        str(group.get("identity_group") or ""), set()
                    )
                )
                if same_group_authority:
                    proof_anchors = [
                        str(value) for value in group.get("labels") or []
                    ]
                    proof_kind = "same_group_authority"
                else:
                    canonical_anchor = (
                        [canonical_name]
                        if canonical_name
                        and authority_id not in episode_named_authorities
                        else []
                    )
                    proof_anchors = list(dict.fromkeys([
                        *registered_aliases,
                        *canonical_anchor,
                    ]))
                    proof_kind = (
                        "registered_alias" if registered_aliases
                        else "canonical_name"
                    )
                if not proof_anchors:
                    continue
                # 锚定窗口必须同时含有本组自己的标签。这个组的证据目录并不只
                # 装"提到本组标签"的窗口：上面选窗时还把"提到任一已登记真名"
                # 的窗口一并收了进来（供 N: 分支看新名字）。那些窗口跟本组标签
                # 毫无逐字关联，窗口里出现的权威名字只是巧合共现。
                #
                # 真实故障 ERR-20260828-e65955（《罗刹海市》EP1）：G001 的标签是
                # 「父亲」，铸出 K:G001:bible:马骥 的那扇窗口是"三天之内，马骥
                # 遍游各处海国。从此，「龙媒」的名声传遍四海"——整扇窗口没有
                # 「父亲」二字，只因含有马骥的本集别名「龙媒」就成了锚点。模型
                # 选中它，「父亲」就此成为马骥的已登记称谓；同一次运行的后一批
                # current 识别正确地把「父亲」判成 functional，撞上「不得冒用已
                # 登记身份称谓」，整集失败且重试必然复现。
                #
                # fallback_evidence_group_keys 拦的是同一件事，但它的判据挂在
                # 「整个未来文本里出没出现过这个标签」这个组级信号上：标签只要
                # 在别处出现过一次，全组的窗口就都成了合法锚点。判据下沉到每扇
                # 窗口自己。same_group_authority 分支的 proof_anchors 本来就是
                # 这批标签，这条要求对它恒真。
                anchored_evidence_ids = []
                for evidence_id in evidence_ids_by_group[group_key]:
                    window_text = str(
                        evidence_by_id.get(evidence_id, {}).get("text") or ""
                    )
                    if not any(
                        anchor and anchor in window_text
                        for anchor in proof_anchors
                    ):
                        continue
                    if not any(
                        label in window_text for label in group_label_texts
                    ):
                        continue
                    anchored_evidence_ids.append(evidence_id)
                if not anchored_evidence_ids:
                    continue
                known_hash = evidence_repository.content_hash({
                    "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                    "group_key": group_key,
                    "authority_id": authority_id,
                    "evidence_ids": anchored_evidence_ids,
                })[:12]
                known_id = f"K:{group_key}:{authority_id}:{known_hash}"
                decision_by_id[known_id] = {
                    "decision_id": known_id,
                    "group_key": group_key,
                    "resolution_kind": "known_named",
                    "authority_id": authority_id,
                    "canonical_name": canonical_name,
                    "evidence_ids": anchored_evidence_ids,
                    "materialization_compatible": bool(
                        authority.get("materialization_compatible")
                    ),
                    "proof_kind": proof_kind,
                    "proof_anchors": proof_anchors,
                }
                decision_ids.append(known_id)
        if evidence_ids_by_group[group_key]:
            new_id = f"N:{group_key}"
            decision_by_id[new_id] = {
                "decision_id": new_id,
                "group_key": group_key,
                "resolution_kind": "new_named",
            }
            decision_ids.append(new_id)
        decision_ids_by_group[group_key] = decision_ids
    return decision_by_id, decision_ids_by_group
