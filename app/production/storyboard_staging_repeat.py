"""容量拆分续段的画面去重：分镜台 2.4.1。

背景（2026-09-13 我欲封天第 1 集重跑，逐帧看成片）：镜 1–6 是同一个男孩坐在同一块
石头上抱着同一个葫芦，整整 90 秒；6 段里 4 段有葫芦特写、5 段有拉远夕阳。这 6 段是
``storyboard_capacity_normalize`` 把一段 1200 字的内心独白按口播容量拆出来的续段——
拆出来的段只继承 synopsis/beat_ids/palette，除了一个后缀没有任何「这是同一场戏的
第 N 段」的信号；阶段二逐段独立调用，第 k 段只拿到 ``previous_segment_prompt`` 全文
和最近几段的**开场机位**（``recent_camera_language``）。机位防重复被满足了——固定/
推近/拉远/横摇/仰拍都换过——但**调度**一模一样：同一个人、同一个位置、同一件道具、
同样三拍。对比第 3 集镜 9–13 那条五段链就没这个问题：三个人物，有跑来、拿烤鸡、灵气
纹路，模型有新东西可拍。所以空转只在单人独白链上发生：没有新事件时模型把同样三拍
重拍一遍，而此前没有任何规则或闸门管「续段不得重拍上一段的画面」。

与 ``storyboard_dialogue_repeat``（跨段台词去重）同一个形状：一侧是喂给模型的正面
陈述（上一段拍过哪些画面、本段要推进什么），一侧是确定性闸门兜底。判据从数据推导：

* 子镜 = ``prompt_text`` 里 ``镜头N：`` 引出的每一段描述；
* 「样板」= 在上一段与本段全部子镜里出现频率 ≥ 一半的二元组——风格句
  （「国漫3D动画电影质感的虚构室外…场景，黄昏时分，带有精致统一的光影…」）和
  人物固定描述（「十四五岁少年身形，黑发束起，身着灰布外宗修士袍」）每个子镜都有，
  它们不是调度，先剔掉，否则任何两个子镜都「相似」；
* 剔掉样板后，本段某个子镜与上一段某个子镜的二元组覆盖率 ≥ ``REPEAT_COVERAGE``
  即视为重拍同一画面；一个子镜重拍是合法的回切，**两个及以上**才阻断——那意味着
  这一段基本就是上一段再放一遍。

只对续段（synopsis 带 ``CAPACITY_SPLIT_MARKER``）生效：正常换段本来就允许回到同一
场景。

依赖边界：只依赖同层的 ``storyboard_capacity_normalize``（拿拆分标记常量）与标准库。
"""
from __future__ import annotations

import logging
import re

from app.production.storyboard_capacity_normalize import CAPACITY_SPLIT_MARKER

_LOGGER = logging.getLogger(__name__)

_SUBSHOT_RE = re.compile(r"镜头\d+[：:]\s*(.+?)(?=\n\s*镜头\d+[：:]|\n\s*全片贯穿|\n\s*参考图说明|\Z)", re.S)
_MENTION_RE = re.compile(r"@[\w:（）()·-]+")
_NON_CONTENT_RE = re.compile(r"[\s，。、；;：:！!？?“”\"'‘’（）()【】\[\]《》〈〉—…·.,~\-]+")
#: 剔掉样板后，本段子镜与上一段某个子镜的二元组覆盖率达到这个值即视为重拍同一画面。
#: 用 2026-09-13 的五条真实拆分链标定：第 1 集镜 1–6（逐帧确认的空转链）每段都有
#: ≥2 个子镜过线；第 3 集镜 9–13（三人有动作）没有一段过线。
REPEAT_COVERAGE = 0.5
#: 在（上一段 + 本段）的子镜里出现频率达到这个比例的二元组算样板。
_BOILERPLATE_RATIO = 0.5
#: 至少这么多个子镜重拍才阻断；单个子镜回到同一画面是合法的回切。
_MIN_REPEATED_SUBSHOTS = 2


def is_continuation(synopsis: str) -> bool:
    """本段是不是容量拆分出来的续段——标记由 ``_split_one_segment`` 写入，不是模型自报。"""
    return CAPACITY_SPLIT_MARKER in (synopsis or "")


