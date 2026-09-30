"""间接转述的情绪转折刺激必须出声（2026-09-30，真实回归《顾念长安》
proj_ca86b15ab7d7 EP1，只读核实见改造记录）。

背景：原文「顾屿却先开口，说她眼底那点青影藏得不太好，问她是不是又没睡好」
是间接转述——顾屿说了什么被叙述者转述出来，原文没有用引号把这句话本身写成
台词。真实产出里这句刺激只被拍成了顾屿指了指自己眼下、温念别开视线的静默
动作（``shot_contract_json`` 逐字核对：``dialogue`` 里只有房东的电话，没有
任何一条旁白/台词提到"没睡好"），``degraded_capabilities`` 为空——说明
``app.production.storyboard_beat_causality.segment_advisories`` 的
``beat_is_shot`` 判据通过了（画面确实以改写措辞呈现了这个动作），但它只能
判断证据文字有没有以改写措辞出现在 ``prompt_text`` 的画面描述里，判不出这段
转述内容有没有真的"被说出来"——视频模型不会把散文式的画面描述念出声，只有
``dialogue[]`` 里的条目才会真正配音。第二处「顾屿一边领她往里走，一边随口
说起这套房子他已经住了两年……语气很淡，却让她莫名觉得安心」整段被跳过，
同一根因。

模型提名、代码核验（CLAUDE.md「判据从数据推导」）：是否属于"间接转述、需要
出声"是语义判断（原文写的是引号台词、转述、还是纯动作/画面），代码判不出——
规划阶段由模型在提名 ``emotional_turns`` 的 stimulus 时一并标注
``stimulus_needs_voice``（见 ``storyboard_beat_sheet_schemas._AiEmotionalTurn``
字段文档、``storyboard_beat_causality.causality_beat_sheet_rules`` 的对应
条目）；本模块只核验"标了需要出声的刺激，是否真的在对应段落的 ``dialogue``
里有一条旁白逐字取自这句原文（或其中连续一截）"——纯结构性子串判据，不猜
语义、也不做关键词黑名单。

认领机制独立于 ``storyboard_beat_causality.moments_for_segment``：那边按
``turn.beat_id`` 认领（决定/转折本身所在的段），这里按 ``turn.stimulus_
beat_id`` 认领（刺激真正所在的段）——两者可能是同一段，也可能不同段（刺激
在更早的段落已经交代过，旁白必须落在那一段，不是决定/转折所在的那一段）。
两条认领线各自维护独立的 ``covered`` 集合，键都是 ``turn.beat_id``（一条
提名只有一个 beat_id，天然唯一，两个不同提名共享同一个 stimulus_beat_id 时
各自独立认领，互不影响）。

``StimulusVoiceSoftCheck``：与 ``EmotionalTurnSoftCheck`` 同一套"前
``hard_attempts`` 次当阻断打回模型重试、用尽后放行"让步策略，但放行分支
不是只写后端日志——那是 ``StagingSoftGate`` 的已知局限（CLAUDE.md「闸门
放行分支必须是产品里的可见信号」，``storyboard_beat_causality`` 模块
docstring 也记录了同一条教训）。``segment_advisories`` 在最终产物上独立
重算同一份判据（``_unvoiced_turns``），不依赖 ``StimulusVoiceSoftCheck`` 的
内部尝试计数：用尽重试后仍未出声的提名，这里仍会命中同一份判据、写进
``degraded_capabilities``（生产已可见的三处渲染 + 一处后期文字导出）。

台词容量：``segment_rule_text`` 提醒的上限复用 ``config.MAX_SPOKEN_CHARS_
PER_SHOT``——与 ``storyboard_dialogue_ledger.required_dialogue_rule`` 同一个
常量，不另起口径；规则文案本身允许"只取其中连续一截"，把控制长度的自由度
交给模型，不强制整句照搬。
"""
from __future__ import annotations

from typing import Any

from app import config, textmatch


def voice_claim_moments(segment_beat_ids: list[str], turns: list[Any], covered: set[str]) -> list[Any]:
    """本段（``plan.beat_ids``）首次认领的「需要出声」提名：按 ``turn.
    stimulus_beat_id`` 是否落在本段判断，与 ``storyboard_beat_causality.
    moments_for_segment`` 按 ``turn.beat_id`` 认领是两条独立的认领线，见
    模块 docstring。``covered`` 由调用方传入并原地更新。"""
    claimed: list[Any] = []
    for turn in turns:
        if not turn.stimulus_needs_voice:
            continue
        if turn.stimulus_beat_id in segment_beat_ids and turn.beat_id not in covered:
            claimed.append(turn)
            covered.add(turn.beat_id)
    return claimed


