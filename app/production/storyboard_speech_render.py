"""从台词合同生成唯一声道标签；新分镜用占位符保留模型安排的发声时机。"""
import re
from typing import Any

from app import textmatch
from app.production.storyboard_identity_contract import effective_delivery_kind

SPEECH_TOKEN = re.compile(r"\{\{speech:([A-Za-z0-9_-]+)\}\}")


def speaker_names(segment: dict) -> dict[str, str]:
    names = {"旁白": "旁白"}
    for entry in (segment.get("resources") or {}).get("characters") or []:
        identity = str(entry.get("identity_id") or "")
        names[identity] = str(entry.get("display_name") or identity.split(":", 1)[-1])
    return names


def rendered_utterance(line: dict, names: dict[str, str], *, dialect: str) -> str:
    speaker = names.get(str(line.get("speaker_identity_id") or ""), str(line.get("speaker_identity_id") or ""))
    kind = effective_delivery_kind(line)
    label = {"spoken_dialogue": "画内对白", "offscreen_dialogue": "人物画外对白", "inner_monologue": "内心独白", "narration": "旁白"}[kind]
    # H3 的外层字段保持原方言，音频标签同样确定性生成，避免英文标签被漏改。
    if dialect == "minimax_h3_native_fields":
        label = {"spoken_dialogue": "on-screen dialogue", "offscreen_dialogue": "off-screen voiceover", "inner_monologue": "inner monologue", "narration": "narration"}[kind]
    mouth = "发声者开口，其他可见人物不跟随口型" if kind == "spoken_dialogue" else "画面人物不随此句张嘴"
    return f"{label}（{speaker}）：“{line.get('line') or ''}”（{mouth}）"


def speech_template_errors(segment: dict, *, require_tokens: bool) -> list[str]:
    prompt = str(segment.get("prompt_text") or "")
    lines = segment.get("dialogue") or []
    tokens = SPEECH_TOKEN.findall(prompt)
    if not require_tokens and not tokens:
        return explicit_prompt_speaker_errors(segment)
    ids = [str(line.get("utterance_id") or "") for line in lines]
    errors = []
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", identity) for identity in ids) or len(set(ids)) != len(ids):
        errors.append("每条台词须有唯一 utterance_id（如 U01），包含字母、数字、下划线或连字符")
    if sorted(tokens) != sorted(ids):
        errors.append("prompt_text 须在每句发声时机写一次 {{speech:utterance_id}}，与 dialogue[] 一一对应")
    if any(str(line.get("line") or "") in prompt for line in lines if line.get("line")):
        errors.append("台词原话保存在 dialogue[].line；prompt_text 对应位置使用 speech 占位符，防止同一句发声两次")
    return errors


def render_segment_speech(segment: dict, *, dialect: str) -> dict:
    """占位符按合同一次性展开，并保留模板供以后修订与审计。"""
    template = str(segment.get("speech_template") or segment.get("prompt_text") or "")
    if not SPEECH_TOKEN.search(template):
        return segment
    names = speaker_names(segment)
    by_id = {line["utterance_id"]: rendered_utterance(line, names, dialect=dialect) for line in segment.get("dialogue") or []}
    segment["speech_template"] = template
    segment["speech_dialect"] = dialect
    segment["prompt_text"] = SPEECH_TOKEN.sub(lambda m: by_id[m.group(1)], template)
    return segment


def explicit_prompt_speaker_errors(segment: dict) -> list[str]:
    """旧产物仅检查可证明冲突的显式声道标签；不靠自由文本文意猜人。"""
    prompt = str(segment.get("prompt_text") or "")
    labels = re.findall(r'(?:画外音|画内对白|人物画外对白|内心独白|旁白|on-screen dialogue|off-screen voiceover|inner monologue|narration)[（(]([^）)]+)[）)]\s*[：:]\s*[「“『"]([^」”』"]+)[」”』"]', prompt)
    names = speaker_names(segment)
    errors = []
    for line in segment.get("dialogue") or []:
        expected = names.get(str(line.get("speaker_identity_id") or ""), str(line.get("speaker_identity_id") or ""))
        for actual, spoken in labels:
            if textmatch.condense(spoken) == textmatch.condense(str(line.get("line") or "")) and actual != expected:
                errors.append(f"台词『{spoken[:20]}』的提示词发声者「{actual}」与台词合同「{expected}」不同，请修订该片段")
    if segment.get("speech_template"):
        candidate: dict[str, Any] = dict(segment)
        render_segment_speech(candidate, dialect=str(segment.get("speech_dialect") or ""))
        if candidate["prompt_text"] != prompt:
            errors.append("提示词与已保存的发声模板/台词合同不同，请重新生成该片段的提示词")
    return errors


def attach_quote_provenance(segment: dict) -> None:
    """以来源段号和唯一原话匹配补充引用偏移；重复原话有歧义时要求模型给 quote_id。"""
    for line in segment.get("dialogue") or []:
        candidates = [q for q in segment.get("required_dialogue") or [] if q.get("source_segment_index") == line.get("source_segment_index") and textmatch.condense(str(q.get("text") or "")) == textmatch.condense(str(line.get("line") or ""))]
        if line.get("source_quote_id"):
            candidates = [q for q in candidates if q.get("quote_id") == line["source_quote_id"]]
        if len(candidates) == 1:
            quote = candidates[0]
            line.update(source_quote_id=quote["quote_id"], source_start=quote.get("source_start", -1), source_end=quote.get("source_end", -1))
            line["attribution_evidence"] = str(quote.get("note") or quote.get("speaker") or "原文未点名说话人")
