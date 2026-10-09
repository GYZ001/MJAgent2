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
# 序号两侧的行内空白（空格/制表/全角空格）是排版差异不是结构差异：2026-09-15
# 《龙猫出爪》12 章全部写作「第 1 章《掌心里的猫》」，此前正则要求紧排，一章都没
# 认出，整本书退化成 3000 字硬切。刻意不用 \s（含换行）：标题必须独占一行。
_INLINE_WS = r"[ \t\u3000]*"
_CHAPTER_CORE = (
    rf"(?:第{_INLINE_WS}[{_CHAPTER_NUMERALS}]+{_INLINE_WS}[章卷回节集部幕篇]"
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
# 从标题里取「第 N 章」的序号；标题保持原文逐字（含空白），序号解析自己容忍空白。
CHAPTER_ORDINAL_RE = re.compile(rf"第{_INLINE_WS}([{_CHAPTER_NUMERALS}]+){_INLINE_WS}章")

# 行内标题核心：标题标号可能不独占一行，而是跟同一行里的其他内容挤在一
# 起——两种形态结构上完全相同（同一行、核心前有前缀），真假与切法都交给
# 调用方（见 ``_accept_inline_matches``/``_finalize_inline_matches``）：
#   1）前缀是上一章末行残片，如《魂穿刘关张，诸侯们被整麻了》里的
#      `怎么斩华第2章我斩华雄？`："怎么斩华"是残片，"第2章我斩华雄？"是标题；
#   2）前缀是纯装饰/编号，如 `--- 259.第258章 标题 ---`；
# 这里的正则只负责标出「核心在行内的位置」。捕获组 1 是序号数字（供
# ``_parse_chapter_number`` 解析），捕获组 2 是单位字（供同单位连续性比较，
# 见 ``_heading_ordinal_unit``）。词表故意比 _CHAPTER_CORE 少一个「节」：
# 独占一行时「第N节」极少歧义，但允许它出现在行中/行首任意位置扫描时就不
# 再安全——实测《高考体》回归样本里「第二节课」「第三节晚自习」这类描述
# 上课节次的日常叙述出现 508 次，且因故事反复按顺序交代一天的课次，天然
# 形成 1,2,3,4 的局部连续序列，会被序号连续性误判成真标题，炸出 254 个不
# 存在的假章节。这本书的真实章节标题全部用「章」，零例用「节」，两者在
# 「行内扫描」场景下没有安全的共存方式。
_INLINE_CHAPTER_CORE_RE = re.compile(
    rf"第{_INLINE_WS}([{_CHAPTER_NUMERALS}]+){_INLINE_WS}([章卷回集部幕篇])"
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


_InlineCandidate = tuple[int, int, int, str, str, int, str, bool]


# 行首纯装饰/编号前缀后面跟的分隔符串（如末尾的 ` ---`）同样是装饰，title
# 里不保留。字符集故意比 app.ingest.SEPARATOR_ONLY_RE 宽（含 ASCII 连字符
# 与常见全角分隔符变体），只在已确认「整行是装饰前缀+标题」之后才使用，
# 不会误伤正常标题末尾的汉字标点。
_TRAILING_DECOR_RE = re.compile(r"[\s\-_=~*－—～·]+$")


def _inline_heading_candidates(text: str) -> list[_InlineCandidate]:
    """逐行找出「序号核心出现在行中」的候选，含误报，不做真假判定、不判切法。

    每行只取最后一个核心出现的位置（防止正文里援引更早的「第N章」抢占行
    尾）。返回 ``(core_start, line_start, line_end, prefix, title, ordinal,
    unit, is_decorative)``：``title`` 是核心到行尾、去掉末尾装饰分隔符、
    strip 后要求非空且 <=40 字（与 CHAPTER_RE 的标题上限一致）；
    ``is_decorative`` 是核心前的 ``prefix`` 是否不含任何汉字/字母
    （``str.isalpha()`` 对汉字同样为真）。真假判定交给
    ``_accept_inline_matches``，装饰/粘连/卷名前缀三种切法交给
    ``_finalize_inline_matches``。
    """
    candidates: list[_InlineCandidate] = []
    offset = 0
    for line in text.split("\n"):
        line_start, line_end = offset, offset + len(line)
        offset = line_end + 1
        last_match = None
        for m in _INLINE_CHAPTER_CORE_RE.finditer(line):
            last_match = m
        if last_match is None:
            continue
        prefix = line[:last_match.start()]
        title = _TRAILING_DECOR_RE.sub("", line[last_match.start():]).strip()
        ordinal = _parse_chapter_number(last_match.group(1))
        if not title or len(title) > 40 or ordinal is None:
            continue
        is_decorative = not any(ch.isalpha() for ch in prefix)
        candidates.append((
            line_start + last_match.start(), line_start, line_end,
            prefix, title, ordinal, last_match.group(2), is_decorative,
        ))
    return candidates


def _heading_ordinal_unit(title: str) -> tuple[int | None, str | None]:
    matches = list(_INLINE_CHAPTER_CORE_RE.finditer(title))
    if not matches:
        return None, None
    return _parse_chapter_number(matches[-1].group(1)), matches[-1].group(2)


# 独占一行标题要用含「节」的完整单位词表取 (序号, 单位)，不能复用
# _INLINE_CHAPTER_CORE_RE（那个词表故意不含「节」，见其注释），否则「第二
# 节晚自习结束。」会解析成 (None, None)，_structural_units 的过滤形同虚设。
_HEADING_UNIT_RE = re.compile(
    rf"第{_INLINE_WS}([{_CHAPTER_NUMERALS}]+){_INLINE_WS}([章卷回节集部幕篇])"
)


def _keyword_ordinal_unit(title: str) -> tuple[int | None, str | None]:
    matches = list(_HEADING_UNIT_RE.finditer(title))
    if not matches:
        return None, None
    return _parse_chapter_number(matches[-1].group(1)), matches[-1].group(2)


def _is_ascending_run(values: list[int], start_values: tuple[int, ...]) -> bool:
    return bool(values) and values[0] in start_values and values == list(range(values[0], values[0] + len(values)))


def _unit_nested_in_every_segment(
    main_positions: list[int], items: list[tuple[int, int]],
) -> bool:
    """嵌套小节判据：每个「相邻两主单位标题之间」的段里，候选序号恰好 1..k。

    候选落在段外（第一个主单位前/最后一个后/主单位不足 2 个构不成段）判负。
    """
    bounds = sorted(main_positions)
    segments = list(zip(bounds[:-1], bounds[1:]))
    buckets: list[list[int]] = [[] for _ in segments]
    for start, ordinal in items:
        index = next((i for i, (lo, hi) in enumerate(segments) if lo < start < hi), None)
        if index is None:
            return False
        buckets[index].append(ordinal)
    return all(_is_ascending_run(bucket, (1,)) for bucket in buckets if bucket)


def _structural_units(keyword_matches: list[tuple[int, int, str]]) -> set[str] | None:
    """正面定义：带序号标题里，非主单位必须整书构成一致结构才算真标题。

    案例（《刚准备高考》）：全书主单位是「章」，但 189 处独占一行的叙述句
    「第二节晚自习结束。」也符合 CHAPTER_RE 的「第N节」词表，被当成假标题，
    还连带把紧邻的真标题 `--- 第481章 少女情怀 ---` 挤掉（两者之间没有正
    文，被 ``if remainder:`` 判定空章丢弃）。主单位 D＝出现最多的单位，自
    己总是结构单位。其余单位 U 要么 (a) 在每个含 U 的 D 段（相邻两个 D 标
    题之间）里序号恰好 1,2,…,k（嵌套小节，如每章的"第一节/第二节"），要么
    (b) 全书 U 的序号整体是一条 0/1 起的连续序列（卷/部这类跨章上级单位）。
    两者都不满足就判定 U 不是结构单位，按整本书全有或全无（不逐条判）：
    误拒的代价只是小节并入所在章，误收的代价是丢真标题/切错。没有 D（没
    有任何带序号标题）时不做这项校验，返回 ``None`` 放行一切。
    """
    ordinal_matches = [
        (start, ordinal, unit)
        for start, _end, title in keyword_matches
        for ordinal, unit in (_keyword_ordinal_unit(title),)
        if ordinal is not None and unit is not None
    ]
    if not ordinal_matches:
        return None
    counts: dict[str, int] = {}
    for _start, _ordinal, unit in ordinal_matches:
        counts[unit] = counts.get(unit, 0) + 1
    majority = max(counts, key=lambda u: counts[u])
    main_positions = [s for s, _o, u in ordinal_matches if u == majority]
    structural = {majority}
    for unit in counts:
        if unit == majority:
            continue
        items = [(s, o) for s, o, u in ordinal_matches if u == unit]
        whole_ordinals = [o for _s, o in items]
        if _is_ascending_run(whole_ordinals, (0, 1)) or _unit_nested_in_every_segment(
            main_positions, items,
        ):
            structural.add(unit)
    return structural


def _filter_structural_keyword_matches(
    keyword_matches: list[tuple[int, int, str]],
) -> list[tuple[int, int, str]]:
    structural_units = _structural_units(keyword_matches)
    if structural_units is None:
        return keyword_matches
    kept = []
    for match in keyword_matches:
        _ordinal, unit = _keyword_ordinal_unit(match[2])
        if unit is None or unit in structural_units:
            kept.append(match)
    return kept


def _accept_inline_matches(
    anchor_matches: list[tuple[int, int, str]],
    candidates: list[_InlineCandidate],
    covered: list[tuple[int, int]],
) -> list[_InlineCandidate]:
    """序号连续性过滤，装饰前缀候选与粘连候选走同一条判据——不再对装饰
    前缀无条件采信：独占一行的对白「"第二回合开始！"」前缀只有引号，不含
    字母，若无条件采信会被当成标题整行吃掉。候选只有在与前/后邻居序号相
    差恰好 1、且前后邻居都不与它同序号时才采信；同序号说明真标题就在旁
    边，它只是正文提及（例如正文援引"第3章"而真标题"第3章"就在紧邻处）。
    续性只在同一单位（章/卷/回/…）之间比较：取紧邻的前一个/后一个候选，
    单位不同就视为不连续/不同序号——不跨单位去远处找同单位邻居，因为那
    等于悄悄跳过中间的真实结构，可能把本不连续的两处说成连续（例如"第一
    卷"后紧跟粘连的"第2章"不该算连续）。独占一行标题（``anchor_matches``）
    本身就是结构证据，不受此过滤，只参与邻居比较；候选若已被某个独占行
    标题覆盖（``covered``），说明只是同一标题被两种信号重复发现，跳过。
    """
    anchors = [
        (start, *_heading_ordinal_unit(title)) for start, _end, title in anchor_matches
    ]
    inline = [c for c in candidates if not any(s <= c[0] < e for s, e in covered)]
    entries = sorted(
        [(start, ordinal, unit, None) for start, ordinal, unit in anchors if ordinal is not None]
        + [(c[0], c[5], c[6], c) for c in inline],
        key=lambda item: item[0],
    )
    accepted: list[_InlineCandidate] = []
    for i, (_start, ordinal, unit, payload) in enumerate(entries):
        if payload is None:
            continue
        prev_e = entries[i - 1] if i > 0 else None
        next_e = entries[i + 1] if i + 1 < len(entries) else None
        prev_ordinal = prev_e[1] if prev_e and prev_e[2] == unit else None
        next_ordinal = next_e[1] if next_e and next_e[2] == unit else None
        continuous = prev_ordinal == ordinal - 1 or next_ordinal == ordinal + 1
        same_neighbor = prev_ordinal == ordinal or next_ordinal == ordinal
        if continuous and not same_neighbor:
            accepted.append(payload)
    return accepted


def _finalize_inline_matches(accepted: list[_InlineCandidate]) -> list[tuple[int, int, str]]:
    """把通过连续性过滤的候选按切法落成 ``(start, end, title)``。

    装饰前缀（不含字母）：整行是标题，start 取行首，前缀直接丢弃——``title``
    已经是核心到行尾（见 ``_inline_heading_candidates``），不含前缀。粘连前缀
    （含汉字/字母）默认只把核心到行尾当标题、前缀留给上一章当残片；但前缀
    若与「本次采信名单」里紧邻的前一个或后一个候选前缀完全相同，说明它是
    卷名一类反复出现、属于标题自身的部分——卷名会连续出现在同卷每一章
    标题前，必然在已采信的顺序里前后相邻。这里特意不用"全书范围内这个
    前缀出现了几次"：实测《魂穿刘关张》回归样本里"是""好""遵命""谢陛下"这类
    极短的对话收尾词在近 700 章的长篇里会碰巧重复出现十几次，但从不会恰好
    出现在紧邻的下一章或上一章标题前——按"全书计数 >=2"会把这些无关的
    残片误判成卷名，炸出一批标题带残留对话的假阳性；按"紧邻候选前缀相同"
    判断，两个相隔几百章的残片不会相邻，天然被排除。重复前缀的候选 start
    同样取行首、标题改成「前缀+核心到行尾」整段，不往上一章挪字。
    """
    matches: list[tuple[int, int, str]] = []
    for i, (core_start, line_start, line_end, prefix, title, _ord, _unit, is_decorative) in enumerate(accepted):
        stripped_prefix = prefix.strip()
        prev_prefix = accepted[i - 1][3].strip() if i > 0 else None
        next_prefix = accepted[i + 1][3].strip() if i + 1 < len(accepted) else None
        is_volume_prefix = bool(stripped_prefix) and stripped_prefix in (prev_prefix, next_prefix)
        if is_decorative:
            matches.append((line_start, line_end, title))
        elif is_volume_prefix:
            matches.append((line_start, line_end, (prefix + title).strip()))
        else:
            matches.append((core_start, line_end, title))
    return matches


def _find_heading_matches(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, title)`` for every recognized chapter heading.

    Three signal families qualify a heading. (1) ``CHAPTER_RE`` matches a
    whole line — the structural anchor everything else is checked against,
    except a non-majority ordinal unit (e.g. 节 in a 章-numbered book) is
    dropped book-wide unless ``_structural_units`` confirms it forms a real
    structural pattern (see its docstring for the data-loss case this
    guards against). (2) a short line sandwiched between decorative
    separator lines (``SEPARATOR_TITLE_RE``) — custom part names with no
    ordinal at all. (3) an ordinal core sits on a line with some other
    prefix — covers both a pure decoration/numbering prefix and a genuine
    prose fragment glued from the previous chapter; both go through the
    same ordinal-continuity check (``_accept_inline_matches``), then
    ``_finalize_inline_matches`` decides whether the prefix is kept or left
    for the previous chapter. Signals (2)-(3) are dropped wherever their
    span already sits inside an earlier-listed match.
    """
    keyword_matches = _filter_structural_keyword_matches([
        (m.start(), m.end(), m.group(1).strip()) for m in CHAPTER_RE.finditer(text)
    ])
    covered = [(start, end) for start, end, _ in keyword_matches]
    accepted = _accept_inline_matches(
        keyword_matches, _inline_heading_candidates(text), covered,
    )
    inline_matches = _finalize_inline_matches(accepted)
    covered = covered + [(start, end) for start, end, _ in inline_matches]
    separator_matches: list[tuple[int, int, str]] = []
    for m in SEPARATOR_TITLE_RE.finditer(text):
        title = m.group(1).strip()
        if not title or len(title) > 40:
            continue
        title_start, title_end = m.start(1), m.end(1)
        if any(start <= title_start and title_end <= end for start, end in covered):
            continue
        separator_matches.append((m.start(), m.end(), title))
    return sorted(
        keyword_matches + separator_matches + inline_matches,
        key=lambda item: item[0],
    )


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
