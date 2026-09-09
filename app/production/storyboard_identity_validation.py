"""身份合同的结构、原文引用和最终图片引用校验，共用于生成与人工修订。"""
import re

from pydantic import ValidationError

from app import config, textmatch
from app.schemas.segment_identity import SegmentCharacter, SegmentDialogue


def identity_schema_errors(segment: dict) -> list[str]:
    """对 HTTP 字典和旧存档也执行与模型相同的字段检查。"""
    try:
        for character in (segment.get("resources") or {}).get("characters") or []:
            SegmentCharacter.model_validate(character)
        for line in segment.get("dialogue") or []:
            SegmentDialogue.model_validate(line)
    except (ValidationError, TypeError, AttributeError) as exc:
        return [f"片段身份字段格式不正确，请重新选择说话人与发声方式：{exc}"]
    return []


def quote_provenance_errors(segment: dict) -> list[str]:
    """必保原话以唯一原文引用对应，重复原话不靠列表顺序猜归属。"""
    required = segment.get("required_dialogue") or []
    errors = []
    for line in segment.get("dialogue") or []:
        candidates = [q for q in required if q.get("source_segment_index") == line.get("source_segment_index")
                      and textmatch.condense(str(q.get("text") or "")) == textmatch.condense(str(line.get("line") or ""))]
        quote_id = line.get("source_quote_id")
        if quote_id:
            candidates = [q for q in candidates if q.get("quote_id") == quote_id]
            if len(candidates) != 1:
                expected = [q for q in required if q.get("quote_id") == quote_id]
                detail = (
                    f"；{quote_id} 的原文段号是 {expected[0]['source_segment_index']}，原话为『{expected[0]['text']}』"
                    if len(expected) == 1 else f"；本段合法 quote_id 为 {[q.get('quote_id') for q in required]}"
                )
                errors.append(f"台词『{str(line.get('line') or '')[:20]}』的原文引用与原话、段号不一致{detail}")
        elif len(candidates) > 1:
            errors.append("重复原话需要 source_quote_id 指向唯一来源，不能按台词顺序猜测")
    for quote in required:
        if not any(
            line.get("source_segment_index") == quote.get("source_segment_index")
            and textmatch.condense(str(line.get("line") or "")) == textmatch.condense(str(quote.get("text") or ""))
            and (not line.get("source_quote_id") or line["source_quote_id"] == quote.get("quote_id"))
            for line in segment.get("dialogue") or []
        ):
            errors.append(f"必保引用 {quote.get('quote_id')} 须保留原文段 {quote.get('source_segment_index')} 的完整原话『{quote.get('text')}』，照录原文字形并保持来源编号")
    return errors


def final_identity_prompt_errors(segment: dict) -> list[str]:
    """只约束新合同：明确引用可见主体；检验展开后的供应商输入长度。"""
    prompt = str(segment.get("prompt_text") or "")
    resources = segment.get("resources") or {}
    names = {str(c.get("display_name") or str(c.get("identity_id") or "").split(":", 1)[-1])
             for c in resources.get("characters") or []
             if c.get("visibility") == "visible" and c.get("subject_kind") == "character"}
    names.update(str(s.get("display_name") or str(s.get("scene_id") or "").split(":", 1)[-1]) for s in resources.get("scenes") or [])
    unknown = sorted(set(re.findall(r"@([\w:-]+)", prompt)) - names)
    errors = [f"图片引用 @{name} 没有对应的可见角色或场景；请使用完整名称并用空格或标点分隔，群演使用独立描述" for name in unknown]
    if not prompt.strip():
        errors.append("视频提示词不能为空")
    if len(prompt) > config.PROMPT_CHAR_LIMIT:
        errors.append(f"台词展开后提示词长度 {len(prompt)} 超过上限 {config.PROMPT_CHAR_LIMIT}，请缩短镜头描述并保留台词")
    if segment.get("speech_dialect") == "minimax_h3_native_fields":
        for field in ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:"):
            if field not in prompt:
                errors.append(f"视频提示词缺少 H3 必需字段 {field}")
    return errors
