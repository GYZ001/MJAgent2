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

# 规则文案（系统提示词/Schema 字段描述）或核验逻辑有实质变化时必须递增：
# 指纹（``plan_fingerprint``）把它编码进输入，旧规则生成并缓存的计划会因指纹
# 变化而失效，不会被新规则部署后继续复用（2026-09-29：修配乐"换曲点"语义 +
# 三项开关开启却空输出必须重试两项改动，从 1 bump 到 2；同日预告片改为"模型
# 只选起点、片长固定为常量" + 独白候选窗口阈值从固定 30s 改为数据推导 + 独白
# 正文新增"不能与本集已播出台词/旁白重复"校验，三项改动再从 2 bump 到 3——
# 见 ``app.final_edit_enhance.plan_store.load_cached_plan`` 顶部注释：旧缓存
# 里 ``TeaserClipDraft``/``ResolvedTeaserClip`` 形状里的 ``end_s`` 字段在新
# dataclass 里已不存在，就算指纹意外撞车，反序列化本身也会因未知关键字参数
# 报 ``TypeError`` 被现有 except 分支吞掉、退回重新生成，指纹变化只是第一道、
# 不是唯一一道防线）。
_PLAN_RULES_VERSION = 3

_SYSTEM_PROMPT = (
    "你是短剧成片后期剪辑师。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释。"
    "配乐 music_cues 是「换曲点」列表，不是逐段配乐表：把全集按情绪走向切成 2-5 个大段"
    "（例如「艰难铺垫→重逢→甜蜜/悬念收尾」），每段从给定曲库里选一首与该段情绪最匹配的"
    "曲目（参考 mood_tags）；每条 cue 表示「从这个 shot_no 起改播这首曲子，一直连续播放到"
    "下一条 cue 的 shot_no 为止（没有下一条就播到全集结束）」——不要给每个 shot_no 都各提"
    "一条，只在真正换曲的 shot_no 处给一条；第一条 cue 必须落在本集参与合成的第一个"
    "shot_no；除最后一段允许因全集本身较短而不足外，每一段都要覆盖足够时长（把该段起点到"
    "下一条 cue 之间的段落时长加起来，至少约 45 秒），不要几秒钟就换一首曲子——那正是要"
    "避免的「观众跟不上音乐」的问题；track_id 必须逐字取自给定曲库清单。"
    "预告片段给出 3-4 个：每条只给 shot_no 和 start_s，不要给结束时间——片段时长系统"
    "固定为 3 秒，从 start_s 起自动截取到 start_s+3 秒，你只需要选「从哪一秒开始最有"
    "悬念/信息量」，并保证 start_s+3 秒不超出该段时长；3 段共 9 秒、4 段共 12 秒，都在"
    "预告总长 8-12 秒目标区间内。"
    "独白给出 3-8 句：正文必须是原文里描述该角色内心感受/想法的叙述句（引号之外的叙述，"
    "不是角色已经用引号说出口的台词），逐字取自给定原文；不能与给定 segments 的 dialogue"
    "里已经出现过的台词/旁白重复或互相包含——独白是观众听不到的心里话，不是把已经说过的"
    "话再念一遍；character_name 必须是给定人物谱中的真实角色，window_index 必须是给定"
    "候选静默窗口的下标（不要自己编造秒数）。"
)


@dataclass(frozen=True)
class ResolvedMusicCue:
    shot_no: int
    track_id: str


@dataclass(frozen=True)
class ResolvedTeaserClip:
    """``end_s`` 不再是字段：片长固定为 ``plan_schema.TEASER_CLIP_LENGTH_S``，
    需要结束秒数的地方（``teaser.py``/``apply.py``）现算
    ``start_s + TEASER_CLIP_LENGTH_S``，不在这里存一份可能与常量脱节的副本。"""
    shot_no: int
    start_s: float
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
        "rules_version": _PLAN_RULES_VERSION,
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


def _music_semantic_errors(
    draft: EnhancementPlanDraft, *, context: EpisodeContext, library: MusicLibrary | None, music_on: bool,
) -> list[str]:
    if library is None:
        return []
    item_valid, item_dropped = plan_validate.validate_music_cues(draft.music_cues, context=context, library=library)
    errors = [f"配乐：{d['reason']}" for d in item_dropped]
    section_valid, section_dropped, first_gap = plan_validate.validate_music_sections(item_valid, context=context)
    errors.extend(f"配乐：{d['reason']}" for d in section_dropped)
    if first_gap:
        errors.append(f"配乐：{first_gap}")
    if music_on and not section_valid:
        errors.append("配乐：开关已开启且曲库可用，请至少给出一条可用的配乐换曲点（track_id 需逐字取自曲库清单）")
    return errors


def _teaser_semantic_errors(draft: EnhancementPlanDraft, *, context: EpisodeContext, teaser_on: bool) -> list[str]:
    valid_clips, dropped_clips = plan_validate.validate_teaser_clips(draft.teaser_clips, context=context)
    errors = [f"预告：{d['reason']}" for d in dropped_clips]
    if not valid_clips:
        if teaser_on:
            errors.append(
                f"预告：开关已开启，请至少给出 {plan_validate.TEASER_CLIP_COUNT_MIN}-"
                f"{plan_validate.TEASER_CLIP_COUNT_MAX} 段可用的预告片段",
            )
        return errors
    total = plan_validate.teaser_total_duration_s(valid_clips)
    if not (plan_validate.TEASER_TOTAL_MIN_S <= total <= plan_validate.TEASER_TOTAL_MAX_S):
        errors.append(f"预告：{len(valid_clips)} 段总长 {total:.1f}s，目标区间 8-12s")
    if not (plan_validate.TEASER_CLIP_COUNT_MIN <= len(valid_clips) <= plan_validate.TEASER_CLIP_COUNT_MAX):
        errors.append(
            f"预告：{len(valid_clips)} 段，目标区间 "
            f"{plan_validate.TEASER_CLIP_COUNT_MIN}-{plan_validate.TEASER_CLIP_COUNT_MAX} 段",
        )
    return errors


