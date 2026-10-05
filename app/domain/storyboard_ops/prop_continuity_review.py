"""存量分镜按现行复核规则批量核查（P0 第 4/5 项，2026-10-04，用户反馈《顾念
长安》EP1 第 1→2 段插座/插头驱动，完整调查见 app.production.
storyboard_prop_continuity 模块 docstring）。

## 设计

只读已落库的各段 ``prompt_text``/``continuity_memo``，跑与生成期「边写边审」
同一套复核判据（``app.production.storyboard_prose_review._review_segment``/
``_verified_violations``，模型提名、代码核验），不重新规划 beat_sheet/
wardrobe_plan/prop_entrance——那是 ``storyboard_identity_regenerate.
_existing_plan`` 已经在「修订本段」里做过的降级近似，本模块原样复用，不新造
第二套判据或第二套近似。

``review_existing_episode_segments``：纯复核，不写库、不调用重写模型——可以
反复跑、可以在只读连接上跑（见 ``scripts/storyboard_prop_continuity_dry_run.py``）。

``rewrite_flagged_segments``：对前者产出里有已核验违规的段，逐一走「修订
本段」语义重写并保存（``identity_workspace.save_identity_candidate`` 既有
语义：旧视频继续采用直到新版本生成成功——不是本模块新发明的保留规则）；
没有违规的段原样跳过，不触碰其 ``shot_contract_json``/``shot_versions``。
单段失败——不管是并发冲突/候选校验不过/重写结果与存量一致（``ValueError``）
还是模型调用层本身的失败（供应商错误、格式修复耗尽等，``Exception`` 的其它
子类）——都记在该段 ``outcome.error`` 上，不中断其余段落（2026-10-04 复核
实测：曾经只捕获 ``ValueError`` 会让其它异常直接中断整批、吞掉已经处理过的
``outcomes``，与本段落「不中断其余段落」的承诺矛盾）——复核结果本身可见，
哪一段失败一目了然（CLAUDE.md「拦住用户时必须给出路」：这里不是拦截用户，
是让失败可追溯）。

重写成功后会用新 ``prompt_text``/``continuity_memo`` 再跑一次同一套复核
（``_review_segment``/``_verified_violations``），核验仍通过的违规写进
``outcome.remaining_violations`` 并记可见日志——不是「重写了就算数」，重写后
仍有问题要能看见，不静默（与生成期 ``review_segment_inline`` 把剩余违规写进
``degraded_capabilities`` 同一纪律，这里落在批量重写自己的结果结构里，不
借道 ``degraded_capabilities``——那是分镜草稿本身的字段，本模块不改写它）。

## 为什么放在 app.domain.storyboard_ops（L5）

本模块既要用 ``app.production.storyboard_identity_regenerate``（L4，「修订本段」
的模型重写）又要用同包的 ``identity_workspace``（L5，CAS 保存/旧视频保留）
——两者分属不同层，只有 L5 能同时模块级 import 两边，不需要为了避免层号倒置
而把 CAS 保存这一步用函数内延迟 import 藏进 L4（那是「函数内 import app.*
不是解耦手段」要防的事）。

## 与文本 provider 覆盖的关系

复核与重写调用都要落在项目配置的分环节文本 provider（``projects.
board_text_provider``）下，否则批量跑出来的判据会用错模型——两个公开函数各自
在自己的调用边界内用 ``stage_text_provider`` 包一层，与
``app.domain.storyboard_ops.identity_review`` 现有路由同一先例，不依赖调用方
记得包。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.harness.text_provider_scope import stage_text_provider
from app.model_registry import resolve_stage_text_provider
from app.production.storyboard_identity_contract import identity_contract_fingerprint
from app.production.storyboard_identity_regenerate import regenerate_identity_candidate
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_prose_review import ProseViolation, _revision_notes_text, _review_segment, _verified_violations
from app.visual_styles import current_visual_style_prompt, is_photographic_style_prompt
from .identity_workspace import load_identity_workspace, save_identity_candidate

_LOGGER = logging.getLogger(__name__)


@dataclass
class SegmentReviewOutcome:
    """一段的复核/重写终态，供 dry-run 脚本与 API 响应共用——``violations``
    是已核验（代码核验通过）的违规，不是模型原始提名。``remaining_violations``
    只在 ``rewritten=True`` 时可能非空：重写后再核验一遍仍通过的违规，见
    模块 docstring「不是重写了就算数」。"""

    segment_no: int
    shot_id: str
    violations: list[ProseViolation] = field(default_factory=list)
    rewritten: bool = False
    artifact_id: str | None = None
    error: str | None = None
    remaining_violations: list[ProseViolation] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_no": self.segment_no, "shot_id": self.shot_id, "rewritten": self.rewritten,
            "artifact_id": self.artifact_id, "error": self.error,
            "violations": [v.model_dump(mode="json") for v in self.violations],
            "remaining_violations": [v.model_dump(mode="json") for v in self.remaining_violations],
        }


def _load_stored_segments(conn, episode_id: str) -> list[tuple[dict, dict]]:
    """返回 [(shot_row, segment_dict), ...] 按 shot_no 升序；旧式分镜（没有
    storyboard_pack_segment）直接抛错，与 regenerate_identity_candidate 既有
    判据一致（CLAUDE.md「模型契约两侧必须对齐」不重开一套判据）。"""
    rows = conn.execute(
        "SELECT id, shot_no, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no", (episode_id,),
    ).fetchall()
    stored = [(dict(row), json.loads(row["shot_contract_json"] or "{}").get("storyboard_pack_segment")) for row in rows]
    if not all(segment for _, segment in stored):
        raise ValueError("本集包含旧式分镜，请使用对应的分镜修订入口")
    return stored


def _stored_drafts_by_segment_no(conn, episode_id: str) -> dict[int, Any]:
    """``_load_stored_segments`` 的结果按 segment_no 建索引，供重写后定位
    「上一段现在的草稿」（可能是本批次刚重写过的新版本，不是重写前的旧版本——
    见 ``rewrite_flagged_segments`` 对该字典的原地更新）。"""
    return {
        segment["segment_no"]: _AiStoryboardSegmentDraft.model_validate(dict(segment, beats=segment.get("montage_beats") or []))
        for _row, segment in _load_stored_segments(conn, episode_id)
    }


def _is_photographic(bible) -> bool:
    if bible is None or bible.world is None:
        return False
    return is_photographic_style_prompt(current_visual_style_prompt(bible.world.visual_style_canonical))


def _board_text_provider(conn, project_id: str) -> str | None:
    project = conn.execute("SELECT board_text_provider FROM projects WHERE id=?", (project_id,)).fetchone()
    return resolve_stage_text_provider(dict(project or {}).get("board_text_provider"))


async def review_existing_episode_segments(conn, *, episode: dict, bible) -> list[SegmentReviewOutcome]:
    """只读复核：不写库、不调用重写模型——见模块 docstring。"""
    photographic = _is_photographic(bible)
    stored = _load_stored_segments(conn, episode["id"])
    outcomes: list[SegmentReviewOutcome] = []
    previous_draft: Any | None = None
    with stage_text_provider(_board_text_provider(conn, episode["project_id"])):
        for row, segment in stored:
            draft = _AiStoryboardSegmentDraft.model_validate(dict(segment, beats=segment.get("montage_beats") or []))
            raw = await _review_segment(
                episode_id=episode["id"], segment_no=segment["segment_no"], draft=draft,
                previous_draft=previous_draft, photographic=photographic, max_shots=draft.shot_count,
            )
            violations = _verified_violations(raw, segment_no=segment["segment_no"], draft=draft, previous_draft=previous_draft)
            outcomes.append(SegmentReviewOutcome(segment_no=segment["segment_no"], shot_id=row["id"], violations=violations))
            previous_draft = draft
    return outcomes


async def _rewrite_and_save(
    conn, *, episode: dict, payload: dict, bible, shot_id: str, segment_no: int,
    violations: list[ProseViolation], previous_draft: Any | None, photographic: bool,
) -> tuple[str, Any, list[ProseViolation]]:
    """重写并保存后，用新草稿再跑一次同一套复核——返回 ``artifact_id``、新
    草稿（供调用方更新「上一段现在的草稿」索引）与重写后仍核验通过的违规
    （可能非空，见模块 docstring）。"""
    _row, _episode, _payload, original = load_identity_workspace(conn, shot_id)
    baseline = identity_contract_fingerprint(original)
    candidate = await regenerate_identity_candidate(
        conn, episode=episode, shot_id=shot_id, payload=payload, bible=bible,
        revision_notes=_revision_notes_text(violations),
    )
    result = save_identity_candidate(conn, shot_id=shot_id, baseline=baseline, candidate=candidate)
    if result.get("unchanged"):
        raise ValueError("重写结果与存量分镜一致，未发生实际变化")
    new_draft = _AiStoryboardSegmentDraft.model_validate(dict(candidate, beats=candidate.get("montage_beats") or []))
    raw = await _review_segment(
        episode_id=episode["id"], segment_no=segment_no, draft=new_draft,
        previous_draft=previous_draft, photographic=photographic, max_shots=new_draft.shot_count,
    )
    remaining = _verified_violations(raw, segment_no=segment_no, draft=new_draft, previous_draft=previous_draft)
    if remaining:
        _LOGGER.warning(
            "[STORYBOARD_PROP_CONTINUITY_BATCH_REWRITE_STILL_FLAGGED] 第 %s 段（shot=%s）重写后仍有 %d 条已核验违规：%s",
            segment_no, shot_id, len(remaining), "；".join(f"[{v.kind}] {v.fix[:60]}" for v in remaining),
        )
    return result["artifact_id"], new_draft, remaining


async def rewrite_flagged_segments(
    conn, *, episode: dict, payload: dict, bible, outcomes: list[SegmentReviewOutcome],
) -> list[SegmentReviewOutcome]:
    """对 ``review_existing_episode_segments`` 产出里有已核验违规的段逐一
    重写并保存；没有违规的段原样跳过，不重写。"""
    photographic = _is_photographic(bible)
    drafts_by_no = _stored_drafts_by_segment_no(conn, episode["id"])
    with stage_text_provider(_board_text_provider(conn, episode["project_id"])):
        for outcome in outcomes:
            if not outcome.violations:
                continue
            try:
                artifact_id, new_draft, remaining = await _rewrite_and_save(
                    conn, episode=episode, payload=payload, bible=bible, shot_id=outcome.shot_id,
                    segment_no=outcome.segment_no, violations=outcome.violations,
                    previous_draft=drafts_by_no.get(outcome.segment_no - 1), photographic=photographic,
                )
                outcome.artifact_id = artifact_id
                outcome.rewritten = True
                outcome.remaining_violations = remaining
                drafts_by_no[outcome.segment_no] = new_draft
            except Exception as exc:  # noqa: BLE001 -- 单段失败（含供应商/模型调用层异常）不中断其余段落，见模块 docstring
                outcome.error = str(exc)
                _LOGGER.warning(
                    "[STORYBOARD_PROP_CONTINUITY_BATCH_REWRITE_FAILED] 第 %s 段（shot=%s）重写失败：%s",
                    outcome.segment_no, outcome.shot_id, exc,
                )
    return outcomes