def sub_shots(prompt_text: str) -> list[str]:
    """``prompt_text`` 里 ``镜头N：`` 引出的每一段描述（原文，未归一化）。"""
    return [m.group(1).strip() for m in _SUBSHOT_RE.finditer(prompt_text or "")]


def _content(text: str, drop_phrases: list[str]) -> str:
    """去掉 @参考图/@人物 引用、已知的外观/场景固定描述与标点空白，只留调度描述的字。

    ``drop_phrases`` 是 asset_manifest 注入提示词的人物外观句与场景描述句（模型被要求
    逐字照抄进每个子镜）：它们本来就不是调度，先按已知文本剥掉，比事后靠出现频率猜
    稳——子镜只有五六个时，一句只在两个子镜里出现的外观描述频率够不上「样板」，却能
    让「猛地站起把葫芦举过头顶」这种全新动作被判成 59% 相似（合成用例实测）。
    """
    plain = _MENTION_RE.sub("", text or "")
    for phrase in sorted((p for p in drop_phrases if p), key=len, reverse=True):
        plain = plain.replace(phrase, "")
    return _NON_CONTENT_RE.sub("", plain)


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _boilerplate(beats: list[set[str]]) -> set[str]:
    """出现在 ≥ 一半子镜里的二元组：风格句与人物固定描述，不是调度。"""
    if not beats:
        return set()
    counts: dict[str, int] = {}
    for beat in beats:
        for bigram in beat:
            counts[bigram] = counts.get(bigram, 0) + 1
    floor = max(2, int(len(beats) * _BOILERPLATE_RATIO + 0.999))
    return {bigram for bigram, n in counts.items() if n >= floor}


def repeated_staging(
    previous: list[str], current: list[str], *, drop_phrases: list[str] = (),  # type: ignore[assignment]
) -> list[tuple[int, int, float]]:
    """本段哪些子镜重拍了上一段的哪个子镜：``(本段子镜序号, 上一段子镜序号, 覆盖率)``，
    序号从 1 起。每个本段子镜只报覆盖率最高的那一个。"""
    prev_beats = [_bigrams(_content(text, list(drop_phrases))) for text in previous]
    cur_beats = [_bigrams(_content(text, list(drop_phrases))) for text in current]
    boilerplate = _boilerplate(prev_beats + cur_beats)
    prev_beats = [beat - boilerplate for beat in prev_beats]
    hits: list[tuple[int, int, float]] = []
    for cur_index, beat in enumerate(cur_beats, start=1):
        beat = beat - boilerplate
        if len(beat) < 8:  # 剔掉样板后几乎没剩下调度描述的子镜，不参与比较
            continue
        best = max(
            ((len(beat & prev) / len(beat), prev_index) for prev_index, prev in enumerate(prev_beats, start=1) if prev),
            default=(0.0, 0),
        )
        if best[0] >= REPEAT_COVERAGE:
            hits.append((cur_index, best[1], round(best[0], 2)))
    return hits


def chain_prompt_texts(plans: list, drafts_by_segment_no: dict, segment_no: int) -> list[tuple[int, str]]:
    """本段所属续段链里、排在本段之前的各段 ``(segment_no, prompt_text)``，链头在前。

    从本段往前走：只要上一段还是同一条链（本段是续段，且上一段要么是链头、要么也是
    续段），就把它收进来。90 秒的单调是沿整条链累积的——第 1 集镜 3 的葫芦特写重的是
    镜 2，镜 6 的拉远夕阳重的是镜 1/2/4——两两相邻比每次只命中 1 个子镜，全部放行；
    对整条链比才看得见。
    """
    by_no = {plan.segment_no: plan for plan in plans}
    chain: list[tuple[int, str]] = []
    no = segment_no
    while is_continuation(getattr(by_no.get(no), "synopsis", "") or "") and (no - 1) in drafts_by_segment_no:
        no -= 1
        chain.append((no, drafts_by_segment_no[no].prompt_text))
        if not is_continuation(getattr(by_no.get(no), "synopsis", "") or ""):
            break  # 到链头
    chain.reverse()
    return chain


