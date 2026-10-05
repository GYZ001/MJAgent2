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

``rewrite_flagged_segments``：对调用方确认重写的段（``confirmed_segment_nos``，
必须是待重写集合的子集；``kinds`` 可选，只处理这些类别），按「最小修改重写」
语义保存——只对局部可修的违规（``prop_continuity_minimal_patch.
is_locally_patchable``）发一次最小替换提案、代码核验区间后应用，走
``identity_workspace.save_identity_candidate`` 既有语义（旧视频继续采用直到
新版本生成成功）；**不再调用整段重生成**（``storyboard_identity_regenerate.
regenerate_identity_candidate`` 对目标段从头重写，会把用户在「修订本段」里
手工调过、没有持久化成结构化字段的措辞冲掉——2026-10-05 复核修正，完整背景见
``prop_continuity_minimal_patch`` 模块 docstring）。结构类违规（``action_
density``/``unvoiced_speech``/``time_jump``/``impossible_camera_move``）与
没有一条替换核验通过的段都不会被保存，只在 ``outcome`` 里原样留痕，供人工走
单段「修订本段」处理。没有违规、或违规未被确认/未命中 ``kinds`` 过滤的段原样
跳过，不触碰其 ``shot_contract_json``/``shot_versions``。单段失败——不管是
并发冲突/候选校验不过（``ValueError``）还是模型调用层本身的失败（供应商错误、
格式修复耗尽等，``Exception`` 的其它子类）——都记在该段 ``outcome.error`` 上，
不中断其余段落（2026-10-04 复核实测：曾经只捕获 ``ValueError`` 会让其它异常
直接中断整批、吞掉已经处理过的 ``outcomes``，与本段落「不中断其余段落」的
承诺矛盾）——复核结果本身可见，哪一段失败一目了然（CLAUDE.md「拦住用户时
必须给出路」：这里不是拦截用户，是让失败可追溯）。

重写成功后会用新 ``prompt_text``（``continuity_memo`` 本身不随局部替换改写，
见下段）再跑一次同一套复核（``_review_segment``/``_verified_violations``），
核验仍通过的违规写进 ``outcome.remaining_violations`` 并记可见日志——不是
「重写了就算数」，重写后仍有问题要能看见，不静默（与生成期
``review_segment_inline`` 把剩余违规写进 ``degraded_capabilities`` 同一纪律，
这里落在批量重写自己的结果结构里，不借道 ``degraded_capabilities``——那是
分镜草稿本身的字段，本模块不改写它）。``outcome.continuity_memo_conflicts``
（见 ``prop_continuity_minimal_patch.continuity_memo_conflicts``）同样不
静默：局部替换改了正文但没人跟着改 ``continuity_memo``，两者矛盾时只报告，
不代替用户判断该怎么改。

批量重写按段号升序处理，``rewrite_flagged_segments`` 把每段重写后的新草稿
原地写回 ``drafts_by_no``，供下一段的 ``previous_draft`` 使用——但新草稿的
``continuity_memo`` 字段是原样沿用、没有跟着局部替换更新（上一段落）。如果
第 N-1 段本身也被重写过、且它的 ``continuity_memo`` 恰好记录了改之前的状态
（即出现在它自己的 ``outcome.continuity_memo_conflicts`` 里），第 N 段「重写
后再复核」用到的 ``previous_continuity_memo`` 就是这份陈旧记录——这种情况下
第 N 段 ``outcome.remaining_violations`` 里出现的 ``prop_state_regression``
等跨段判据，可能是因为上一段的 ``continuity_memo`` 没跟着改，不代表第 N 段
这次替换本身无效；核对时先看上一段 ``outcome.continuity_memo_conflicts`` 是
否非空。

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
from app.production.storyboard_pack import _AiStoryboardSegmentDraft
from app.production.storyboard_prose_review import ProseViolation, _KIND_RULES, _review_segment, _verified_violations
from app.visual_styles import current_visual_style_prompt, is_photographic_style_prompt
from .identity_workspace import load_identity_workspace, save_identity_candidate
from .prop_continuity_minimal_patch import (
    build_patch_candidate, continuity_memo_conflicts, is_locally_patchable, propose_minimal_patch, validate_replacements,
)

