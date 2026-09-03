"""画面存在证据：给定候选称谓与原文（可选分镜标签），从数据推导它是否有
在场描写/对白/动作，供人物发现的非角色判定核对使用。

WS3「人物发现按叙事分量与画面存在判定，非角色判定不再与画面事实相反」新增。
纯函数，不调模型——分类只靠两类结构信号：标点标出的对白（引号）与通用具身
动词邻接（跨项目、跨角色名通用的动作类单字，不针对任何具体项目或人名，命中
与否只决定一句话落进 onscreen_mentions 还是 mention_only 这两类证据里，不
直接产出 important 的最终结论；最终结论仍由模型判定 + 下游核对逻辑给出，见
``app.portraits.card_verdict.reconsider_verdict_with_presence_evidence``）。

生产事故（proj_ce9fcf749b23《跑不快的孩子》）：马拉多纳先是墙上被盯着看的
海报，又是"赛后哭了"的主教练——"马拉多纳哭了"一句里动词紧邻人名，是明确的
动作描写，但建卡判定仍把他判成 important=false 并写进 char_not_character
永久负缓存。姆巴佩同理（"姆巴佩刚刚跑过他身边，快得像一道白色的光"）。见
tests/test_character_presence_discovery.py 的 fixture（取自生产库原文）。
"""

from __future__ import annotations

import hashlib
import json
import re

_SENTENCE_END_CHARS = "。！？!?\n"
_QUOTE_OPEN_CHARS = "「『"
_QUOTE_CLOSE_CHARS = "」』"
_QUOTE_TOGGLE_CHARS = "“”\""  # “ ” 与直引号 "（开合同一个字符，只能靠计数切换）
# 「」『』是常见中文直角引号；“”/" 覆盖弯引号与直引号（原文两种混用，见
# 生产样本"再不定，人就走了""梅西，别走。"）。

# 通用具身动作/言说动词单字集合：跨项目、跨角色名通用的语言学结构信号，不是
# 针对任何具体人名的名单——用来识别"动作描写"这一结构性类别（对白靠引号识别），
# 命中与否只决定证据分类，不给任何候选的重要性打分（CLAUDE.md「不用词表打分」
# 指的是不能靠这类命中直接决定 important，最终判定见
# card_verdict.reconsider_verdict_with_presence_evidence）。
#
# 刻意不收 站/坐/立/看/听/进：中文写建筑、山峦、宗门"矗立/耸立/坐落/屹立"在
# 某处是极常见的场景描写套路，这几个字紧邻一个组织/地点/器物名同样会命中——
# 真实回归 test_non_person_never_enters_the_character_bible 命中过"靠山宗矗立
# 在山谷之中"（矗立含"立"）被误判成"这是人"。只收需要躯体/声带才做得出、
# 组织/地点/器物几乎不会被这样描写的动作，降低假阳性。
_PHYSICAL_ACTION_VERB_CHARS = frozenset(
    "跑走笑哭抬转伸推拉打抱握扑踢冲撞跳蹲趴躺爬扶拽甩摸碰"
    "拄捂捏揉搓拍摔砸扔丢接抓拧扯拖挥举颤僵愣怔瞪皱蹬奔捧递撑扛背驮牵"
)
# 说/道/喊/叫：这几个字既是具身动作，也是中文最常见的言说归属动词（"X说"
# "X道"最常见的用法就是引出下文的话，即使没有引号）。真实回归（西游记探针
# 复验残留假阳性）："出灵霄宝殿道：「请如来少待……」"——"灵霄宝殿"是"出"
# 的宾语（从大殿里出来），"道"的主语是前文另一个人，不是"灵霄宝殿"；但
# "灵霄宝殿"紧邻"道"，若把"道"当成普通具身动作，邻接检查照样会命中。
# 这几个言说归属动词因此要求候选称谓紧贴分句开头（``_is_clause_initial``，
# 与"说话人框架"对白判据同一条件）才算数——不满足就只是"提到某处、后面有
# 人说话"，不是"这个名字在说话"。
_SPEECH_ATTRIBUTION_VERB_CHARS = frozenset("说道喊叫")
_ACTION_WINDOW = 6


