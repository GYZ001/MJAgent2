"""关键内容模糊匹配底座（映射台 / 大纲台 / 分镜台共用）。

务实优先：只拦【明显丢失】，用模糊匹配容忍口语化改写与标点差异，绝不逐字比对。

PRD VAL-422 §4.4.4 之后，这些函数的定位被下调：它们只用于
① 旧数据缺少稳定 ID 时的降级判定；② 规划期把关键内容自动分配到镜头；③ 人工复核风险提示。
**不得**单独产生 `must_keep missing` blocker——结构化 ID 台账才是主判据。
"""
from __future__ import annotations

import re

_SPEAKER_PREFIX_RE = re.compile(r"^([^\n：（(:]{1,16})(?:（[^）]{0,12}）)?[：:]")
_NON_CONTENT_RE = re.compile(r"""[\s，。、；;：:！!？?“”"'‘’（）()【】\[\]《》〈〉—…·.,~\-]+""")
# 句读分隔：把一条复合的关键内容切成原子。纯标点驱动、与具体剧情无关，适用任意题材。
_CLAIM_SPLIT_RE = re.compile(r"[；;。.！!？?，,、\n]+")
_SPOKEN_DIGITS = {
    "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2",
    "三": "3", "四": "4", "五": "5", "六": "6", "七": "7",
    "八": "8", "九": "9",
}
_DIGIT_SEQUENCE_CHARS = frozenset("0123456789" + "".join(_SPOKEN_DIGITS))

# 关键台词主干连续保留过半即视为"仍在"（容忍前后改写，只要核心句仍出现）。
KEY_LINE_PRESENT_RATIO = 0.4
KEY_LINE_BIGRAM_COVERAGE = 0.42
# 关键剧情点是描述而非逐字，故用 2-gram 覆盖率判定："过三分之一被涵盖"即视为"已落实"。
KEY_POINT_COVERAGE = 0.34


def strip_speaker(line: str) -> str:
    """去掉"角色名（情绪）："前缀，取台词正文本身用于匹配。"""
    return _SPEAKER_PREFIX_RE.sub("", (line or "").strip(), count=1).strip()


def speaker_name(line: str) -> str | None:
    """提取 key_lines / 对白中的说话人姓名（不含情绪括号）。"""
    match = _SPEAKER_PREFIX_RE.match((line or "").strip())
    if not match:
        return None
    return match.group(1).strip() or None


def condense(text: str) -> str:
    """压成纯内容字符串（去空白与标点），让匹配对标点/排版差异稳健。"""
    return _NON_CONTENT_RE.sub("", text or "")


def spoken_digit_sequence_equivalent(left: str, right: str) -> bool:
    """Return whether two pure digit identifiers have the same spoken digits.

    This intentionally handles only digit-by-digit sequences such as
    ``7-3-1`` / ``七、三、一``. Mixed prose is excluded so source-fidelity
    checks remain strict for actual dialogue rewrites.
    """
    first = condense(left)
    second = condense(right)
    if not first or not second:
        return False
    if any(char not in _DIGIT_SEQUENCE_CHARS for char in first + second):
        return False

    def normalize(value: str) -> str:
        return "".join(_SPOKEN_DIGITS.get(char, char) for char in value)

    return normalize(first) == normalize(second)


def longest_run_ratio(needle: str, haystack: str) -> float:
    """needle 核心字符在 haystack 中的最长连续公共块长度 ÷ needle 长度。"""
    n, h = condense(needle), condense(haystack)
    if not n:
        return 1.0
    if n in h:
        return 1.0
    if not h:
        return 0.0
    return _longest_common_substring_len(n, h) / len(n)


def _longest_common_substring_len(left: str, right: str) -> int:
    """Return the exact longest contiguous common substring length in O(n+m).

    ``difflib.SequenceMatcher.find_longest_match`` computes the same kind of
    contiguous block but can become quadratic on long, repetitive screenplay
    text. A suffix automaton over ``left`` preserves the matching contract
    while keeping certificate and recovery validation linear.
    """
    transitions: list[dict[str, int]] = [{}]
    links = [-1]
    lengths = [0]
    last = 0

    for char in left:
        current = len(transitions)
        transitions.append({})
        links.append(0)
        lengths.append(lengths[last] + 1)
        cursor = last
        while cursor >= 0 and char not in transitions[cursor]:
            transitions[cursor][char] = current
            cursor = links[cursor]
        if cursor < 0:
            links[current] = 0
        else:
            target = transitions[cursor][char]
            if lengths[cursor] + 1 == lengths[target]:
                links[current] = target
            else:
                clone = len(transitions)
                transitions.append(dict(transitions[target]))
                links.append(links[target])
                lengths.append(lengths[cursor] + 1)
                while cursor >= 0 and transitions[cursor].get(char) == target:
                    transitions[cursor][char] = clone
                    cursor = links[cursor]
                links[target] = clone
                links[current] = clone
        last = current

    state = 0
    run = 0
    best = 0
    for char in right:
        while state and char not in transitions[state]:
            state = links[state]
            run = min(run, lengths[state])
        target = transitions[state].get(char)
        if target is None:
            state = 0
            run = 0
            continue
        state = target
        run += 1
        if run > best:
            best = run
            if best == len(left):
                return best
    return best


def bigram_set(text: str) -> set[str]:
    c = condense(text)
    if len(c) < 2:
        return {c} if c else set()
    return {c[i:i + 2] for i in range(len(c) - 1)}


def bigram_coverage(needle: str, haystack: str) -> float:
    """needle 的 2-gram 有多大比例出现在 haystack 里。"""
    nb = bigram_set(needle)
    if not nb:
        return 1.0
    return len(nb & bigram_set(haystack)) / len(nb)


def atomize_claim(text: str) -> list[str]:
    """把一条可能复合的关键内容按句读切成原子 claim。

    复合 covers/剧情点如"测出三段，被宣告低级，引发哄笑"应逐条核对，避免漏掉其中一件事时
    整句一起判失败、报错也指不到具体缺哪条。过短碎片（连接词等）丢弃，避免噪声。
    """
    atoms: list[str] = []
    seen: set[str] = set()
    for piece in _CLAIM_SPLIT_RE.split(text or ""):
        atom = strip_speaker(piece).strip()
        key = condense(atom)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        atoms.append(atom)
    return atoms


