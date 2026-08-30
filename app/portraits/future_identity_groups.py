"""未来章节身份候选解析——待决身份候选的筛选与分组。

从 ``future_identity_resolution.py`` 拆出两段原来内联在
``resolve_future_identity_candidates`` 顶部的逻辑：

* ``_future_identity_unresolved_candidates`` —— 从本集全部候选里挑出仍待未来
  文本消歧的那些（原函数的 ``unresolved_onscreen_groups``/``unresolved``）。
* ``_future_identity_group_specs`` —— 把待决候选按复合键
  ``(source_label, scope_qualifier)`` 分组、分配不透明 ``group_key``（原函数的
  ``raw_groups``/``label_to_group``/``group_specs``/``group_keys``）。
"""
from __future__ import annotations

from app.errors import ContentGenerationError

from .constants import CURRENT_IDENTITY_SYNTHETIC_PROVENANCE


def _future_identity_unresolved_candidates(
    candidates: list[dict],
    *,
    future_text: str,
) -> list[dict]:
    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if item.get("identity_kind") == "functional"
        and item.get("source_label_provenance")
        != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        and item.get("kind") == "onscreen"
        and str(item.get("identity_group") or "").strip()
    }
    return [
        dict(item) for item in candidates
        if item.get("identity_kind") == "functional"
        and item.get("source_label_provenance")
        != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        and (
            item.get("kind") == "onscreen"
            or str(item.get("identity_group") or "").strip()
            in unresolved_onscreen_groups
            or str(item.get("source_label") or "").strip() in future_text
        )
    ]


def _future_identity_group_specs(
    unresolved: list[dict],
) -> tuple[list[dict], list[str]]:
    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：这里原本按裸
    # source_label 键控（label_to_group: dict[str, str]），跟 _project_
    # current_identity_response 单批内的 (source_label, scope_qualifier)
    # 复合键判定不一致——上游合法放行的"两个外宗弟子"（不同 scope_
    # qualifier、不同 identity_group）流到这里，因为只看裸 label 又被判成
    # "同一称谓对应多个身份组"重新拦下。键升级为复合键；raw_group 的兜底
    # 生成也带上 qualifier，避免两个原本不同的人在都没有 identity_group 时
    # 被兜底成同一个 group（"label:外宗弟子"），把两个人的 candidates 揉
    # 到一起。
    raw_groups: dict[str, dict] = {}
    label_to_group: dict[tuple[str, str], str] = {}
    for candidate in unresolved:
        source_label = str(candidate.get("source_label") or "").strip()
        if not source_label:
            raise ContentGenerationError(
                "future identity candidate 缺少 source_label"
            )
        scope_qualifier = str(candidate.get("scope_qualifier") or "").strip()
        raw_group = str(candidate.get("identity_group") or "").strip()
        if not raw_group:
            raw_group = (
                f"label:{source_label}:{scope_qualifier}"
                if scope_qualifier else f"label:{source_label}"
            )
        previous_group = label_to_group.setdefault(
            (source_label, scope_qualifier), raw_group,
        )
        if previous_group != raw_group:
            raise ContentGenerationError(
                "future identity 同一称谓对应多个身份组："
                f"{source_label}"
            )
        group = raw_groups.setdefault(raw_group, {
            "identity_group": raw_group,
            "labels": [],
            "candidates": [],
        })
        if source_label not in group["labels"]:
            group["labels"].append(source_label)
        group["candidates"].append(candidate)

    group_specs: list[dict] = []
    for index, group in enumerate(raw_groups.values(), start=1):
        group_specs.append({
            **group,
            "group_key": f"G{index:03d}",
        })
    group_keys = [str(group["group_key"]) for group in group_specs]
    return group_specs, group_keys
