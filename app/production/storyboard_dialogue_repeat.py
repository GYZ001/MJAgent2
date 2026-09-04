"""跨段台词去重闸门：分镜台 2.4.0（见 app.production.storyboard_pack 的
STORYBOARD_PACK_VERSION changelog）。

背景（2026-09-03「橘座在上」EP1 真实回归）：阶段二逐段独立调用，第 k 段的
那次调用完全看不到第 1..k-1 段已经写了什么台词——猫跳上桌在段 4、段 6 各拍
一次，黄总抓猫在段 7/8/9 拍了三次，全集最后一句内心独白在段 4、段 6 各说一
遍，根因都是"没人告诉模型这句话之前已经说过"。本模块给第 k 段生成时喂一份
"前 k-1 段已经交付的台词"清单，同一说话人重复同一句台词时阻断重试，逼模型
把这段改写成反应、动作或画面呼应，而不是原样再演一遍。

签名说明：``delivered`` 是 3 元组 ``(segment_no, speaker_identity_id, line)``——
派单原文写的是 2 元组 ``(speaker_identity_id, line)``，但报错要求写明"在第几
段说过"，2 元组没有段号信息做不到；调用方（``storyboard_pack.
_generate_all_segment_prompts``）本就按段号累积这份清单，加一个字段不增加
调用方负担。``current``（本段自己已经生成的台词）不需要自证段号——它就是
``current_segment_no`` 本身，因此保持 2 元组。
"""
from __future__ import annotations

import re

_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
#: 归一化后短于这个长度的台词不参与比较——语气词、单字应答（"嗯""好的"）
#: 天然会在多段重复出现，不是本闸门要拦的"整句台词照搬"。
_MIN_REPEAT_CHARS = 6
#: 本段台词与后面段落必保台词的二元组覆盖率达到这个值即视为「提前说/改写后说」。
#: EP1 重跑实测：段 4 写了「跟我去公司，别出声。」，而它是段 5 必保台词「算了，跟我去
#: 公司当社畜吧……绝对不能出声，知道吗？」的压缩版（覆盖率 0.71），观众听到的是同一句
#: 话在相邻两段各说一遍；精确匹配挡不住这种改写。
_PREEMPT_BIGRAM_COVERAGE = 0.6


def _normalize(text: str) -> str:
    """去标点空白、casefold，让重复判定不被排版差异（全半角、空格）掩盖。"""
    return _NORMALIZE_RE.sub("", text or "").casefold()


def already_delivered_payload(delivered: list[tuple[int, str, str]]) -> list[dict[str, object]]:
    """``delivered`` 渲染成阶段二 task_payload["already_delivered_dialogue"]
    的形状：按段号顺序列出 speaker_identity_id/line，供模型自己核对是否重复
    （闸门本身用 ``repeated_delivery_errors`` 兜底，这份 payload 是"先给一次
    自查机会"）。
    """
    return [
        {"segment_no": segment_no, "speaker_identity_id": speaker, "line": line}
        for segment_no, speaker, line in delivered
    ]


def reserved_lines_for(
    required_by_segment_no: dict[int, list[dict[str, object]]], current_segment_no: int,
) -> list[tuple[int, str]]:
    """后面段落的必保台词 ``(segment_no, text)``：这些话由那一段原样说出，本段
    不得提前说，也不得改写后说。"""
    reserved: list[tuple[int, str]] = []
    for segment_no in sorted(required_by_segment_no):
        if segment_no <= current_segment_no:
            continue
        for item in required_by_segment_no[segment_no]:
            text = str(item.get("text") or "")
            if text:
                reserved.append((segment_no, text))
    return reserved


def reserved_dialogue_payload(reserved: list[tuple[int, str]]) -> list[dict[str, object]]:
    """``reserved`` 渲染成阶段二 task_payload["reserved_dialogue"] 的形状。"""
    return [{"segment_no": segment_no, "line": line} for segment_no, line in reserved]


