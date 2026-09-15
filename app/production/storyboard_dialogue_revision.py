"""2.x 镜头台词的人工修订：原文句子被视频供应商合规拒收、用户拍板改措辞时的唯一入口。

修订只改段落合同 ``dialogue[].line`` 与按模板重新展开的 ``prompt_text``；原句留在 ``revised_from`` 供溯源。
提交断言（storyboard_identity_submission）按原句核验来源、按修订句核验提示词展开——不放松任何一条已有检查。
2026-09-15 龙猫出爪第 2 集镜 12：「那根越来越粗了」被 InputTextSensitiveContentDetected 三次一致拒收，
改画面描述的 override 救不了，台词本身要改成「那根线越来越粗了」。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.production.storyboard_speech_render import render_segment_speech, speaker_names

REVISED_FROM = "revised_from"
REVISION_REASON = "revision_reason"


def has_revisions(segment: dict) -> bool:
    return any(str(line.get(REVISED_FROM) or "") for line in segment.get("dialogue") or [])


def revise_segment_dialogue(segment: dict, revisions: dict[str, str], *, reason: str) -> dict:
    """按 utterance_id 改句，原句（或更早的原句）留在 revised_from；有模板就按段方言重新展开 prompt_text。"""
    result = deepcopy(segment)
    by_id = {str(line.get("utterance_id") or ""): line for line in result.get("dialogue") or []}
    for utterance_id, new_line in revisions.items():
        line = by_id.get(utterance_id)
        if line is None:
            raise ValueError(f"台词修订指向不存在的 utterance_id「{utterance_id}」")
        text = str(new_line or "").strip()
        if not text:
            raise ValueError(f"台词「{utterance_id}」的修订句不能为空")
        if text == str(line.get("line") or ""):
            continue
        line.setdefault(REVISED_FROM, str(line.get("line") or ""))
        line["line"] = text
        line[REVISION_REASON] = str(reason or "").strip() or "人工修订"
    if result.get("speech_template"):
        render_segment_speech(result, dialect=str(result.get("speech_dialect") or ""))
    return result


def source_faithful_copy(segment: dict) -> dict:
    """把修订句换回原句并重新展开，供逐字溯源类检查按原文核验。"""
    result = deepcopy(segment)
    for line in result.get("dialogue") or []:
        if str(line.get(REVISED_FROM) or ""):
            line["line"] = line[REVISED_FROM]
    if result.get("speech_template"):
        render_segment_speech(result, dialect=str(result.get("speech_dialect") or ""))
    return result


def revision_errors(segment: dict) -> list[str]:
    """修订段自身的一致性：只支持带 speech_template 的段；prompt_text 必须正好是模板按修订句的展开。"""
    if not has_revisions(segment):
        return []
    if not segment.get("speech_template"):
        return ["本段没有台词模板（旧产物），不支持台词修订，请重新生成本段分镜"]
    expected = render_segment_speech(deepcopy(segment), dialect=str(segment.get("speech_dialect") or ""))
    if str(expected.get("prompt_text") or "") != str(segment.get("prompt_text") or ""):
        return ["修订后的提示词不是模板按修订句的展开结果，请只改台词、不要手改提示词正文"]
    return []


def revisions_from_dialogues(segment: dict, dialogues: list[dict[str, Any]]) -> dict[str, str]:
    """把编辑后的 shots.dialogues[]（speaker/line，按顺序）对回段落 dialogue[]：条数与发声者必须一致，只允许改句子。"""
    lines = list(segment.get("dialogue") or [])
    if len(lines) != len(dialogues):
        raise ValueError(f"台词条数与段落合同不一致（合同 {len(lines)} 条，提交 {len(dialogues)} 条）：本入口只改措辞，不增删台词")
    names = speaker_names(segment)
    revisions: dict[str, str] = {}
    for index, (line, edited) in enumerate(zip(lines, dialogues)):
        expected = names.get(str(line.get("speaker_identity_id") or ""), str(line.get("speaker_identity_id") or ""))
        if str(edited.get("speaker") or "").strip() != expected:
            raise ValueError(f"dialogue[{index}] 的发声者「{edited.get('speaker')}」与段落合同「{expected}」不同：本入口只改措辞，不改发声者")
        text = str(edited.get("line") or "").strip()
        if text != str(line.get("line") or ""):
            revisions[str(line.get("utterance_id") or "")] = text
    return revisions