def _monologue_semantic_errors(
    draft: EnhancementPlanDraft, *, context: EpisodeContext, windows: list[Interval], monologue_on: bool,
) -> list[str]:
    valid_lines, dropped_lines = plan_validate.validate_monologue_lines(draft.monologue_lines, context=context, windows=windows)
    errors = [f"独白：{d['reason']}" for d in dropped_lines]
    if not valid_lines:
        if monologue_on:
            errors.append(
                f"独白：开关已开启，请至少给出 {plan_validate.MONOLOGUE_LINE_COUNT_MIN}-"
                f"{plan_validate.MONOLOGUE_LINE_COUNT_MAX} 句可用的独白台词",
            )
        return errors
    if not (plan_validate.MONOLOGUE_LINE_COUNT_MIN <= len(valid_lines) <= plan_validate.MONOLOGUE_LINE_COUNT_MAX):
        errors.append(
            f"独白：{len(valid_lines)} 句，目标区间 "
            f"{plan_validate.MONOLOGUE_LINE_COUNT_MIN}-{plan_validate.MONOLOGUE_LINE_COUNT_MAX} 句",
        )
    return errors


def _semantic_errors(
    draft: EnhancementPlanDraft, *, context: EpisodeContext, library: MusicLibrary | None,
    windows: list[Interval], switches: tuple[bool, bool, bool],
) -> list[str]:
    """三项开关开启却给出零个可用条目也算语义错误（触发重试）——「空集合不等于
    无需检查」，不能因为模型什么都没给就跳过范围/覆盖率校验（见各 ``_*_semantic_
    errors`` 里 ``if $x_on and not valid`` 分支）。开关关闭时不强求该项非空。"""
    music_on, teaser_on, monologue_on = switches
    errors = _music_semantic_errors(draft, context=context, library=library, music_on=music_on)
    errors += _teaser_semantic_errors(draft, context=context, teaser_on=teaser_on)
    errors += _monologue_semantic_errors(draft, context=context, windows=windows, monologue_on=monologue_on)
    return errors


async def _call_model(
    context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval], episode_id: str, fingerprint: str,
    switches: tuple[bool, bool, bool],
) -> EnhancementPlanDraft:
    payload = _task_payload(context, library, windows)
    validate = _amnesty_validate(
        lambda draft: _semantic_errors(draft, context=context, library=library, windows=windows, switches=switches),
    )
    return await model_gateway.chat_structured(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
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


def _resolve_music(
    draft: EnhancementPlanDraft, *, context: EpisodeContext, library: MusicLibrary | None,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """先按单条判据核验（段号存在、曲目存在于曲库、无重复段号），再把存活的
    换曲点当分段起点核验最短情绪段时长——两层核验的丢弃原因都要进 ``dropped``，
    与 ``_semantic_errors`` 触发重试用的是同一份 ``plan_validate`` 判据函数。
    第二层丢弃某个换曲点后，前一个存活换曲点的曲子据此在最终时间轴上延续
    播放到下一个存活换曲点（展开逻辑见 ``app.final_edit_enhance.apply``/
    ``music_runs.expand_sparse_cues``），不需要在这里另外处理。"""
    if library is None:
        return [], []
    item_valid, item_dropped = plan_validate.validate_music_cues(draft.music_cues, context=context, library=library)
    section_valid, section_dropped, _first_gap = plan_validate.validate_music_sections(item_valid, context=context)
    dropped = [{"feature": "music_bed", **d} for d in item_dropped]
    dropped += [{"feature": "music_bed", **d} for d in section_dropped]
    return section_valid, dropped


def _resolve(draft: EnhancementPlanDraft, *, context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval]) -> EnhancementPlan:
    music_valid, dropped = _resolve_music(draft, context=context, library=library)
    teaser_valid, teaser_total, teaser_dropped = _resolve_teaser(draft, context=context)
    dropped = dropped + teaser_dropped
    monologue_lines, monologue_dropped = _resolve_monologue(draft, context=context, windows=windows)
    dropped = dropped + monologue_dropped
    return EnhancementPlan(
        music_cues=tuple(ResolvedMusicCue(c.shot_no, c.track_id) for c in music_valid),
        teaser_clips=tuple(ResolvedTeaserClip(c.shot_no, c.start_s, c.reason) for c in teaser_valid),
        monologue_lines=tuple(monologue_lines),
        dropped=tuple(dropped),
        teaser_total_duration_s=teaser_total,
    )


async def generate_plan(
    *, context: EpisodeContext, library: MusicLibrary | None, windows: list[Interval], episode_id: str, fingerprint: str,
    switches: tuple[bool, bool, bool],
) -> EnhancementPlan:
    draft = await _call_model(context, library, windows, episode_id, fingerprint, switches)
    return _resolve(draft, context=context, library=library, windows=windows)