def already_delivered_dialogue_rule(
    delivered: list[tuple[int, str, str]], reserved: list[tuple[int, str]],
) -> str:
    """task_payload["rules"] 里对应的正面陈述；清单为空时如实说明，不假装存在。"""
    if delivered:
        delivered_rule = (
            "already_delivered_dialogue 列出了前面各段已经交付的台词"
            "（speaker_identity_id/line/segment_no）：本段任何角色的台词如果与其中"
            "某一条完全相同，说明这句话已经说过了，请把这一处改写成反应、动作或"
            "画面呼应，不要原样再说一遍。"
        )
    else:
        delivered_rule = (
            "already_delivered_dialogue 目前为空——本集到这一段为止还没有任何已"
            "交付的台词，不需要比对。"
        )
    if reserved:
        reserved_rule = (
            "reserved_dialogue 列出了后面段落的必保台词（segment_no/line）：这些话由"
            "对应的那一段原样说出，本段不能提前说，也不能压缩或改写后说；本段只用"
            "本段范围内的原文写台词，需要铺垫时用画面、动作或反应带过。"
        )
    else:
        reserved_rule = "reserved_dialogue 为空——本段之后没有预留给后面段落的必保台词。"
    return delivered_rule + reserved_rule


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _preempts(normalized_current: str, normalized_reserved: str) -> bool:
    """本段台词是否是某句预留台词的原样/子串/改写版本（二元组覆盖率判定）。"""
    if min(len(normalized_current), len(normalized_reserved)) < _MIN_REPEAT_CHARS:
        return False
    if normalized_current in normalized_reserved or normalized_reserved in normalized_current:
        return True
    current_bigrams = _bigrams(normalized_current)
    if not current_bigrams:
        return False
    overlap = len(current_bigrams & _bigrams(normalized_reserved)) / len(current_bigrams)
    return overlap >= _PREEMPT_BIGRAM_COVERAGE


def repeated_delivery_errors(
    delivered: list[tuple[int, str, str]],
    current: list[tuple[str, str]],
    *,
    current_segment_no: int,
    reserved: list[tuple[int, str]],
) -> list[str]:
    """本段台词若与同一说话人在更早段落已交付的台词完全相同（归一化后），阻断。

    只比较"完全相同"（归一化后逐字相等），不做模糊匹配——半句改写、信息
    增量属于正常的剧情推进，不该被这道闸拦下；只有原样照搬才是真的重复。
    """
    by_speaker: dict[str, list[tuple[int, str]]] = {}
    for segment_no, speaker, line in delivered:
        normalized = _normalize(line)
        if len(normalized) < _MIN_REPEAT_CHARS:
            continue
        by_speaker.setdefault(speaker, []).append((segment_no, normalized))
    errors: list[str] = []
    for speaker, line in current:
        normalized = _normalize(line)
        if len(normalized) < _MIN_REPEAT_CHARS:
            continue
        for segment_no, prior_normalized in by_speaker.get(speaker, []):
            if normalized == prior_normalized:
                errors.append(
                    f"第 {current_segment_no} 段 {speaker} 的台词「{line}」与第 {segment_no} 段"
                    "已经交付的台词重复：这句话已经说过了，本段请改写成反应、动作或画面呼应，"
                    "不要原样再说一遍"
                )
                break
    errors.extend(_preemption_errors(current, reserved, current_segment_no=current_segment_no))
    return errors


def _preemption_errors(
    current: list[tuple[str, str]], reserved: list[tuple[int, str]], *, current_segment_no: int,
) -> list[str]:
    """本段台词若是后面段落必保台词的原样、子串或改写版本，阻断。"""
    errors: list[str] = []
    for speaker, line in current:
        normalized = _normalize(line)
        for segment_no, text in reserved:
            if _preempts(normalized, _normalize(text)):
                errors.append(
                    f"第 {current_segment_no} 段 {speaker} 的台词「{line}」是第 {segment_no} 段必保台词"
                    f"「{text}」的提前版或改写版：这句话留给第 {segment_no} 段原样说出，本段只拍本段"
                    "范围内的内容，用画面、动作或反应带过，不要提前说，也不要改写后说"
                )
                break
    return errors