def _split_sentences(text: str) -> list[str]:
    """按中文句末标点切句，保留每句原文，用于逐句定位候选称谓的出现位置。

    不在引号内部切句：引号内常见问句/感叹句（"我怎么会……联系到一起呢？"
    这类），若按裸标点切开会把引号拆成两半，对白检测（``_quote_spans``
    要求开合引号在同一句内）就会失效——机场男孩举牌"梅西，别走。"、神墓
    "他父亲"的引述都是这个形状，见 tests/test_character_presence_discovery.py。
    """
    if not text:
        return []
    out: list[str] = []
    start = 0
    depth = 0
    toggled = False
    for i, ch in enumerate(text):
        if ch in _QUOTE_OPEN_CHARS:
            depth += 1
        elif ch in _QUOTE_CLOSE_CHARS:
            depth = max(0, depth - 1)
        elif ch in _QUOTE_TOGGLE_CHARS:
            toggled = not toggled
        elif ch in _SENTENCE_END_CHARS and depth == 0 and not toggled:
            piece = text[start:i + 1].strip()
            if piece:
                out.append(piece)
            start = i + 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _quote_spans(sentence: str) -> list[tuple[int, int]]:
    """句内每对引号的 ``(open_index, close_index)``（含首尾引号字符本身）。

    「」『』按各自的开合字符配对；直引号 ``"`` 开合同形，靠奇偶次数切换配对
    （与 ``_split_sentences`` 的 depth/toggle 状态机同一思路，这里只在单句内
    重新扫一遍，因为需要具体的起止下标而不只是"在不在引号里"这一个布尔值）。
    未闭合的孤立引号不产生 span。
    """
    spans: list[tuple[int, int]] = []
    open_stack: list[int] = []
    toggle_open: int | None = None
    for i, ch in enumerate(sentence):
        if ch in _QUOTE_OPEN_CHARS:
            open_stack.append(i)
        elif ch in _QUOTE_CLOSE_CHARS:
            if open_stack:
                spans.append((open_stack.pop(), i))
        elif ch in _QUOTE_TOGGLE_CHARS:
            if toggle_open is None:
                toggle_open = i
            else:
                spans.append((toggle_open, i))
                toggle_open = None
    return spans


def _inside_any_span(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < pos < end for start, end in spans)


# 分句内部的逗号/顿号/分号同样是从句边界：动作动词邻接与"说话人框架"都不
# 能跨过它去够到别的分句——真实回归（三国探针复验）"…鸩杀董后于河间驿庭，
# 举柩回京…"，"举"紧邻在"河间驿庭"后 2 字，但中间隔着一个逗号：抬棺材的
# 是前一分句的主语（何进一伙），不是"河间驿庭"这个地点在"举"。引号闭合
# 字符同样是边界：上一个人的话音刚落，紧跟着的名字理所当然是下一分句的
# 主语（"…”操曰：…" ）。
_CLAUSE_BOUNDARY_CHARS = (
    "。！？，；、\n" + _QUOTE_CLOSE_CHARS + _QUOTE_TOGGLE_CHARS
)


def _clause_bounds(sentence: str, pos: int) -> tuple[int, int]:
    """``pos`` 所在分句的 ``[start, end)``——向左找最近的分句边界字符（不含），
    向右找最近的分句边界字符（不含），没有则到句首/句尾。"""
    left = pos
    while left > 0 and sentence[left - 1] not in _CLAUSE_BOUNDARY_CHARS:
        left -= 1
    right = pos
    while right < len(sentence) and sentence[right] not in _CLAUSE_BOUNDARY_CHARS:
        right += 1
    return left, right


def _sentence_has_action_near(sentence: str, start: int, end: int) -> bool:
    """称谓（本次出现的 span 是 ``[start, end)``）在同一分句内紧邻（前后
    ``_ACTION_WINDOW`` 字内、且不跨分句边界）具身动作/言说动词，视为动作
    描写：动词在称谓之后（"马拉多纳哭了"）或之前（"扶起老人"，称谓是动作
    对象）都算，中文两种语序都常见，只认邻接不认句法角色。

    言说归属动词（说/道/喊/叫）额外要求称谓紧贴分句开头才算数——它们不是
    纯物理动作，"X 道"最常见的意思就是"X 说了下面的话"，邻接检查本身分不清
    "X 是道的主语"和"X 只是刚提到的地点/宾语，道的主语是别人"，只能靠位置
    这条结构信号收窄（见 _SPEECH_ATTRIBUTION_VERB_CHARS 常量注释）。
    """
    clause_start, clause_end = _clause_bounds(sentence, start)
    forward = sentence[end:min(end + _ACTION_WINDOW, clause_end)]
    backward = sentence[max(clause_start, start - _ACTION_WINDOW):start]
    if any(ch in _PHYSICAL_ACTION_VERB_CHARS for ch in forward) or any(
        ch in _PHYSICAL_ACTION_VERB_CHARS for ch in backward
    ):
        return True
    return _is_clause_initial(sentence, start) and (
        any(ch in _SPEECH_ATTRIBUTION_VERB_CHARS for ch in forward)
        or any(ch in _SPEECH_ATTRIBUTION_VERB_CHARS for ch in backward)
    )


