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


def already_delivered_dialogue_rule(delivered: list[tuple[int, str, str]]) -> str:
    """task_payload["rules"] 里对应的正面陈述；本集到目前为止没有已交付台词
    时如实说明，不假装存在一份清单。"""
    if not delivered:
        return (
            "already_delivered_dialogue 目前为空——本集到这一段为止还没有任何已"
            "交付的台词，不需要比对。"
        )
    return (
        "already_delivered_dialogue 列出了前面各段已经交付的台词"
        "（speaker_identity_id/line/segment_no）：本段任何角色的台词如果与其中"
        "某一条完全相同，说明这句话已经说过了，请把这一处改写成反应、动作或"
        "画面呼应，不要原样再说一遍。"
    )


def repeated_delivery_errors(
    delivered: list[tuple[int, str, str]],
    current: list[tuple[str, str]],
    *,
    current_segment_no: int,
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
    return errors
