"""台词说话人的确定性归属 + 画外音的可追溯性 + 结尾段台词剥除（2026-09-05 两条成片根因）。

根因复盘（B 库 7 天）：① 15 秒段里同句台词说两遍——提示词把台词写进「镜头N」又在结尾
「全片贯穿」段里重抄一遍（「台词：…」「对话清晰可闻：…」「音频为 X 说出的…」「画外音（X）：…」），
09-03 晚方言规则后归零，但代码里没有守卫；② 内心独白绑错说话人——小说体引号台词的台账
``speaker`` 留空、必保台词不传说话人，第二阶段模型只能猜；方言又鼓励把叙述句改成某个角色的
画外音（132 条画外音 35 条原文找不到，可定位的 97 条里 14 条说话人与引号旁的人名矛盾）。

三条修法都是机械规则：
- ``attribute_prose_speaker``：引号后 30 字内第一个人物谱正名/别名，其次引号前 40 字内最后一个；
  只用人物谱名字与位置，不做动词名单。
- ``strip_tail_dialogue``：结尾「全片贯穿」段里的引号台词（含其标签）直接剥掉并留痕。
- ``dialogue_speaker_errors``：必保台词带说话人时第二阶段必须一致（改提示词要模型重写，报精确错误）；
  画外音必须能追溯到本段原文句（逐字或二元组 ≥0.8），追溯不到报错；来自叙述句（原文不在引号里）的
  画外音说话人一律「旁白」，不是就确定性改成旁白并改写提示词里对应的「画外音（X）」标签。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app import textmatch

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


#: 归属证据：名字后面紧跟的「发声/反应」动词。这是正面证据（说话人行为的语言学信号），不是名单式
#: 拦截——没有证据就留空交给第二阶段，绝不按「离引号最近的名字」猜（2026-09-05 第 3 集：
#: 『……觉得虎爷声音大？』孟浩翻了个白眼——按最近名字归给孟浩，实际是虎爷在说）。
_UTTERANCE_VERB_RE = (
    r"(?:说|道|问|喊|叫|笑|哼|骂|吼|叹|答|应|念|呼|嚷|喃|想|心|暗|沉声|冷声|轻声|低声|大声|淡淡|开口"
    r"|沉吟|嘀咕|自语|嘲|喝|呢喃|咕哝|嘟囔|思忖|疑惑|惊|摇头|摇了摇头|点头|点了点头|皱眉|皱了皱眉|眯|瞪|冷冷|怒|急)"
)


def attribute_prose_speaker(segment_text: str, quote_start: int, quote_end: int, names: list[str] | set[str]) -> str:
    """小说体引号台词的说话人。引号后：名字紧接引号（允许 3 字内的标点/副词）且名字后 3 字内有发声/反应
    动词；否则引号前：名字在引号前 8 字内且以冒号引出，或名字后 4 字内有发声动词。窗口在相邻引号处截断；
    两处都没有证据就返回空串（不猜）。"""
    ordered = _sorted_names(names)
    if not ordered:
        return ""
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
    before = segment_text[max(0, quote_start - PRE_WINDOW):quote_start]
    last_quote = max((m.end() for m in _QUOTE_RE.finditer(before)), default=0)
    before = before[last_quote:]
    for name in ordered:
        if re.search(re.escape(name) + r".{0,12}[：:]\s*[「“『\"]?$", before):
            return name
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


def manifest_name_to_identity(payload: dict[str, Any]) -> dict[str, str]:
    """人物谱正名/别名 → identity_id（阶段二 dialogue[].speaker_identity_id 的取值域）。"""
    mapping: dict[str, str] = {}
    for character in (payload.get("asset_manifest") or {}).get("characters") or []:
        identity = str(character.get("identity_id") or "")
        if not identity:
            continue
        for name in [character.get("display_name"), *(character.get("aliases") or [])]:
            if name:
                mapping.setdefault(str(name).strip(), identity)
    return mapping


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


def _rewrite_offscreen_label(prompt_text: str, old_name: str, line: str) -> str:
    """把「画外音（旧名）：『台词』」这一条标签改成旁白；找不到精确形态就不动提示词。"""
    if not old_name:
        return prompt_text
    pattern = re.compile(
        r"画外音（" + re.escape(old_name) + r"）(\s*[：:]\s*[「“『\"])" + re.escape(line[:6])
    )
    return pattern.sub("画外音（" + NARRATOR + "）\\1" + line[:6], prompt_text, count=1)


def dialogue_speaker_errors(
    draft: Any, required_dialogue: list[dict[str, Any]], name_to_identity: dict[str, str], segment_source_text: str,
) -> list[str]:
    """必保台词说话人一致性（报错）、画外音可追溯性（报错）、叙述句画外音改旁白（就地修补）。"""
    errors: list[str] = []
    identity_to_name = {v: k for k, v in name_to_identity.items()}
    for item in required_dialogue:
        expected = name_to_identity.get(str(item.get("speaker") or "").strip())
        needle = textmatch.condense(str(item.get("text") or ""))
        if not expected or not needle:
            continue
        for index, line in enumerate(draft.dialogue):
            if needle not in textmatch.condense(line.line) and textmatch.condense(line.line) not in needle:
                continue
            if line.speaker_identity_id != expected and line.speaker_identity_id != NARRATOR:
                errors.append(
                    f"dialogue[{index}]『{line.line[:20]}』的说话人按原文归属应为「{item.get('speaker')}」"
                    f"（identity_id={expected}），当前写成「{line.speaker_identity_id}」；请把 dialogue[] 与 "
                    "prompt_text 里这句的说话人都改成原文归属的人"
                )
    if not segment_source_text:
        return errors
    for index, line in enumerate(draft.dialogue):
        if line.delivery != "offscreen_voice":
            continue
        traceable, in_quotes = _trace_to_source(line.line, segment_source_text)
        if not traceable:
            errors.append(
                f"dialogue[{index}] 画外音『{line.line[:24]}』在本段原文里找不到对应句子：只能用原文里的句子"
                "作画外音，删除这一条或改成原文句"
            )
            continue
        if not in_quotes and line.speaker_identity_id != NARRATOR:
            old = identity_to_name.get(line.speaker_identity_id, line.speaker_identity_id)
            draft.prompt_text = _rewrite_offscreen_label(draft.prompt_text, old, line.line)
            _LOGGER.info("[STORYBOARD_SPEAKER_REPAIR] 画外音『%s』来自叙述句，说话人「%s」改为旁白", line.line[:20], old)
            line.speaker_identity_id = NARRATOR
    return errors


def repair_draft_tail(draft: Any) -> None:
    """就地剥掉 draft.prompt_text 结尾段里重抄的台词并留痕（第二阶段校验前调用）。"""
    draft.prompt_text, removed = strip_tail_dialogue(draft.prompt_text)
    if removed:
        _LOGGER.info("[STORYBOARD_PROMPT_TAIL_REPAIR] 剥掉结尾段台词 %s", removed)