def _is_clause_initial(sentence: str, start: int) -> bool:
    """称谓是否紧贴在分句开头（句首，或紧跟在上一个分句边界字符之后，
    中间没有其它字符）。"""
    return start == 0 or sentence[start - 1] in _CLAUSE_BOUNDARY_CHARS


def _occurrence_kinds(name: str, sentence: str) -> list[str]:
    """按候选称谓在句中每一次出现的具体位置分类，不是按整句笼统判断。

    真实回归（西游记/三国探针复验）：
    - 「弟子乃东胜神洲傲来国花果山水帘洞人氏」这类台词里，地名整段落在
      引号内部——旧版只看"句子里有没有引号 + 有没有这个名字"，两者都命中
      就判 dialogue，把"被谈论的地名"误判成"在场对白"。
    - 「…直至灵霄宝殿，启奏道：「…」」这类句子里，地名是"直至"的宾语（到
      达的地点），真正的说话人是更早提到的另一个主语；旧版"该名与开引号
      之间没有其它引号即算说话人框架"太松，只要地名后面不远处刚好接了个
      引号就会误判。改成要求该名紧贴分句开头（``_is_clause_initial``）——
      "却说十常侍既握重权"、"众猴都道""操曰"这类真正由该名做主语的分句，
      该名前面就是句首或上一个分句的边界（逗号/句号/引号闭合）；"直至灵霄
      宝殿，启奏道"里"灵霄宝殿"前面是"直至"两个字，不是分句边界，就不再
      算说话人框架。

    规则：
    - 出现在引号内部（``_inside_any_span``）：一律不算 dialogue、也不查动作
      邻接——引号里说的是什么，不代表说话人是谁，这次出现只能证明"被提及"。
    - 出现在引号外部、且紧贴分句开头：若同一分句内存在起点在该称谓之后的
      引号（说话人框架）→ 算 dialogue。
    - 同一分句内动作动词邻接（含"「……」众猴道。"这种反向语序，由动作邻接
      检查而非本分支覆盖）→ 算 action，不要求分句开头（动作对象常见于分句
      中段，如"扶起老人"）。
    """
    quote_spans = _quote_spans(sentence)
    kinds: set[str] = set()
    for match in re.finditer(re.escape(name), sentence):
        start, end = match.span()
        if _inside_any_span(start, quote_spans) or _inside_any_span(end - 1, quote_spans):
            continue
        _, clause_end = _clause_bounds(sentence, start)
        if _is_clause_initial(sentence, start) and any(
            end <= span_start < clause_end for span_start, _span_end in quote_spans
        ):
            kinds.add("dialogue")
        if _sentence_has_action_near(sentence, start, end):
            kinds.add("action")
    return sorted(kinds)


