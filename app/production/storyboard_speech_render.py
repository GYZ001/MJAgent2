"""从台词合同生成唯一声道标签；新分镜用占位符保留模型安排的发声时机。"""
import logging
import re
from typing import Any

from app import textmatch
from app.production.storyboard_identity_contract import effective_delivery_kind

SPEECH_TOKEN = re.compile(r"\{\{speech:([A-Za-z0-9_-]+)\}\}")

log = logging.getLogger(__name__)


def remove_draft_utterance(draft: Any, line: Any) -> None:
    """删除一条发声时同步清理其模板位置，保留其它声音和镜头动作。"""
    token = "{{speech:" + str(getattr(line, "utterance_id", "")) + "}}"
    for field in ("prompt_text", "speech_template"):
        prompt = getattr(draft, field, "")
        if not prompt:
            continue
        if token in prompt:
            setattr(draft, field, prompt.replace(token, ""))
        elif not SPEECH_TOKEN.search(prompt) and line.line:
            quoted = r'[「“『"]' + re.escape(line.line) + r'[」”』"]'
            prompt = re.sub(r"(?:画外音（[^）]*）|旁白)\s*[：:]?\s*" + quoted + r"[。；;，,]?", "", prompt)
            prompt = re.sub(quoted + r"[。；;，,]?", "", prompt).replace(line.line, "")
            setattr(draft, field, re.sub(r"(?:[；;]\s*){2,}", "；", prompt))


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
    if dialect == "minimax_h3_native_fields":
        return _h3_utterance(line, names, speaker=speaker, kind=kind)
    mouth = "发声者开口，其他可见人物不跟随口型" if kind == "spoken_dialogue" else "画面人物嘴唇闭合无张合动作"
    sentences = re.findall(r".*?(?:[。！？!?]+|[.]+(?=\s|$)|$)", str(line.get("line") or ""), re.S)
    quoted = "".join(f"“{sentence}”" for sentence in sentences if sentence)
    return f"{label}（{speaker}）：{quoted}（{mouth}）"


def _h3_utterance(line: dict, names: dict, *, speaker: str, kind: str) -> str:
    """保留 H3 既有稳定 S 编号、<d> 原话块和英语描述合同。"""
    number = sorted(names).index(str(line["speaker_identity_id"])) + 1
    delivery = {"spoken_dialogue": "says", "offscreen_dialogue": "says in an off-screen voiceover",
                "inner_monologue": "says in an inner-monologue voiceover", "narration": "narrates in an off-screen voiceover"}[kind]
    mouth = "Only the speaker's lips move" if kind == "spoken_dialogue" else "All on-screen lips remain fully closed with no movement"
    return f"(S{number}, {speaker}) {delivery}: <d>[Chinese] {line.get('line') or ''}</d>. {mouth}."


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


def _pair_label(line_text: str, labels: list[tuple[str, str]], all_lines: list[dict]) -> str | None:
    """给一条台词找出它在提示词里唯一对应的声道标签；配不唯一就返回 None。

    2026-09-19 真实误报（ERR-20260919-5a4c99，龙猫出爪 EP6 段 9）：原实现直接拿
    ``condense`` 后的文本两两相等来配对，而 ``condense`` 会把标点一起去掉——
    一问一答复述同一个词时，「……八折的？」（周晚）与「八折的。」（小李）归一后都是
    「八折的」，于是两句交叉配对、双双报冲突，而数据本身完全正确。这类台词在对话戏里
    很常见，全库当时有 3 段中招（另两段是「善」关羽/刘备、「签了」豪尔赫/里奥）。

    误报比漏报贵得多：漏掉一个发声者写错，人在成片里听得出来；误报直接阻断正确产出，
    而且按提示改不动——数据本来就是对的。所以配不唯一时不判。

    顺序：① 原文精确匹配（保留标点，一步分开问句与答句）；② 匹配不上再用 condense
    兜住真排版差异（全角半角、多余空格）；③ 归一匹配必须**双向唯一**——这条台词只命中
    一个标签，且那个标签也只对应这一条台词，否则算歧义，不判。
    """
    exact = [actual for actual, spoken in labels if spoken == line_text]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # 同一句在提示词里出现多次，无从判断说的是哪一次
    target = textmatch.condense(line_text)
    if not target:
        return None
    loose = [(actual, spoken) for actual, spoken in labels if textmatch.condense(spoken) == target]
    if len(loose) != 1:
        if loose:
            log.info("发声者校验跳过：台词 %r 归一后命中 %d 个声道标签，无法唯一配对", line_text[:20], len(loose))
        return None
    back = [one for one in all_lines if textmatch.condense(str(one.get("line") or "")) == target]
    if len(back) != 1:
        log.info("发声者校验跳过：归一文本 %r 对应 %d 条台词，无法唯一配对", target[:20], len(back))
        return None
    return loose[0][0]


def explicit_prompt_speaker_errors(segment: dict) -> list[str]:
    """校验提示词里的发声者与台词合同是否一致。

    **有 speech_template 时只走模板判据，不做文本配对**（2026-09-19）：模板判据是
    「按合同重新展开一次、与成品逐字比对」，任何发声者错位都会让展开结果对不上，
    它对这件事是完备的。再叠一层文本配对不提供额外保证，只贡献歧义误报——
    ERR-20260919-5a4c99 就是这么来的：两个判据同时跑，弱的那个先报错，而数据本身
    完全正确。这是「有的门禁多余」的具体一处，不是判据不够。

    没有模板的旧产物才退到显式声道标签的文本配对（见 _pair_label 的双向唯一规则）。
    """
    prompt = str(segment.get("prompt_text") or "")
    if segment.get("speech_template"):
        template_errors = speech_template_errors(
            dict(segment, prompt_text=segment["speech_template"]), require_tokens=True)
        if template_errors:
            return template_errors
        candidate: dict[str, Any] = dict(segment)
        render_segment_speech(candidate, dialect=str(segment.get("speech_dialect") or ""))
        if candidate["prompt_text"] != prompt:
            return ["提示词与已保存的发声模板/台词合同不同，请重新生成该片段的提示词"]
        return []
    labels = re.findall(r'(?:画外音|画内对白|人物画外对白|内心独白|旁白|on-screen dialogue|off-screen voiceover|inner monologue|narration)[（(]([^）)]+)[）)]\s*[：:]\s*[「“『"]([^」”』"]+)[」”』"]', prompt)
    names = speaker_names(segment)
    errors = []
    all_lines = list(segment.get("dialogue") or [])
    for line in all_lines:
        expected = names.get(str(line.get("speaker_identity_id") or ""), str(line.get("speaker_identity_id") or ""))
        line_text = str(line.get("line") or "")
        actual = _pair_label(line_text, labels, all_lines)
        if actual is not None and actual != expected:
            errors.append(f"台词『{line_text[:20]}』的提示词发声者「{actual}」与台词合同「{expected}」不同，请修订该片段")
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