#: GET 预览与 POST /rewrite 的 ``kinds`` 过滤共用的合法取值集合——与
#: ``storyboard_prose_review._verified_violations`` 判断 kind 合法性同一份
#: 数据（CLAUDE.md「模型契约两侧必须对齐」），不另立第二份清单。
VALID_PROSE_REVIEW_KINDS = frozenset(_KIND_RULES)

_LOGGER = logging.getLogger(__name__)


@dataclass
class SegmentReviewOutcome:
    """一段的复核/重写终态，供 dry-run 脚本与 API 响应共用——``violations``
    是已核验（代码核验通过）的违规，不是模型原始提名。``remaining_violations``
    只在 ``rewritten=True`` 时可能非空：重写后再核验一遍仍通过的违规，见
    模块 docstring「不是重写了就算数」。``skip_reason`` 与 ``error`` 是两类不同
    的「没有发生保存」：前者是「核验后没有一条替换可应用」这个预期内结果（不是
    失败），后者是并发冲突/供应商错误这类真正的异常。"""

    segment_no: int
    shot_id: str
    violations: list[ProseViolation] = field(default_factory=list)
    rewritten: bool = False
    artifact_id: str | None = None
    error: str | None = None
    skip_reason: str | None = None
    remaining_violations: list[ProseViolation] = field(default_factory=list)
    continuity_memo_conflicts: list[str] = field(default_factory=list)
    rejected_replacements: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        patchable = [v for v in self.violations if is_locally_patchable(v.kind)]
        manual = [v for v in self.violations if not is_locally_patchable(v.kind)]
        return {
            "segment_no": self.segment_no, "shot_id": self.shot_id, "rewritten": self.rewritten,
            "artifact_id": self.artifact_id, "error": self.error, "skip_reason": self.skip_reason,
            "violations": [v.model_dump(mode="json") for v in self.violations],
            "locally_patchable_violations": [v.model_dump(mode="json") for v in patchable],
            "needs_manual_revision_violations": [v.model_dump(mode="json") for v in manual],
            "remaining_violations": [v.model_dump(mode="json") for v in self.remaining_violations],
            "continuity_memo_conflicts": self.continuity_memo_conflicts,
            "rejected_replacements": self.rejected_replacements,
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


def stored_segment_prompt_text(conn, episode_id: str, segment_no: int) -> str:
    """供 ``scripts/storyboard_prop_continuity_dry_run.py`` 按段号取当前落库的
    ``prompt_text``，离线试算最小修改提案的替换与差异展示——只读，复用
    ``_load_stored_segments``，不新开第二套『取段落』逻辑。"""
    for _row, segment in _load_stored_segments(conn, episode_id):
        if segment["segment_no"] == segment_no:
            return str(segment.get("prompt_text") or "")
    raise ValueError(f"第 {segment_no} 段不存在")


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


async def _minimal_patch_and_save(
    conn, *, episode: dict, shot_id: str, segment_no: int, violations: list[ProseViolation],
    previous_draft: Any | None, photographic: bool,
) -> tuple[str | None, Any | None, list[ProseViolation], list[str], list[dict[str, str]], str | None]:
    """最小修改重写并保存——见模块 docstring「不再调用整段重生成」。``violations``
    必须已经是调用方按 ``is_locally_patchable`` 过滤过的局部可修子集。返回
    ``(artifact_id, new_draft, remaining_violations, continuity_memo_conflicts,
    rejected_replacements, skip_reason)``；``artifact_id is None`` 表示没有一条
    替换核验通过，没有发生保存——``skip_reason`` 说明原因，不是异常。"""
    _row, _episode, _payload, original = load_identity_workspace(conn, shot_id)
    raw = await propose_minimal_patch(episode_id=episode["id"], segment_no=segment_no, prompt_text=original["prompt_text"], violations=violations)
    accepted, rejected = validate_replacements(original["prompt_text"], raw, violations=violations)
    rejected_dicts = [r.to_dict() for r in rejected]
    if not accepted:
        return None, None, [], [], rejected_dicts, "最小替换提案核验后没有一条可应用，未发生保存"
    candidate = build_patch_candidate(original, accepted)
    if candidate is None:
        rejected_dicts.append({"quote": "", "replacement": "", "reason": "替换无法同步套用到台词模板（speech_template），本段不可安全局部修改"})
        return None, None, [], [], rejected_dicts, "替换无法同步套用到台词模板，未发生保存"
    conflicts = continuity_memo_conflicts(original.get("continuity_memo") or {}, accepted)
    baseline = identity_contract_fingerprint(original)
    result = save_identity_candidate(conn, shot_id=shot_id, baseline=baseline, candidate=candidate)
    if result.get("unchanged"):
        return None, None, [], conflicts, rejected_dicts, "替换结果与存量分镜一致，未发生实际变化"
    saved_segment = result["segment"]
    new_draft = _AiStoryboardSegmentDraft.model_validate(dict(saved_segment, beats=saved_segment.get("montage_beats") or []))
    raw_review = await _review_segment(
        episode_id=episode["id"], segment_no=segment_no, draft=new_draft,
        previous_draft=previous_draft, photographic=photographic, max_shots=new_draft.shot_count,
    )
    remaining = _verified_violations(raw_review, segment_no=segment_no, draft=new_draft, previous_draft=previous_draft)
    if remaining:
        _LOGGER.warning(
            "[STORYBOARD_PROP_CONTINUITY_BATCH_REWRITE_STILL_FLAGGED] 第 %s 段（shot=%s）局部修改后仍有 %d 条已核验违规：%s",
            segment_no, shot_id, len(remaining), "；".join(f"[{v.kind}] {v.fix[:60]}" for v in remaining),
        )
    return result["artifact_id"], new_draft, remaining, conflicts, rejected_dicts, None


async def rewrite_flagged_segments(
    conn, *, episode: dict, bible, outcomes: list[SegmentReviewOutcome],
    confirmed_segment_nos: set[int], kinds: set[str] | None = None,
) -> list[SegmentReviewOutcome]:
    """对 ``confirmed_segment_nos`` 里确认的段，取其违规里局部可修
    （``is_locally_patchable``）且命中 ``kinds`` 过滤（``None`` 表示不过滤）的
    子集走最小修改重写；未确认的段、没有命中子集的段原样跳过。结构类违规无论
    是否确认都不会被自动改——只会出现在 ``outcome.violations`` 里原样保留，
    见模块 docstring。"""
    photographic = _is_photographic(bible)
    drafts_by_no = _stored_drafts_by_segment_no(conn, episode["id"])
    with stage_text_provider(_board_text_provider(conn, episode["project_id"])):
        for outcome in outcomes:
            if outcome.segment_no not in confirmed_segment_nos:
                continue
            candidates = [v for v in outcome.violations if is_locally_patchable(v.kind) and (kinds is None or v.kind in kinds)]
            if not candidates:
                outcome.skip_reason = (
                    "已核验违规均为结构类（需人工修订本段）或被 kinds 过滤排除，没有可应用的局部修改"
                )
                continue
            try:
                artifact_id, new_draft, remaining, conflicts, rejected, skip_reason = await _minimal_patch_and_save(
                    conn, episode=episode, shot_id=outcome.shot_id, segment_no=outcome.segment_no,
                    violations=candidates, previous_draft=drafts_by_no.get(outcome.segment_no - 1), photographic=photographic,
                )
                outcome.rejected_replacements = rejected
                outcome.continuity_memo_conflicts = conflicts
                if artifact_id is None:
                    outcome.skip_reason = skip_reason
                    continue
                outcome.artifact_id = artifact_id
                outcome.rewritten = True
                outcome.remaining_violations = remaining
                drafts_by_no[outcome.segment_no] = new_draft
            except Exception as exc:  # noqa: BLE001 -- 单段失败（含供应商/模型调用层异常）不中断其余段落，见模块 docstring
                outcome.error = str(exc)
                _LOGGER.warning(
                    "[STORYBOARD_PROP_CONTINUITY_BATCH_REWRITE_FAILED] 第 %s 段（shot=%s）局部修改失败：%s",
                    outcome.segment_no, outcome.shot_id, exc,
                )
    return outcomes