def _shot_tag_hits(name: str, shot_rows: list[dict] | None) -> list[dict]:
    """分镜已经把 name 标进 ``shots.characters`` 标签列表——已有分镜时最强的
    画面存在信号（分镜台自己判定的在场角色，不是从原文猜的）。"""
    hits = []
    for row in shot_rows or []:
        try:
            tags = json.loads(row.get("characters") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            tags = []
        if any(isinstance(tag, str) and tag.strip() == name for tag in tags):
            hits.append(row)
    return hits


def collect_presence_evidence(
    name: str,
    chapters_by_idx: dict[int, str] | None,
    shot_rows: list[dict] | None = None,
) -> dict:
    """推导 name 的画面存在证据。

    返回 ``{"name", "onscreen_mentions", "mention_only", "recurrence"}``：
    - onscreen_mentions/mention_only：每条 ``{chapter_idx, excerpt, evidence_kinds}``，
      ``evidence_kinds`` 是 ``{"dialogue","action","shot_tagged"}`` 的子集
      （非空即落入 onscreen_mentions，否则落入 mention_only——只被提及/回忆/
      海报等非在场提及没有对白也没有动作邻接）。逐字相同的句子只计一次
      （测试夹具里常见 ``"路人甲走过。" * 6`` 这类人工重复；同一句原文重复
      出现不代表新增了一处叙事证据，去重后才是"这段原文里有几处不同的
      在场描写"，不是"这句话字面出现了几次"）。
    - recurrence：``{"paragraph_count", "chapter_count"}``，前者是两类证据去重
      后的句子总数，后者是覆盖的章节数（不含 shot_tagged 条目，它们没有
      章节号）。
    """
    name = str(name or "").strip()
    onscreen: list[dict] = []
    mention_only: list[dict] = []
    seen_excerpts: set[str] = set()
    if name:
        for chapter_idx, content in sorted((chapters_by_idx or {}).items()):
            if name not in (content or ""):
                continue
            for sentence in _split_sentences(content):
                if name not in sentence:
                    continue
                excerpt = sentence[:120]
                if excerpt in seen_excerpts:
                    continue
                seen_excerpts.add(excerpt)
                kinds = _occurrence_kinds(name, sentence)
                entry = {
                    "chapter_idx": chapter_idx,
                    "excerpt": excerpt,
                    "evidence_kinds": kinds,
                }
                (onscreen if kinds else mention_only).append(entry)
        for row in _shot_tag_hits(name, shot_rows):
            onscreen.append({
                "chapter_idx": None,
                "excerpt": str(row.get("source_excerpt") or row.get("action_desc") or "")[:120],
                "evidence_kinds": ["shot_tagged"],
            })
    return {
        "name": name,
        "onscreen_mentions": onscreen,
        "mention_only": mention_only,
        "recurrence": {
            "paragraph_count": len(onscreen) + len(mention_only),
            "chapter_count": len({
                e["chapter_idx"] for e in onscreen + mention_only
                if e["chapter_idx"] is not None
            }),
        },
    }


def has_onscreen_evidence(evidence: dict) -> bool:
    return bool((evidence or {}).get("onscreen_mentions"))


def functional_card_worthy(evidence: dict) -> bool:
    """无名功能身份是否值得单独建卡：出场证据 ≥2 段，或单段但同时具备对白与
    动作描写（真正的高潮单场景）。一句话路人（既无对白也无动作、或只出现一次
    且证据单薄）不建卡——避免把每一处偶然命中的动作邻接都当成建卡理由。"""
    onscreen = (evidence or {}).get("onscreen_mentions") or []
    recurrence = (evidence or {}).get("recurrence") or {}
    if int(recurrence.get("paragraph_count") or 0) >= 2:
        return True
    if len(onscreen) == 1:
        kinds = set(onscreen[0].get("evidence_kinds") or [])
        return {"dialogue", "action"} <= kinds
    return False


def presence_evidence_citation(evidence: dict, *, limit: int = 2) -> str:
    """给 reason 文案用的可读引用：最多 ``limit`` 条 onscreen 证据的
    "第N章「逐字片段」"，供人工/下游核对画面证据具体是什么。"""
    parts = []
    for item in ((evidence or {}).get("onscreen_mentions") or [])[:limit]:
        chapter = item.get("chapter_idx")
        label = f"第{chapter}章" if chapter is not None else "分镜"
        parts.append(f"{label}「{item.get('excerpt') or ''}」")
    return "；".join(parts)


def presence_evidence_fingerprint(evidence: dict) -> str:
    """证据指纹：只把 onscreen_mentions 折进哈希——mention_only 的变化不该让
    "非角色/戏份不足"的负缓存重新失效（只被提及的内容变了不代表在场证据变
    了）；真正需要重判的是"新出现的在场证据"（新章节写到这个人在场、或分镜
    后来把这个标签标成在场角色）。"""
    payload = json.dumps(
        [
            (item.get("chapter_idx"), item.get("excerpt"), sorted(item.get("evidence_kinds") or []))
            for item in (evidence or {}).get("onscreen_mentions") or []
        ],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fetch_project_shot_rows(conn, project_id: str) -> list[dict]:
    """项目内全部分镜的 characters/action_desc/source_excerpt，供画面存在证据
    交叉核对。读失败/表不存在时安全返回空列表——分镜数据是可选增强信号，不是
    必需输入：新角色发现常常发生在分镜生成之前，那时压根没有分镜数据。
    """
    try:
        rows = conn.execute(
            """SELECT s.characters AS characters, s.action_desc AS action_desc,
                      s.source_excerpt AS source_excerpt
                 FROM shots s JOIN episodes e ON e.id = s.episode_id
                 WHERE e.project_id = ? AND s.characters IS NOT NULL AND s.characters != ''""",
            (project_id,),
        ).fetchall()
    except Exception:  # noqa: BLE001 -- 缺表/迁移中等环境差异，证据源可选不必硬失败
        return []
    return [dict(row) for row in rows]
