"""编排计划的模型调用：一次结构化文本调用产出 (a)(b)(c) 三项草稿，代码核验
（``plan_validate``），不满足的条目打回重试一次，最后仍不满足则丢弃并记录
原因——不整体失败、不兜底编造（见模块顶部函数 ``_amnesty_validate`` 的实现
说明）。

重试预算固定为 1（``_SEMANTIC_RETRY_LIMIT``），不做成可配置项：CLAUDE.md
「新增运行时设置键必须在 app/config.py 默认表里声明」，这个值是业务规则
（"打回重试一次"）不是运维旋钮，不值得为它开一个设置键。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.final_edit_enhance import plan_validate
from app.final_edit_enhance.context import EpisodeContext
from app.final_edit_enhance.music_library import MusicLibrary
from app.final_edit_enhance.plan_schema import EnhancementPlanDraft
from app.final_edit_enhance.silence import Interval
from app.harness import model_gateway

_SEMANTIC_RETRY_LIMIT = 1


@dataclass(frozen=True)
class ResolvedMusicCue:
    shot_no: int
    track_id: str


@dataclass(frozen=True)
class ResolvedTeaserClip:
    shot_no: int
    start_s: float
    end_s: float
    reason: str


@dataclass(frozen=True)
class ResolvedMonologueLine:
    start_s: float
    end_s: float
    character_name: str
    text: str


@dataclass(frozen=True)
class EnhancementPlan:
    music_cues: tuple[ResolvedMusicCue, ...]
    teaser_clips: tuple[ResolvedTeaserClip, ...]
    monologue_lines: tuple[ResolvedMonologueLine, ...]
    dropped: tuple[dict[str, Any], ...]
    teaser_total_duration_s: float


def plan_fingerprint(
    *, manifest_hash: str, context: EpisodeContext, library: MusicLibrary | None,
    switches: tuple[bool, bool, bool],
) -> str:
    """版本集合（``manifest_hash``）+ 节拍表（各段梗概/台词）+ 曲库清单 + 开关状态。"""
    shots_signature = [
        (s.shot_no, round(s.duration_s, 3), s.prompt_text, [d.text for d in s.dialogue])
        for s in context.shots
    ]
    library_signature = (
        sorted((t.track_id, t.duration_s, t.mood_tags) for t in library.tracks) if library else None
    )
    payload = {
        "manifest_hash": manifest_hash, "shots": shots_signature,
        "library": library_signature, "switches": switches,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=list).encode("utf-8"),
    ).hexdigest()[:32]


def _task_payload(
    context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval],
) -> dict[str, Any]:
    return {
        "segments": [
            {
                "shot_no": s.shot_no, "start_s": round(s.start_s, 2), "duration_s": round(s.duration_s, 2),
                "synopsis": s.prompt_text,
                "dialogue": [{"speaker": d.speaker, "line": d.text} for d in s.dialogue],
            }
            for s in context.shots
        ],
        "music_library": [
            {"track_id": t.track_id, "title": t.title, "mood_tags": list(t.mood_tags)}
            for t in (library.tracks if library else ())
        ],
        "silence_windows": [
            {"window_index": i, "start_s": round(w[0], 2), "end_s": round(w[1], 2), "duration_s": round(w[1] - w[0], 2)}
            for i, w in enumerate(windows)
        ],
        "character_roster": list(context.character_roster),
        "source_text": context.source_text,
    }


def _amnesty_validate(validate_fn):
    """把"打回重试一次，最后仍不满足就接受（交给调用方按条目丢弃）"这个策略
    包成 ``chat_structured`` 的 ``validate`` 回调：第 1 次调用（首次作答）如果
    有条目不满足就报错触发重试；第 2 次调用（重试后的作答，预算已耗尽）无论
    是否仍有不满足的条目都直接放行——不满足的条目由调用方在拿到最终 ``parsed``
    后再跑一遍同一份 ``validate_fn`` 做真正的丢弃，这里只负责"要不要再问模型
    一次"。"""
    attempts = [0]

    def _validate(draft: EnhancementPlanDraft) -> list[str]:
        attempts[0] += 1
        errors = validate_fn(draft)
        if attempts[0] > _SEMANTIC_RETRY_LIMIT:
            return []
        return errors

    return _validate


def _semantic_errors(draft: EnhancementPlanDraft, *, context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval]) -> list[str]:
    errors: list[str] = []
    if library is not None:
        _valid, dropped = plan_validate.validate_music_cues(draft.music_cues, context=context, library=library)
        errors.extend(f"配乐：{d['reason']}" for d in dropped)
    valid_clips, dropped_clips = plan_validate.validate_teaser_clips(draft.teaser_clips, context=context)
    errors.extend(f"预告：{d['reason']}" for d in dropped_clips)
    total = plan_validate.teaser_total_duration_s(valid_clips)
    if valid_clips and not (plan_validate.TEASER_TOTAL_MIN_S <= total <= plan_validate.TEASER_TOTAL_MAX_S):
        errors.append(f"预告：{len(valid_clips)} 段总长 {total:.1f}s，目标区间 8-12s")
    if valid_clips and not (plan_validate.TEASER_CLIP_COUNT_MIN <= len(valid_clips) <= plan_validate.TEASER_CLIP_COUNT_MAX):
        errors.append(
            f"预告：{len(valid_clips)} 段，目标区间 "
            f"{plan_validate.TEASER_CLIP_COUNT_MIN}-{plan_validate.TEASER_CLIP_COUNT_MAX} 段",
        )
    valid_lines, dropped_lines = plan_validate.validate_monologue_lines(draft.monologue_lines, context=context, windows=windows)
    errors.extend(f"独白：{d['reason']}" for d in dropped_lines)
    if valid_lines and not (plan_validate.MONOLOGUE_LINE_COUNT_MIN <= len(valid_lines) <= plan_validate.MONOLOGUE_LINE_COUNT_MAX):
        errors.append(
            f"独白：{len(valid_lines)} 句，目标区间 "
            f"{plan_validate.MONOLOGUE_LINE_COUNT_MIN}-{plan_validate.MONOLOGUE_LINE_COUNT_MAX} 句",
        )
    return errors


