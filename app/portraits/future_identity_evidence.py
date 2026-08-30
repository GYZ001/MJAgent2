"""未来章节身份候选解析——按分组切分未来原文证据窗口。

从 ``future_identity_resolution.py`` 拆出：原来内联在
``resolve_future_identity_candidates`` 里的一段（把 ``future_text`` 切成
120 字重叠窗口，再给每个待决分组挑选它自己的证据窗口子集），是一个自成一体的
「证据检索」阶段，只读 ``group_specs``/``future_text``/``known_names``，不依赖
后面的权威判定与决议铸造。
"""
from __future__ import annotations

from app.evidence import repository as evidence_repository
from app.source_excerpt import SourceSegment

from .constants import CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET, FUTURE_IDENTITY_DECISION_VERSION


def _future_identity_evidence_windows(
    group_specs: list[dict],
    *,
    future_text: str,
    known_names: list[str],
) -> tuple[dict[str, dict], dict[str, list[str]], set[str]]:
    # Evidence IDs always resolve to an exact raw-future span.  Current-tail
    # context is shown separately for semantic handoff, but can never be cited
    # as the owned evidence which authorizes a decision.
    # Use one overlap policy across the complete raw future source.  Applying
    # overlap only inside a long balanced quotation leaves ordinary 120-char
    # segment boundaries able to split a <=16-char name.  A 32-char overlap
    # guarantees every allowed label/name is complete in at least one window.
    future_segments = [
        SourceSegment(
            segment_id=f"FUTURE:E{index + 1}",
            text=future_text[offset:offset + 120],
            start_offset=offset,
            end_offset=min(len(future_text), offset + 120),
        )
        for index, offset in enumerate(range(0, len(future_text), 88))
        if future_text[offset:offset + 120]
    ]
    evidence_by_id: dict[str, dict] = {}
    evidence_ids_by_group: dict[str, list[str]] = {}
    # 事故 RCA（EP2「绿袍男子」误并入「李富贵」）：当某个标签在整段未来文本
    # 里从未逐字出现，下面的 else 分支盲抓未来文本开头约 900 字符作为该组
    # 的证据窗口，内容与该标签毫无关系——纯属兜底，只是为了让 N: 分支（发现
    # 新真名）仍有文本可看。这样取得的窗口绝不能被当成"这就是该标签的身份
    # 证据"去背书任何 K: 决议：窗口里偶然出现的任何已登记角色别名/真名都只
    # 是巧合共现，不是该标签与那个角色同一人的证据。用这个集合记录哪些组是
    # 纯兜底取得证据，供下面铸造决议时拒绝为它们产出 K: 选项。
    fallback_evidence_group_keys: set[str] = set()
    per_group_budget = min(
        1800,
        max(
            120,
            CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET // max(1, len(group_specs)),
        ),
    )
    for group in group_specs:
        group_key = str(group["group_key"])
        group_labels = [str(value) for value in group["labels"]]
        label_source_indexes = {
            index for index, segment in enumerate(future_segments)
            if any(label in segment.text for label in group_labels)
        }
        if label_source_indexes:
            context_source_indexes = {
                neighbor
                for index in label_source_indexes
                for neighbor in (index - 1, index, index + 1)
                if 0 <= neighbor < len(future_segments)
            }
            context_source_indexes.add(len(future_segments) - 1)
            context_source_indexes.update(
                index for index, segment in enumerate(future_segments)
                if any(name in segment.text for name in known_names)
            )
            matching = [
                segment for index, segment in enumerate(future_segments)
                if index in context_source_indexes
            ]
        else:
            fallback_evidence_group_keys.add(group_key)
            matching = [
                segment for segment in future_segments
                if segment.start_offset < 900
            ]
        label_window_indexes = {
            index for index, segment in enumerate(matching)
            if any(label in segment.text for label in group_labels)
        }
        adjacent_label_window_indexes = {
            neighbor
            for index in label_window_indexes
            for neighbor in (index - 1, index + 1)
            if 0 <= neighbor < len(matching)
        }
        ranked = sorted(
            enumerate(matching),
            key=lambda item: (
                0 if item[0] in label_window_indexes else (
                    1 if item[0] in adjacent_label_window_indexes else 2
                ),
                -sum(name in item[1].text for name in known_names),
                0 if item[0] == 0 else 1,
                0 if item[0] == len(matching) - 1 else 1,
                item[1].start_offset,
            ),
        )
        selected: list = []
        used = 0
        max_windows = max(1, min(6, per_group_budget // 120))
        for _rank, segment in ranked:
            if used >= per_group_budget or len(selected) >= max_windows:
                break
            if segment.text in {item.text for item in selected}:
                continue
            if selected and used + len(segment.text) > per_group_budget:
                continue
            selected.append(segment)
            used += len(segment.text)
        selected.sort(key=lambda item: item.start_offset)
        group_evidence_ids: list[str] = []
        for segment in selected:
            evidence_id = "E:" + evidence_repository.content_hash({
                "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                "origin": "future",
                "source_hash": evidence_repository.content_hash(future_text),
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "text": segment.text,
            })[:20]
            evidence_by_id.setdefault(evidence_id, {
                "evidence_id": evidence_id,
                "origin": "future",
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "text": segment.text,
            })
            group_evidence_ids.append(evidence_id)
        evidence_ids_by_group[group_key] = list(dict.fromkeys(
            group_evidence_ids
        ))
    return evidence_by_id, evidence_ids_by_group, fallback_evidence_group_keys
