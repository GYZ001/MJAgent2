"""小说章节结构识别：标题正面定义、小节边界、超/欠尺寸章节的拆分与合并。

从 ``app/ingest.py`` 拆出（2026-09-02，WS1 派单）：``app.ingest`` 原本 299 行，
新增「标题正面定义」（第 X[章卷回节集部幕篇]/英文 Chapter 系列/分隔线包夹短行）
与「章节尺寸上下限拆分合并」两块逻辑后会超过单文件 500 行的红线。这里只放纯
正则/纯函数的结构判据，零 app 内部依赖，供 ``app.ingest`` 单向导入
（``app.ingest`` -> ``app.novel.structure``，同层 L1，不构成环）。落在
``app.novel`` 包下而不是 ``app/`` 根目录散文件，遵守「app/ 根目录不再新增
散文件」的结构红线。
"""
from __future__ import annotations

import re

_CHAPTER_NUMERALS = "0-9一二三四五六七八九十百千万零〇两壹贰叁肆伍陆柒捌玖拾佰仟"
# 结构词表：集/部/幕/篇与章/卷/回/节同级（长篇分「集」是常见体例，此前遗漏导致
# 整部按「集」切分的作品被当成无标题正文，见 WS1 派单「跑不快的孩子」案例）；
# 外篇是「番外」之外另一种常见叫法（《神墓》楔子内嵌「外篇——战天时代」即此）；
# 英文 Chapter/Episode/Part/EP 供双语或译制类文本使用。
_CHAPTER_CORE = (
    rf"(?:第[{_CHAPTER_NUMERALS}]+[章卷回节集部幕篇]"
    r"|序章|楔子|引子|前言|后记|尾声|终章|外篇|番外(?:篇)?(?:[0-9一二三四五六七八九十]+)?"
    r"|(?:Chapter|Episode|Part|EP)\s*\.?\s*\d+)"
)
CHAPTER_RE = re.compile(
    rf"^\s*[【\[]?\s*({_CHAPTER_CORE}[^\n】\]]{{0,40}}?)\s*[】\]]?\s*$",
    re.MULTILINE | re.IGNORECASE,
)
CHAPTER_ID_RE = re.compile(
    rf"^(第[{_CHAPTER_NUMERALS}]+[章卷回节集部幕篇])(.*)$",
)

# 装饰性分隔线上下夹住的短行也是标题——不少连载体作品（尤其中篇/剧本体）不用
# 「第X章」词表，只靠分隔线标出每一部分。字符集刻意与 app.ingest.SEPARATOR_ONLY_RE
# 不同（═━─＝，而非 -_=~*）：后者的字符会在 clean_text 里被当广告分隔线整行
# 删掉，若两者共用字符集，题目两侧的分隔线会在切章之前就被抹掉，判据据以失效；
# 这四种装饰线经验上极少被 clean_text 之外的逻辑用作广告分隔。
_SEPARATOR_TITLE_LINE = r"[═━─＝]{4,}"
SEPARATOR_TITLE_RE = re.compile(
    rf"^{_SEPARATOR_TITLE_LINE}\n([^\n]{{1,40}})\n{_SEPARATOR_TITLE_LINE}$",
    re.MULTILINE,
)

# 独占一行的纯序号（一/二/三…、1./１.）是小节，不是章：很多中篇/剧本体作品
# 在「第X集」内部再用序号分场——记进 paratext_json.sections，供尺寸拆分与
# 未来消费方定位，但绝不当作独立章节返回。
_SECTION_MARKER_RE = re.compile(
    r"^[　\s]*([一二三四五六七八九十百零〇]{1,4}|[0-9]{1,4}[.、．]?)[　\s]*$",
    re.MULTILINE,
)

