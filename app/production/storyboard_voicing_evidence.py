"""画外音归属人物时的「原文发声依据」：同一句里「人物 + 发声/心理动词 + 台词」。

背景：`dialogue_speaker_errors` 对归属人物的画外音只认两种依据——原文引号内、或剧本格式
的「说话人：台词」行；否则报「缺少人物发声证据…若为人物自述，请提供原文发声依据」。
可「提供依据」没有任何字段能满足，模型只能改旁白；遇到「精明男子……内心暗笑，心道这些
刚入外宗的弟子最好糊弄」这种无引号心理活动（2026-09-14 我欲封天第 4 集第 16 段），模型
坚持归给精明男子（这是忠实的），三次修复仍报错、整集分镜失败。这里把「依据」做成闸门
自己去原文里核的正面判据，与提示词两侧同宽。

判据（全部来自本段原文与人物谱，不含任何人名清单）：
1. 台词起始片段紧跟在一个发声/心理动词之后（允许中间有冒号、引号）；动词是汉语里
   引出言语或心理内容的封闭小类（说/道/问/答/喊/叫/骂/心道/暗道/心想/暗想/自语/喃喃…），
   不是人名黑名单；
2. 该动词所在小句里若有人物称谓，取最后一个为发声者；否则取本句第一个人物称谓
   （无主语小句沿用句首主语，「精明男子……眼见孟浩迟疑，内心暗笑，心道……」→精明男子）；
3. 单字称谓（他/她/你/我）不参与判定——准备包里存在代词别名（见记忆），它们指代不定。
"""
from __future__ import annotations

import re

from app import textmatch

_TAG_RE = re.compile(r"\[段\d+·S\d+\]")
_SENTENCE_SPLIT_RE = re.compile(r"[。！？!?…]+")
_CLAUSE_SPLIT_RE = re.compile(r"[，,；;、]")
_VOICING_VERBS = (
    "心中暗道", "心中暗想", "心中想道", "心道", "暗道", "心想", "暗想", "想道", "自语", "喃喃", "嘀咕",
    "念叨", "笑道", "冷笑道", "开口道", "低语", "说道", "问道", "答道", "喊道", "叫道", "骂道",
    "开口", "说", "道", "问", "答", "喊", "叫", "骂",
)
_VERB_RE = "|".join(re.escape(v) for v in _VOICING_VERBS)
_GAP = r"[：:，,“「『\"\s]{0,3}"
_LINE_PREFIX_CHARS = 8
_STRIP_QUOTES = "“”「」『』\"'"


def _labels(name_to_identity: dict[str, str], identity: str) -> tuple[list[str], list[str]]:
    """(该人物的称谓, 全部人物称谓)；只保留 ≥2 字的称谓，旁白不算人物。"""
    own = [n for n, i in name_to_identity.items() if i == identity and len(n) >= 2]
    everyone = [n for n, i in name_to_identity.items() if i != "旁白" and len(n) >= 2]
    return own, everyone


def _speaker_of(before: str, everyone: list[str]) -> str | None:
    """动词前文本的发声者：动词所在小句里最后一个称谓；没有就取本句第一个称谓。"""
    clause = _CLAUSE_SPLIT_RE.split(before)[-1]
    in_clause = [(clause.rfind(n), n) for n in everyone if n in clause]
    if in_clause:
        return max(in_clause)[1]
    in_sentence = [(before.find(n), n) for n in everyone if n in before]
    return min(in_sentence)[1] if in_sentence else None


_VERB_ONLY_RE = re.compile(f"(?:{_VERB_RE})")


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def voicing_evidence(line: str, identity: str, name_to_identity: dict[str, str], source_text: str) -> bool:
    """原文同一句里是否有「identity 的称谓 + 发声/心理动词 + 本台词」这种依据。

    两种命中：台词起始片段紧跟动词（「心道这些刚入外宗的弟子…」）；或这句话按二元组覆盖
    能落到某个原文句（≥ KEY_LINE_PRESENT_RATIO），该句里动词之前的发声者是 identity——
    心理活动常被改成第一人称（原文「暗道自己只有凝气一层」，台词「我只有凝气一层」，
    2026-09-14 第 5 集第 8 段），逐字紧邻对不上，但句子归属是清楚的。
    """
    own, everyone = _labels(name_to_identity, identity)
    prefix = (line or "").strip().lstrip(_STRIP_QUOTES)[:_LINE_PREFIX_CHARS]
    if not own or len(prefix) < 4:
        return False
    strict = re.compile(f"(?:{_VERB_RE}){_GAP}{re.escape(prefix)}")
    line_bigrams = _bigrams(textmatch.condense(line))
    plain = _TAG_RE.sub("", source_text or "")
    for sentence in _SENTENCE_SPLIT_RE.split(plain):
        match = strict.search(sentence)
        if match is not None and _speaker_of(sentence[:match.start()], everyone) in own:
            return True
        if match is None and line_bigrams:
            covered = len(line_bigrams & _bigrams(textmatch.condense(sentence))) / len(line_bigrams)
            if covered >= textmatch.KEY_LINE_PRESENT_RATIO and any(
                _speaker_of(sentence[:verb.start()], everyone) in own for verb in _VERB_ONLY_RE.finditer(sentence)
            ):
                return True
    return False