def repeated_staging_errors(
    chain: list[tuple[int, str]], current_prompt_text: str, *, current_segment_no: int, synopsis: str,
    drop_phrases: list[str],
) -> list[str]:
    """续段有 ≥2 个子镜重拍链内已出现过的画面时阻断，报出是哪几个、重了哪一段的哪个。

    ``drop_phrases`` 必传：本段 asset_manifest 里人物的 ``appearance`` 与场景的
    ``scene_canonical``（见 ``_content``）。不留默认值——漏传会让判据悄悄退化成只靠
    频率猜样板。"""
    if not is_continuation(synopsis) or not chain:
        return []
    previous: list[str] = []
    owner: list[int] = []
    for no, text in chain:
        beats = sub_shots(text)
        previous.extend(beats)
        owner.extend([no] * len(beats))
    hits = repeated_staging(previous, sub_shots(current_prompt_text), drop_phrases=drop_phrases)
    if len(hits) < _MIN_REPEATED_SUBSHOTS:
        return []
    detail = "、".join(f"镜头{cur}≈第{owner[prev - 1]}段（{cov:.0%}）" for cur, prev, cov in hits)
    head = chain[0][0]
    return [
        f"第 {current_segment_no} 段是第 {head} 段起同一场戏的续段，但 {len(hits)} 个子镜重拍了这条链里"
        f"已经出现过的画面：{detail}。续段要推进人物的身体动作、位置、视线对象或与道具的互动，"
        "让观众看到时间在往前走；回到已出现过的画面最多用一个子镜。请把重拍的子镜改成新的调度"
    ]


def canonical_phrases(payload: dict) -> list[str]:
    """asset_manifest 注入提示词的固定描述：人物 ``appearance``、场景 ``scene_canonical``。"""
    manifest = payload.get("asset_manifest") or {}
    phrases = [str(c.get("appearance") or "") for c in manifest.get("characters") or []]
    phrases += [str(sc.get("scene_canonical") or "") for sc in manifest.get("scenes") or []]
    return [x for x in phrases if x]


def staging_continuation_rule(previous_prompt_text: str, *, previous_segment_no: int, synopsis: str) -> str | None:
    """喂给续段的正面陈述：上一段拍过哪些画面、本段该怎么推进。不是续段时返回 None。"""
    if not is_continuation(synopsis) or not previous_prompt_text:
        return None
    beats = sub_shots(previous_prompt_text)
    if not beats:
        return None
    listed = "；".join(f"镜头{i}：{_MENTION_RE.sub('', text)[:60]}" for i, text in enumerate(beats, start=1))
    return (
        f"本段是第 {previous_segment_no} 段同一场戏的续段（台词按口播容量拆到本段，人物、场景、光影都"
        f"不变）。上一段已经拍过这些画面：{listed}。本段的每个子镜都要给观众新的东西看——人物换一个"
        "身体动作、挪一个位置、把视线投向新的对象，或与道具发生新的互动；这些变化要能从原文本段的"
        "情绪推进里找到依据。回到上一段出现过的画面（同一件道具的特写、同一个方向的全景）最多"
        "只用一个子镜，且不能是开场子镜。"
    )


class StagingSoftGate:
    """前 ``hard_attempts`` 次校验把画面重复当阻断，之后降级为告警并留痕。

    这是质量类规则，不是保真类（台词/归属那些）：判据只在五条真实拆分链上标定过，
    一次误判会让整集分镜失败、损失一次十几分钟的生成。给模型两次带着明确指引的
    语义重试机会（``semantic_retry_limit``），仍改不出来就放行并打
    ``[STORYBOARD_STAGING_REPEAT][未拦截]``，与 continuity_memo 里布局变化引文找不到
    时「记告警日志供观测、不拦整集」是同一条取舍。``model_gateway.chat_structured``
    的 validate 回调拿不到尝试序号，所以由每段各建一个实例自己数：格式修复不会调
    validate，每次调用就是一次语义尝试。
    """

    def __init__(self, *, hard_attempts: int, segment_no: int) -> None:
        self.hard_attempts = hard_attempts
        self.segment_no = segment_no
        self.calls = 0

    def filter(self, errors: list[str]) -> list[str]:
        self.calls += 1
        if not errors or self.calls <= self.hard_attempts:
            return errors
        for error in errors:
            _LOGGER.warning("[STORYBOARD_STAGING_REPEAT][未拦截] 第 %s 段语义重试用尽后仍重拍，放行：%s",
                            self.segment_no, error[:200])
        return []
