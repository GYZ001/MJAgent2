"""原文发声归属与可追溯性检查。

明确角色行与台账证据优先，连续引句后的听者不能成为说话人。
身份冲突要求共同重写台词与提示词，不再单独改成旁白。
无来源且非必保的可选画外音仍会被删除，提交边界通过副本检查识别这类旧稿。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app import textmatch
from app.production.storyboard_identity_scope import scoped_name_map
from app.production.storyboard_speaker_context import dialogue_listeners, explicit_script_speaker

_LOGGER = logging.getLogger(__name__)
NARRATOR = "旁白"
_QUOTE_RE = re.compile(r"[「“『\"]([^」”』\"]{2,})[」”』\"]")
_TAIL_MARKER = "全片贯穿"
_TAG_RE = re.compile(r"\[段\d+·S\d+\]")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?…]+")
POST_WINDOW = 30
PRE_WINDOW = 40


def _sorted_names(names: list[str] | set[str]) -> list[str]:
    return sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)


#: 归属证据：名字后面紧跟的发声或内心表达动词。单纯听闻、点头等反应不构成发声证据。
#: 拦截——没有证据就留空交给第二阶段，绝不按「离引号最近的名字」猜（2026-09-05 第 3 集：
#: 『……觉得虎爷声音大？』孟浩翻了个白眼——按最近名字归给孟浩，实际是虎爷在说）。
_UTTERANCE_VERB_RE = (
    r"(?:说|道|问|喊|叫|笑|哼|骂|吼|叹|答|应|念|呼|嚷|喃|想|心|暗|沉声|冷声|轻声|低声|大声|淡淡|开口"
    r"|沉吟|嘀咕|自语|嘲|喝|呢喃|咕哝|嘟囔|思忖)"
)


def attribute_prose_speaker(segment_text: str, quote_start: int, quote_end: int, names: list[str] | set[str]) -> str:
    """小说体引号台词的说话人。引号后：名字紧接引号（允许 3 字内的标点/副词）且名字后 3 字内有发声/反应
    动词；否则引号前：名字在引号前 8 字内且以冒号引出，或名字后 4 字内有发声动词。窗口在相邻引号处截断；
    两处都没有证据就返回空串（不猜）。"""
    ordered = _sorted_names(names)
    if not ordered:
        return ""
    before = segment_text[max(0, quote_start - PRE_WINDOW):quote_start]
    last_quote = max((m.end() for m in _QUOTE_RE.finditer(before)), default=0)
    before = before[last_quote:]
    # 明确的「角色说：引句」先于引句后的动作，后者可能属于下一位说话人。
    for name in ordered:
        if re.search(re.escape(name) + r".{0,12}[：:]\s*[「“『\"]?$", before):
            return name
    after = segment_text[quote_end:quote_end + POST_WINDOW]
    cut = _QUOTE_RE.search(after)
    if cut:
        after = after[:cut.start()]
    for name in ordered:
        # quote_end 可能指向收尾引号本身（抽取偏移不含引号符号）：把引号与标点一起当作可跳过的前缀。
        # 名字之后 16 字内出现发声/反应动词才算（「孟浩盘膝坐在洞府内，皱着眉头沉吟起来」），
        # 但中途出现另一个人名就截断——动词属于后面那个人（「孟浩看着他，王腾飞冷笑道」）。
        m = re.match(r"^[」”』\"，。！？…、\s]{0,3}" + re.escape(name), after)
        if not m:
            continue
        rest = after[m.end():m.end() + 16]
        others = [rest.find(o) for o in ordered if o != name and o in rest]
        if others:
            rest = rest[:min(others)]
        if re.search(_UTTERANCE_VERB_RE, rest):
            return _explicitly_named(after[m.end():], ordered, name) or name
    for name in ordered:
        if re.search(re.escape(name) + r".{0,4}?" + _UTTERANCE_VERB_RE + r".{0,4}[「“『\"]?$", before):
            return name
    return ""



_NAMING_CLAUSE = r"(?:他|她|此人|其人|那人|这人)?\s*(?:叫|名叫|名为|便是|正是|就是|乃是|唤作|唤做)\s*"


def _explicitly_named(rest: str, ordered: list[str], token: str) -> str:
    """「少年叹了口气，他叫孟浩」：说话人按称谓匹配到「少年」，但同一句里原文紧接着点了名——
    以点名为准。称谓（少年/女子/老者）在一章里常指不止一个人，映射台把它登记成谁的别名都可能
    在另一处出错（我欲封天第 1 集：「少年」被登记为王有材别名，整段孟浩的戏被判给王有材）；
    而「他叫 X」这种点名句是原文给出的直接依据，比别名表更强。只在同一句（到句号/换行为止）内找。"""
    sentence = re.split(r"[。！？\n]", rest, maxsplit=1)[0]
    for other in ordered:
        if other != token and re.search(_NAMING_CLAUSE + re.escape(other), sentence):
            return other
    return ""


_TAIL_LINE_RE = re.compile(
    r"(?:(?:台词|对白|对话[^：:；。\n]{0,12}|画外音（[^）]*）|旁白|音频为[^「“『\"\n]{0,20}说出的)\s*[：:]?\s*)?"
    r"[「“『\"][^」”』\"]{2,}[」”』\"]"
)


def strip_tail_dialogue(prompt_text: str) -> tuple[str, list[str]]:
    """把「全片贯穿」段里的引号台词（含「台词：」「画外音（X）：」等标签）剥掉；返回新文本与被剥的句子。"""
    idx = prompt_text.rfind(_TAIL_MARKER)
    if idx < 0:
        return prompt_text, []
    head, tail = prompt_text[:idx], prompt_text[idx:]
    removed = [m.group(0) for m in _TAIL_LINE_RE.finditer(tail)]
    if not removed:
        return prompt_text, []
    cleaned = _TAIL_LINE_RE.sub("", tail)
    cleaned = re.sub(r"[；;、，]\s*(?=[；;。])", "", cleaned)
    cleaned = re.sub(r"[：:]\s*(?=[；;。\n]|$)", "", cleaned)
    cleaned = re.sub(r"(?:[；;]\s*){2,}", "；", cleaned)
    return head + cleaned, removed


def manifest_name_to_identity(payload: dict[str, Any], segment_source_indexes: list[int] | None = None) -> dict[str, str]:
    """人物谱正名/别名 → identity_id（阶段二 dialogue[].speaker_identity_id 的取值域）。"""
    return scoped_name_map(payload, segment_source_indexes)


def _source_sentences(source_text: str) -> list[str]:
    plain = _TAG_RE.sub("", source_text or "")
    return [s for s in (p.strip() for p in _SENTENCE_SPLIT_RE.split(plain)) if s]


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _trace_to_source(line: str, source_text: str) -> tuple[bool, bool]:
    """(能否追溯, 原文里是否在引号内)。逐字包含优先；否则与某个原文句二元组覆盖 ≥ KEY_LINE_PRESENT_RATIO。"""
    needle = textmatch.condense(line)
    if not needle:
        return False, False
    plain = _TAG_RE.sub("", source_text or "")
    quoted = {textmatch.condense(m.group(1)) for m in _QUOTE_RE.finditer(plain)}
    if any(needle in q or q in needle for q in quoted if q):
        return True, True
    if needle in textmatch.condense(plain):
        return True, False
    nb = _bigrams(needle)
    for sentence in _source_sentences(plain):
        sb = _bigrams(textmatch.condense(sentence))
        if nb and sb and len(nb & sb) / len(nb) >= textmatch.KEY_LINE_PRESENT_RATIO:
            return True, False
    return False, False


def _scrub_offscreen_line(prompt_text: str, line: str) -> str:
    """把被删掉的画外音从提示词里一并抹掉：带标签的整条（画外音（X）：「…」）、只剩引号的、裸文本。"""
    quoted = r"[「“『\"]" + re.escape(line) + r"[」”』\"]"
    text = re.sub(r"(?:画外音（[^）]*）|旁白)\s*[：:]?\s*" + quoted + r"[。；;，,]?", "", prompt_text)
    text = re.sub(quoted + r"[。；;，,]?", "", text)
    text = text.replace(line, "")
    return re.sub(r"(?:[；;]\s*){2,}", "；", text)


def dialogue_speaker_errors(
    draft: Any, required_dialogue: list[dict[str, Any]], name_to_identity: dict[str, str], segment_source_text: str,
) -> list[str]:
    """原文归属与发声方式冲突明确报错；只删除无来源的可选画外音。"""
    errors: list[str] = []
    identity_to_name: dict[str, str] = {}
    for name, identity in name_to_identity.items():
        identity_to_name.setdefault(identity, identity.split(":", 1)[-1] if identity.startswith("bible:") else name)
    errors.extend(_required_speaker_errors(draft, required_dialogue, name_to_identity))
    if not segment_source_text:
        return errors
    errors.extend(unattributed_quote_speaker_errors(draft, required_dialogue, identity_to_name, segment_source_text))
    dropped: list[int] = []
    for index, line in enumerate(draft.dialogue):
        if line.delivery != "offscreen_voice":
            continue
        traceable, in_quotes = _trace_to_source(line.line, segment_source_text)
        if in_quotes and line.speaker_identity_id == NARRATOR and not any(item.get("speaker") == NARRATOR and textmatch.condense(str(item.get("text") or "")) == textmatch.condense(line.line) for item in required_dialogue):
            errors.append(f"dialogue[{index}]『{line.line[:20]}』是原文人物引语，不能因说话人未知就改为旁白，请保留独立发声主体")
        if not traceable:
            # 模型把原文转述成画外音（「小胖子有家财万贯，我却一穷二白欠着债」）时，打回让它改成原文句
            # 三次仍如此、整集失败（2026-09-05 第 2 集）。画外音是可选的旁白性交代，追溯不到就删掉
            # 这一条（对白与提示词里都删），画面照常——空着诚实，转述是编造。
            if any(textmatch.condense(str(item.get("text") or "")) == textmatch.condense(line.line) for item in required_dialogue):
                errors.append(f"dialogue[{index}] 必保台词无法追溯，请核对原文来源后重写此片段")
                continue
            dropped.append(index)
            draft.prompt_text = _scrub_offscreen_line(draft.prompt_text, line.line)
            _LOGGER.info("[STORYBOARD_OFFSCREEN_DROPPED] dialogue[%s]『%s』追溯不到原文，已删除", index, line.line[:24])
            continue
        explicit = any(textmatch.condense(str(item.get("text") or "")) == textmatch.condense(line.line) and (item.get("speaker") or item.get("speaker_identity_id")) for item in required_dialogue)
        named = explicit_script_speaker(line.line, segment_source_text, list(name_to_identity))
        if named and name_to_identity[named] != line.speaker_identity_id:
            errors.append(f"dialogue[{index}] 原文角色行明确由「{named}」发声，请核对台词与提示词")
        if not in_quotes and not explicit and not named and line.speaker_identity_id != NARRATOR:
            old = identity_to_name.get(line.speaker_identity_id, line.speaker_identity_id)
            errors.append(f"dialogue[{index}]『{line.line[:20]}』缺少人物发声证据；若是叙述者讲述，请将 speaker_identity_id 和提示词共同改为旁白；若为人物自述，请提供原文发声依据。当前归属「{old}」未被自动改写")
    if dropped:
        draft.dialogue = [line for i, line in enumerate(draft.dialogue) if i not in dropped]
    return errors


_LISTENER_CUE_RE = r".{0,6}(?:听|闻)"


def _quote_span(line: str, source_text: str) -> tuple[int, int] | None:
    needle = textmatch.condense(line)
    for m in _QUOTE_RE.finditer(source_text):
        inner = textmatch.condense(m.group(1))
        if needle and inner and (needle in inner or inner in needle):
            return m.start(), m.end()
    return None


def unattributed_quote_speaker_errors(
    draft: Any, required_dialogue: list[dict[str, Any]], identity_to_name: dict[str, str], source_text: str,
) -> list[str]:
    """原文没有点名说话人的引号台词，模型不得安给在场的具名角色（2026-09-05 第 5 集：「以王腾飞师兄的
    资质……」是无名同门的议论，原文紧接着写「孟浩听着身边同门的议论」，成片却让孟浩张嘴说这句）。
    判据是原文结构：账本对这句没有说话人证据（attribute_prose_speaker 留空），而模型写了具名角色 X——
    引号后 X 紧跟听/闻类动词（X 是听者），或 X 根本不在这句前后窗口里，都算没有依据。"""
    errors: list[str] = []
    unattributed = {
        textmatch.condense(str(item.get("text") or "")) for item in required_dialogue
        if not str(item.get("speaker") or "").strip()
    }
    for index, line in enumerate(draft.dialogue):
        speaker = str(line.speaker_identity_id or "")
        name = identity_to_name.get(speaker, "")
        if not speaker or not name:
            continue
        needle = textmatch.condense(line.line)
        if not any(needle in u or u in needle for u in unattributed if u):
            continue
        span = _quote_span(line.line, source_text)
        if span is None:
            continue
        start, end = span
        # 只认「引号后紧跟 X听/闻」这一条正向证据：X 不在窗口里不算错——对话轮替、自称（「为兄」）
        # 都能合法推断出说话人（第 4 集实测按缺席打回是误伤，整集分镜台失败）。
        listener = name in dialogue_listeners(source_text, start, end, list(identity_to_name.values()))
        listener = listener or any(speaker in (item.get("excluded_speaker_identity_ids") or []) and textmatch.condense(str(item.get("text") or "")) == needle for item in required_dialogue)
        if listener:
            errors.append(
                f"dialogue[{index}]『{line.line[:20]}』原文没有点名说话人，引号后原文是「{name}听/闻……」，"
                f"{name} 是听者不是说话人，不得安给具名角色；speaker 改用本段 relevant_assets.characters 里的"
                "无名人物（entity）或 functional_extras 的 visual_entity_id；缺失时保留原文无名说话人的独立称谓并提供来源，prompt_text 与台词归属一起修正"
            )
    return errors


def repair_draft_tail(draft: Any) -> None:
    """就地剥掉 draft.prompt_text 结尾段里重抄的台词并留痕（第二阶段校验前调用）。"""
    draft.prompt_text, removed = strip_tail_dialogue(draft.prompt_text)
    if removed:
        _LOGGER.info("[STORYBOARD_PROMPT_TAIL_REPAIR] 剥掉结尾段台词 %s", removed)


def _required_speaker_errors(draft: Any, required_dialogue: list[dict], name_to_identity: dict) -> list[str]:
    """按原文引用核对发声主体，重复原话优先匹配 source_quote_id。"""
    errors: list[str] = []
    condensed = [textmatch.condense(line.line) for line in draft.dialogue]
    needles = [textmatch.condense(str(item.get("text") or "")) for item in required_dialogue]
    # 账本项与草稿台词先按逐字相等配对；只有没有精确命中的才退到包含匹配，且不再抢别人精确命中的那句。
    # 第 29 集实测：『认输……』是『上去就立刻认输。』的子串，包含匹配把两句的说话人交叉判错，三次重试整集失败。
    exact_claimed = {index for index, text in enumerate(condensed) if text in needles}
    for item, needle in zip(required_dialogue, needles):
        expected = item.get("speaker_identity_id") or name_to_identity.get(str(item.get("speaker") or "").strip())
        if not needle:
            continue
        exact = [index for index, text in enumerate(condensed) if text == needle]
        matched = exact or [
            index for index, text in enumerate(condensed)
            if index not in exact_claimed and (needle in text or text in needle)
        ]
        for index in matched:
            line = draft.dialogue[index]
            if getattr(line, "source_quote_id", "") and line.source_quote_id != item.get("quote_id"):
                continue
            if line.speaker_identity_id in (item.get("excluded_speaker_identity_ids") or []):
                errors.append(f"dialogue[{index}]『{line.line[:20]}』的「{line.speaker_identity_id}」是原文对话块的听者，不是说话人，请改为有原文依据的独立发声主体")
            if item.get("delivery_kind") and getattr(line, "delivery_kind", "") and item["delivery_kind"] != line.delivery_kind:
                errors.append(f"dialogue[{index}] 的发声类型应保持原文的 {item['delivery_kind']}")
            if expected and line.speaker_identity_id != expected:
                errors.append(
                    f"dialogue[{index}]『{line.line[:20]}』的说话人按原文归属应为「{item.get('speaker')}」"
                    f"（identity_id={expected}），当前写成「{line.speaker_identity_id}」；请把 dialogue[] 与 "
                    "prompt_text 里这句的说话人都改成原文归属的人"
                )
    return errors