# 章节尺寸上下限（2026-09-02 实测推导，见 WS1 派单）。仓库里没有「源章节字数
# -> 目标集数/时长」的可推导映射常数：app/config.py 的 EPISODE_TARGET_DEFAULT_S
# (50s)/SPOKEN_CHARS_PER_5_SECONDS(18) 约束的是剧本改编**之后**的口播时长，源
# 文原文到成片经模型自由改编（可压缩也可铺陈），两者之间没有代码编码的系数。
# 改用 6 个验证项目里「正常」的 5 个（西游记/神墓/我欲封天/三国演义两版）合计
# 2508 个章节的实测字数分布：中位数 3143，P95 6299，最大 15885（西游记单章）。
# LOWER_BOUND_CHARS=800：明显低于这 5 个项目各自的最短合法单章（神墓 1169 最
# 低），同时明显高于本次要修的病灶样本"尾声"366 字，两者之间留出安全边际。
# UPPER_BOUND_CHARS=16000：约为中位数的 5 倍，且刻意高于西游记已知最长合法
# 单章 15885（那一章没有可识别的小节，维持整章不拆），只拦住「体量畸大且确有
# 可拆小节」的章节，不误伤经典白话小说天然更长的单章体例。
LOWER_BOUND_CHARS = 800
UPPER_BOUND_CHARS = 16000


def _parse_chapter_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
        "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5,
        "陆": 6, "柒": 7, "捌": 8, "玖": 9,
    }
    units = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
    total = section = number = 0
    for char in value:
        if char in digits:
            number = digits[char]
        elif char == "万":
            total += (section + number) * 10000
            section = number = 0
        elif char in units:
            section += (number or 1) * units[char]
            number = 0
        else:
            return None
    return total + section + number