def segment_rule_text(turns_here: list[Any]) -> list[str]:
    """阶段二 per-segment 正面陈述：本段 ``dialogue[]`` 必须补一条旁白逐字
    取自 ``stimulus_evidence_quote``，并紧跟给出反应镜头。"""
    return [
        f"节拍 {turn.beat_id} 的刺激「{turn.stimulus_evidence_quote}」在原文里是"
        "转述/叙述、不是引号台词，只画成画面的话观众听不出说的是什么：本段 "
        "dialogue[] 里必须加一条 delivery_kind=narration 的旁白，逐字取这句"
        "原文（可以只取其中连续一截，不必整句照搬，只要能让观众听清说了"
        f"什么），紧跟着给出「{turn.turn_evidence_quote}」这个反应的镜头，不要"
        f"只拍反应、不出声；本段全部台词加起来仍不能超过 "
        f"{config.MAX_SPOKEN_CHARS_PER_SHOT} 字的 15 秒口播容量。"
        for turn in turns_here
    ]


def _dialogue_voices_quote(quote: str, dialogue: list[Any]) -> bool:
    """``dialogue`` 里是否已有一条旁白，其正文逐字是 ``quote`` 的连续子串
    （含整句相等）——不用模糊匹配，"逐字来自原文"要求的是真子串，不是相似。"""
    condensed_quote = textmatch.condense(quote)
    if not condensed_quote:
        return False
    for line in dialogue:
        if line.delivery_kind != "narration":
            continue
        condensed_line = textmatch.condense(line.line)
        if condensed_line and condensed_line in condensed_quote:
            return True
    return False


def _unvoiced_turns(turns_here: list[Any], dialogue: list[Any]) -> list[Any]:
    return [turn for turn in turns_here if not _dialogue_voices_quote(turn.stimulus_evidence_quote, dialogue)]


def voice_missing_errors(turns_here: list[Any], dialogue: list[Any]) -> list[str]:
    """blocking 判据文案，供 ``StimulusVoiceSoftCheck`` 与测试直接核对。"""
    return [
        f"节拍 {turn.beat_id} 的刺激「{turn.stimulus_evidence_quote}」标注为需要"
        "出声（stimulus_needs_voice），但 dialogue[] 里没有一条 delivery_kind="
        "narration 的旁白逐字取自这句原文（可以只取其中连续一截）：请补上这条"
        f"旁白，让观众听到触发「{turn.turn_evidence_quote}」这个反应/决定的具体内容"
        for turn in _unvoiced_turns(turns_here, dialogue)
    ]


class StimulusVoiceSoftCheck:
    """前 ``hard_attempts`` 次把缺失的必要旁白当阻断打回模型重试，用尽后
    放行（不再产生 blocking error）——与 ``EmotionalTurnSoftCheck`` 同一套
    让步策略，见模块 docstring「放行分支必须是产品里的可见信号」一节：这里
    的放行不等于问题消失，``segment_advisories`` 会在最终产物上独立发现它。
    """

    def __init__(self, *, hard_attempts: int, segment_no: int) -> None:
        self.hard_attempts = hard_attempts
        self.segment_no = segment_no
        self.calls = 0

    def filter(self, turns_here: list[Any], dialogue: list[Any]) -> list[str]:
        self.calls += 1
        problems = voice_missing_errors(turns_here, dialogue)
        if not problems or self.calls <= self.hard_attempts:
            return problems
        return []


def segment_advisories(turns_here: list[Any], dialogue: list[Any]) -> list[str]:
    """非阻断，供 ``storyboard_pack`` 合并进 ``degraded_capabilities``——判据
    与 ``StimulusVoiceSoftCheck`` 同源（``_unvoiced_turns``），独立重算。"""
    return [
        "[STORYBOARD_PACK_STIMULUS_VOICE_MISSING][未拦截] 情绪转折"
        f"「{turn.turn_evidence_quote[:40]}」的刺激「{turn.stimulus_evidence_quote[:40]}」"
        "是原文转述、不是引号台词，本段 dialogue 里没有找到逐字取自这句原文的旁白："
        "观众可能听不出触发这次转折的具体内容是什么，可在分镜台编辑本段 dialogue 补上一条旁白"
        for turn in _unvoiced_turns(turns_here, dialogue)
    ]
