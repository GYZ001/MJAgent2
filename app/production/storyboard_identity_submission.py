"""视频提交前的片段身份核验；检查副本，禁止修补检查悄悄修改已发布产物。"""
from copy import deepcopy
from types import SimpleNamespace

from app.production.storyboard_dialogue_attribution import dialogue_speaker_errors
from app.production.storyboard_identity_contract import identity_contract_errors
from app.production.storyboard_speech_render import explicit_prompt_speaker_errors, speaker_names
from app.production.storyboard_identity_validation import final_identity_prompt_errors, identity_schema_errors, quote_provenance_errors
from app.production.storyboard_dialogue_ledger import required_dialogue_missing_errors


def segment_submission_errors(segment: dict, *, source_text: str) -> list[str]:
    schema_errors = identity_schema_errors(segment)
    if schema_errors:
        return schema_errors
    errors = identity_contract_errors(segment, require_explicit=bool(segment.get("identity_contract_version")))
    errors.extend(required_dialogue_missing_errors(segment.get("required_dialogue") or [], [str(line.get("line") or "") for line in segment.get("dialogue") or []]))
    errors.extend(explicit_prompt_speaker_errors(segment))
    if segment.get("identity_contract_version"):
        errors.extend(final_identity_prompt_errors(segment))
        errors.extend(quote_provenance_errors(segment))
    lines = [SimpleNamespace(**dict(line, delivery=line.get("delivery") or "spoken_dialogue")) for line in deepcopy(segment.get("dialogue") or [])]
    draft = SimpleNamespace(dialogue=lines, prompt_text=str(segment.get("prompt_text") or ""))
    names = {name: identity for identity, name in speaker_names(segment).items()}
    errors.extend(dialogue_speaker_errors(draft, segment.get("required_dialogue") or [], names, source_text))
    if len(draft.dialogue) != len(lines) or draft.prompt_text != segment.get("prompt_text"):
        errors.append("画外音无法追溯到原文，请先修订本片段台词及提示词，再生成视频")
    return list(dict.fromkeys(errors))


def assert_segment_submission(segment: dict, *, source_text: str) -> None:
    errors = segment_submission_errors(segment, source_text=source_text)
    if errors:
        raise ValueError("[STORYBOARD_IDENTITY_REPAIR_REQUIRED] " + "；".join(errors) + "。请修订当前片段的发声与人物合同后重试，其他片段可继续。")