def _find_heading_matches(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, title)`` for every recognized chapter heading.

    Two independent, positive signals qualify a line as a heading: (1) it
    matches the ordinal/keyword vocabulary in ``CHAPTER_RE``, or (2) it is a
    short line sandwiched directly between two decorative separator lines
    (``SEPARATOR_TITLE_RE``) — this recognizes titles with no ordinal/keyword
    structure at all (custom part names in serialized fiction). Signal (2) is
    dropped wherever its captured title already sits inside a signal-(1)
    match, so a keyword heading is never double-counted.
    """
    keyword_matches = [
        (m.start(), m.end(), m.group(1).strip()) for m in CHAPTER_RE.finditer(text)
    ]
    covered = [(start, end) for start, end, _ in keyword_matches]
    separator_matches: list[tuple[int, int, str]] = []
    for m in SEPARATOR_TITLE_RE.finditer(text):
        title = m.group(1).strip()
        if not title or len(title) > 40:
            continue
        title_start, title_end = m.start(1), m.end(1)
        if any(start <= title_start and title_end <= end for start, end in covered):
            continue
        separator_matches.append((m.start(), m.end(), title))
    return sorted(keyword_matches + separator_matches, key=lambda item: item[0])


def _sequential_numerals(labels: list[str]) -> bool:
    """Whether ``labels`` is a clean 1,2,3… (or 0,1,2…) run, not coincidence."""
    values = [_parse_chapter_number(re.sub(r"[.、．]$", "", label)) for label in labels]
    if any(v is None for v in values) or values[0] not in (0, 1):
        return False
    return values == list(range(values[0], values[0] + len(values)))


def _extract_sections(content: str) -> list[dict]:
    """Standalone-numeral 小节 boundaries within one chapter's own content.

    Requires >=2 markers forming a clean ascending run starting at 0/1 —
    a single stray numeral line is not evidence of a real section scheme.
    """
    markers = [(m.start(), m.group(1).strip()) for m in _SECTION_MARKER_RE.finditer(content)]
    if len(markers) < 2 or not _sequential_numerals([label for _, label in markers]):
        return []
    sections = []
    for i, (start, label) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(content)
        sections.append({"start": start, "end": end, "label": label})
    return sections


def _split_oversized_chapter(chapter: dict, *, upper_bound: int = UPPER_BOUND_CHARS) -> list[dict]:
    """Split one chapter along its own 小节 boundaries once it exceeds ``upper_bound``.

    A chapter with no recorded sections (or only one) cannot be split at all —
    it is returned unchanged, which is the correct outcome for genre-normal
    long chapters (e.g. classic vernacular novels) that carry no sub-markers.
    """
    content = str(chapter.get("content") or "")
    if len(content) <= upper_bound:
        return [chapter]
    sections = _extract_sections(content)
    if len(sections) < 2:
        return [chapter]
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_len = 0
    for section in sections:
        span = section["end"] - section["start"]
        if current and current_len + span > upper_bound:
            groups.append(current)
            current, current_len = [], 0
        current.append(section)
        current_len += span
    if current:
        groups.append(current)
    if len(groups) < 2:
        return [chapter]
    title = str(chapter.get("title") or "").strip()
    pieces = []
    for i, group in enumerate(groups, start=1):
        start = 0 if i == 1 else group[0]["start"]
        piece_text = content[start:group[-1]["end"]].strip()
        if piece_text:
            pieces.append({"idx": 0, "title": f"{title}（{i}/{len(groups)}）", "content": piece_text})
    return pieces if len(pieces) >= 2 else [chapter]


def _split_oversized_chapters(chapters: list[dict]) -> list[dict]:
    result: list[dict] = []
    for chapter in chapters:
        result.extend(_split_oversized_chapter(chapter))
    return result


_ENDING_MERGE_TITLE_RE = re.compile(r"(?:尾声|后记|终章)$")


def _merge_undersized_ending_chapters(chapters: list[dict], *, lower_bound: int = LOWER_BOUND_CHARS) -> list[dict]:
    """Fold a too-short trailing-type chapter (尾声/后记/终章) into its predecessor.

    Scoped to these three keywords only — unlike 番外/外篇 (which can appear
    mid-book as an aside, see 神墓's embedded 外篇 right after 楔子), 尾声/后记/
    终章 always denote genuine end-of-work material, so merging into "the
    chapter right before it" never crosses an unrelated story boundary.
    Never applied to the very first chapter (nothing to merge into).
    """
    merged: list[dict] = []
    for chapter in chapters:
        title = str(chapter.get("title") or "").strip()
        content = str(chapter.get("content") or "")
        if merged and len(content) < lower_bound and _ENDING_MERGE_TITLE_RE.search(title):
            previous = merged[-1]
            previous["title"] = f"{previous['title']} · {title}"
            previous["content"] = f"{previous['content']}\n\n{content}".strip()
            continue
        merged.append(dict(chapter))
    return merged


def _first_short_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= 40 else ""
    return ""


_PREAMBLE_LARGE_FRACTION = 0.5
_PREAMBLE_KEEP_MIN_CHARS = 200


def _preamble_chapters(text: str, preamble: str) -> list[dict]:
    """Name (and if needed, split) the text preceding the first recognized heading.

    A short preamble (<=200 chars) carries no real content and is discarded,
    matching prior behavior. A preamble under half the document is a genuine
    prologue — call it 楔子, the conventional name, regardless of its own
    first line. A preamble at or above half the document is not a prologue at
    all — it is everything ``_find_heading_matches`` failed to subdivide, and
    naming that "楔子" would misdescribe most of the book as an introduction.
    Retry with the loosest available boundary (standalone 小节 markers)
    before falling back to naming it after its own first short line.
    """
    if len(preamble) <= _PREAMBLE_KEEP_MIN_CHARS:
        return []
    if len(preamble) < len(text) * _PREAMBLE_LARGE_FRACTION:
        return [{"idx": 0, "title": "楔子", "content": preamble}]
    if len(_extract_sections(preamble)) >= 2:
        base_title = _first_short_line(preamble) or "正文"
        pieces = _split_oversized_chapter({"title": base_title, "content": preamble}, upper_bound=0)
        if len(pieces) > 1:
            return pieces
    return [{"idx": 0, "title": _first_short_line(preamble) or "楔子", "content": preamble}]