async def _call_model(
    context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval], episode_id: str, fingerprint: str,
) -> EnhancementPlanDraft:
    payload = _task_payload(context, library, windows)
    validate = _amnesty_validate(
        lambda draft: _semantic_errors(draft, context=context, library=library, windows=windows),
    )
    return await model_gateway.chat_structured(
        [
            {
                "role": "system",
                "content": (
                    "你是短剧成片后期剪辑师。只输出符合 Schema 的一个 JSON 对象，不输出 "
                    "Markdown 或解释。配乐 track_id 必须逐字取自给定曲库清单；预告片段给出 "
                    "3-5 个（shot_no/start_s/end_s 必须落在给定段落时长内，单段 1.5-4 秒，"
                    "总长 8-12 秒）；独白给出 3-8 句，正文必须逐字取自给定原文，"
                    "character_name 必须是给定人物谱中的真实角色，window_index 必须是给定"
                    "候选静默窗口的下标（不要自己编造秒数）。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model_type=EnhancementPlanDraft,
        validate=validate,
        operation_id=f"final_edit_enhance_plan_{episode_id}_{fingerprint}",
        max_tokens=4000,
        format_retry_limit=1,
        semantic_retry_limit=_SEMANTIC_RETRY_LIMIT,
        temperature=0.4,
        call_meta={
            "stage_key": "final_edit_enhance_plan", "call_role": "final_edit_enhance",
            "initiator_label": "成片增强编排", "episode_id": episode_id,
        },
    )


def _resolve_teaser(
    draft: EnhancementPlanDraft, *, context: EpisodeContext,
) -> tuple[list[Any], float, list[dict[str, Any]]]:
    """先按单段判据核验，再核验总长（8-12s）——单段各自合法但总长越界时，
    没有原则性的办法挑选"留哪几段丢哪几段"，所以整批预告一起跳过（不兜底
    截断/编造），与 ``_semantic_errors`` 里用于触发重试的同一条判据呼应。"""
    valid, item_dropped = plan_validate.validate_teaser_clips(draft.teaser_clips, context=context)
    dropped = [{"feature": "teaser", **d} for d in item_dropped]
    total = plan_validate.teaser_total_duration_s(valid)
    if valid and not (plan_validate.TEASER_TOTAL_MIN_S <= total <= plan_validate.TEASER_TOTAL_MAX_S):
        dropped.append({
            "feature": "teaser",
            "item": {"clips": [c.model_dump() for c in valid]},
            "reason": f"{len(valid)} 段总长 {total:.1f}s，超出 8-12s 目标区间，预告片整体跳过",
        })
        valid, total = [], 0.0
    return valid, total, dropped


def _resolve_monologue(
    draft: EnhancementPlanDraft, *, context: EpisodeContext, windows: list[Interval],
) -> tuple[list[ResolvedMonologueLine], list[dict[str, Any]]]:
    """通过校验的独白不能直接套用整段候选窗口的原始边界——同一窗口可能被
    多句共用，必须按 ``allocate_monologue_placements`` 重放出的实际占用区间
    摆放，否则同窗口内的多句独白会拿到相同的 (start, end) 而在混音/字幕上
    互相重叠（2026-09-28 评审发现，见测试 ``test_resolve_monologue_lines_
    sharing_one_window_do_not_overlap``）。"""
    valid, item_dropped = plan_validate.validate_monologue_lines(draft.monologue_lines, context=context, windows=windows)
    dropped = [{"feature": "monologue", **d} for d in item_dropped]
    placements = plan_validate.allocate_monologue_placements(valid, windows)
    resolved = [
        ResolvedMonologueLine(start, end, m.character_name, m.text)
        for m, (start, end) in zip(valid, placements, strict=True)
    ]
    return resolved, dropped


def _resolve(draft: EnhancementPlanDraft, *, context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval]) -> EnhancementPlan:
    dropped: list[dict[str, Any]] = []
    music_valid: list[Any] = []
    if library is not None:
        music_valid, music_dropped = plan_validate.validate_music_cues(draft.music_cues, context=context, library=library)
        dropped.extend({"feature": "music_bed", **d} for d in music_dropped)
    teaser_valid, teaser_total, teaser_dropped = _resolve_teaser(draft, context=context)
    dropped.extend(teaser_dropped)
    monologue_lines, monologue_dropped = _resolve_monologue(draft, context=context, windows=windows)
    dropped.extend(monologue_dropped)
    return EnhancementPlan(
        music_cues=tuple(ResolvedMusicCue(c.shot_no, c.track_id) for c in music_valid),
        teaser_clips=tuple(ResolvedTeaserClip(c.shot_no, c.start_s, c.end_s, c.reason) for c in teaser_valid),
        monologue_lines=tuple(monologue_lines),
        dropped=tuple(dropped),
        teaser_total_duration_s=teaser_total,
    )


async def generate_plan(
    *, context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval], episode_id: str, fingerprint: str,
) -> EnhancementPlan:
    draft = await _call_model(context, library, windows, episode_id, fingerprint)
    return _resolve(draft, context=context, library=library, windows=windows)
