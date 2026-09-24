"""``persist_storyboard_pack`` 落库的三份 EvidenceArtifact：节拍表、对白台账、
改编档位留档，同一事务写入。

拆出原因：``storyboard_pack.py`` 在 ``app/FILE_CONVENTIONS.toml`` 的
``line_count`` 棘轮基线上零余量，2026-09-23 新增的改编档位留档（第三份
artifact）需要新行数——原先写 beat_sheet/dialogue_ledger 两份产物的代码段
自成一体、只在 ``persist_storyboard_pack`` 末尾被调用一次，纯搬移（不改
beat_sheet/dialogue_ledger 两份产物的内容/顺序）到这里腾出空间。

``storyboard_pack_adaptation`` 产物形状（冻结契约，门禁侧 ``app.domain.
video_ops.source_coverage`` 读取此形状，字段名/结构不得无协调改动）::

    {"adaptation_mode": "faithful|short_drama",
     "target_duration_s": int | None, "target_segment_count": int | None,
     "max_segment_count": int | None,
     "planned_segment_count": int, "segment_count": int, "over_target": bool,
     "dropped_source_spans": [{"source_segment_index", "from_unit", "to_unit",
                                "reason", "chapter_idx", "start_offset",
                                "end_offset", "excerpt", "chars"}],
     "dropped_line_quote_ids": [str]}

忠实档也写这一条（``adaptation_mode="faithful"``、``dropped_source_spans``
恒空）：门禁按"最高 version 那一条"判定当前留档，只在短剧档才写会让旧的
短剧留档在忠实档重生后仍被当成当前留档（见 CLAUDE.md 对本次改造的冻结
契约第 2 条）。生产路径 ``generate_storyboard_pack`` 一定会填好
``pack.adaptation``（不管哪个档位都调用 ``storyboard_short_drama.
adaptation_summary`` 拼出至少含 ``adaptation_mode`` 的内容），``pack.
adaptation`` 为空只会发生在测试直接构造 ``StoryboardPack`` 且不设这个
字段的场景——这种情况**不写**这一条产物，而不是写一条没有 adaptation_mode
的残缺记录：门禁对"没有这条留档"本就按忠实档（零容忍覆盖）处理，语义上
与"写一条内容为空的记录"等价，但不会把半成品数据落进 artifacts 表。
2026-09-23 权衡记录：改成"缺失即抛错"会牵连仓库里 20+ 处既有测试的
``persist_storyboard_pack``/``_pack()`` 构造点（超过按点逐个修的阈值），
选"跳过写入"是对现有契约测试扰动最小的做法。
"""
from __future__ import annotations

import re
from typing import Any

from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.production.storyboard_source_spans import segment_source_bindings
from app.source_excerpt import SourceSegment


def _dropped_span_evidence(
    span: dict[str, Any], *, segments: list[SourceSegment], full_source_text: str,
    authorized_sources: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """把 ``{source_segment_index, from_unit, to_unit, reason}`` 换算成章节
    偏移 + 原文摘录；复用 ``segment_source_bindings``（分镜台持久化每段原文
    绑定用的同一套换算），不另写一套坐标转换。定位不到任何授权章节（不应
    在正常路径发生——这个区间本就来自本集已加载的 ``segments``）时返回
    ``None``，调用方跳过这一条，不拿一条坐标缺失的记录污染留档。
    """
    bindings = segment_source_bindings(
        {
            "source_segment_indexes": [span["source_segment_index"]],
            "source_unit_ranges": [{
                "source_segment_index": span["source_segment_index"],
                "from_unit": span["from_unit"], "to_unit": span["to_unit"],
            }],
        },
        segments=segments, full_source_text=full_source_text, authorized_sources=authorized_sources,
    )
    if not bindings:
        return None
    binding = bindings[0]
    source = next(
        (s for s in authorized_sources if int(s["idx"]) == binding["chapter_idx"]), None,
    )
    if source is None:
        return None
    excerpt = str(source["content"])[binding["start_offset"]:binding["end_offset"]]
    return {
        **span,
        "chapter_idx": binding["chapter_idx"],
        "start_offset": binding["start_offset"],
        "end_offset": binding["end_offset"],
        "excerpt": excerpt[:24],
        "chars": len(re.sub(r"\s+", "", excerpt)),
    }


def _adaptation_evidence_content(
    pack: Any, *, segments: list[SourceSegment], full_source_text: str, authorized_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = pack.adaptation or {}
    spans = [
        enriched for span in (raw.get("dropped_source_spans") or [])
        if (enriched := _dropped_span_evidence(
            span, segments=segments, full_source_text=full_source_text, authorized_sources=authorized_sources,
        )) is not None
    ]
    return {**raw, "dropped_source_spans": spans}


def _write_artifact(
    conn: Any, *, artifact_type: str, episode_id: str, content: dict[str, Any],
    parent_artifact_ids: list[str], contract_version: str,
) -> None:
    """三份产物共用的落库调用；只有 type/content 逐条不同，抽出来给
    ``persist_storyboard_pack_evidence`` 腾函数行数（``function_lines`` 默认
    上限 50，三份内联 ``create_artifact`` 调用会顶到 51）。"""
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type=artifact_type, scope_type="episode", scope_id=episode_id, status="validated",
            trust_level="T2", content=content, parent_artifact_ids=parent_artifact_ids,
            contract_version=contract_version,
        ),
        conn=conn, commit=False,
    )


def persist_storyboard_pack_evidence(
    conn: Any, episode_id: str, ep: Any, pack: Any, *,
    segments: list[SourceSegment], full_source_text: str, authorized_sources: list[dict[str, Any]],
) -> None:
    """写 beat_sheet/dialogue_ledger/adaptation 三份产物，同一事务、不在这里
    ``commit()``（``persist_storyboard_pack`` 落库全部 shots 之后统一提交）。
    beat_sheet 是每次生成的可审计记录：``_generate_beat_sheet`` 到底产出了
    什么（segment_count 就是 ``len(pack.segments)``，即本模块存在的意义要
    回答的那个数字——见 ``generate_storyboard_pack`` 文档）。
    """
    parents = [str(ep["screenplay_artifact_id"])] if ep["screenplay_artifact_id"] else []
    version = pack.storyboard_version
    _write_artifact(
        conn, artifact_type="storyboard_pack_beat_sheet", episode_id=episode_id, parent_artifact_ids=parents,
        contract_version=version, content={
            "storyboard_version": version, "episode_no": pack.episode_no, "target_model": pack.target_model,
            "segment_count": len(pack.segments),
            "beat_sheet": [beat.model_dump(mode="json") for beat in pack.beat_sheet],
        },
    )
    # 2.1.0：episode 级对白台账，与上面 beat_sheet 产物同一模式、同一事务。
    _write_artifact(
        conn, artifact_type="storyboard_pack_dialogue_ledger", episode_id=episode_id, parent_artifact_ids=parents,
        contract_version=version, content=pack.dialogue_ledger,
    )
    # 2026-09-23：改编档位留档，忠实档也写；pack.adaptation 为空（只应发生在
    # 未显式设置这个字段的测试构造）时跳过，不写一条残缺记录（见本模块 docstring）。
    if pack.adaptation:
        _write_artifact(
            conn, artifact_type="storyboard_pack_adaptation", episode_id=episode_id, parent_artifact_ids=parents,
            contract_version=version, content=_adaptation_evidence_content(
                pack, segments=segments, full_source_text=full_source_text, authorized_sources=authorized_sources,
            ),
        )
