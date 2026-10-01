"""道具段号全文补全（2026-10-01，用户反馈「分镜台道具大多没图」三路只读调查第③
项）：``discovery._prep_pack_build_prop_manifest`` 只把 ``segment_indexes`` 记到
模型这次申报并通过锚定核验的那些段落——同一件道具若在本集其它段落的原文里还会
再次出现（例如手机在原文第 7/16/20/24/54 段反复被人物操作），manifest 不会自动
把那些段落并进来，因为模型抽取是按 chunk 分批进行的，没有任何一次调用能看到整集
全文。这里做确定性检索 + 模型确认两步：先对 manifest 里每条道具，在本集全部原文
段落里重新找一遍它「已经被核验过的原文写法」，命中的段落只是候选；是否真的并入
``segment_indexes`` 要再经一次批量模型确认（2.0.13 新增，见下方「候选确认」一节）。
``provenance``（anchor_segments/anchor_phrase）保持调用方已经算好的锚点不变——
这一步只扩展"这件道具在哪些段落出场"的覆盖面，不改变"这条申报凭什么立住"的证据。

候选确认（2.0.13，defect 3 完整案情）：纯字面子串检索会把不同实物误并成一张卡——
真实案例（顾念长安第2集）道具卡「外套」只在第6段被核验（顾屿「自己的外套搭在她
肩上」），全文检索却命中了第5段陆一舟「牛仔外套」与第7段温念妈妈寄来的「厚外套」
（都只是因为原文里含有「外套」这两个字），三件不同人的东西被并成同一张卡的同一条
segment_indexes=[5,6,7]。子串检索本身继续保留做召回（手机那个真实案例要靠它才能
把本集反复出现的段落找全），但命中段落只作为候选，新增一次批量文本模型确认调用
（文本调用免费）逐条判断候选段落说的是不是同一件实物，只把确认为"是"的段号并入。
调用失败或返回不完整：不把未确认的候选并入，打一条 ``[PREP_PACK_PROP_COVERAGE_
UNCONFIRMED][未拦截]`` 可见信号，不静默吞掉（同 .discovery._prep_pack_record_
unanchored_prop 的处置惯例）。
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict

from app.schemas import Prop
from app.source_excerpt import SourceSegment

from .model_call import _call_structured

log = logging.getLogger(__name__)


def _prop_coverage_retrieval_words(
    entry: dict[str, Any], declared_text: str, cards_by_name: dict[str, Prop],
) -> list[str]:
    """候选检索词：这条道具的 ``label``、``source_wording``，以及——道具已经
    绑定既有卡（``canonical_name`` 非空）时——那张卡自己的 ``name`` 与每一个
    ``alias``。但只保留「本身已经在模型申报并通过锚定核验的段落（``entry
    ["segment_indexes"]`` 对应的原文拼接，即 ``declared_text``）里逐字命中过」
    的那些，其余一律丢弃，不参与下面的全集检索。

    不设最短长度魔数（数据推导，不是拍脑袋）：一个词只要已经在已核验段落里逐字
    命中过，就说明它在这段上下文里已经被证明确实指这件道具——继续拿它去全集
    检索是在复用已核验的证据，不是放行任意短词模糊匹配。真正会引发误命中的是
    「从未在任何已核验段落出现过」的词，这条过滤已经把它们排除在外；长度本身
    不是风险来源的充分或必要条件——例如道具卡别名"伞"（单字）若确实在已核验
    段落命中过，排除它不会减少误报，只会把这件道具真实的其它出场段落漏掉；反过来
    "一支缠着细银丝的木簪"（十个字）如果连在已核验段落里都没命中过，再长也不该
    被信任为检索词。"""
    candidates = [
        str(entry.get("label") or "").strip(),
        str(entry.get("source_wording") or "").strip(),
    ]
    canonical_name = entry.get("canonical_name")
    card = cards_by_name.get(canonical_name) if canonical_name else None
    if card is not None:
        candidates.append(str(card.name or "").strip())
        candidates.extend(str(alias or "").strip() for alias in card.aliases)
    # dict.fromkeys 去重且保序，比 set 更适合这里——后面只用于迭代，顺序无关
    # 紧要，但避免引入 set 遍历序不确定性这类已经在本仓库踩过的坑（参见
    # app.stages 说话人判定的历史教训），习惯性用保序去重。
    return [word for word in dict.fromkeys(candidates) if word and word in declared_text]


def _prop_coverage_request(
    entry: dict[str, Any], segments: list[SourceSegment], cards_by_name: dict[str, Prop],
) -> dict[str, Any] | None:
    """就地规范化 ``entry["segment_indexes"]``（丢弃越界编号，同修复前的既有
    行为），并算出候选检索词命中、但不在已核验范围内的"新增候选段"。没有
    新增候选时返回 None——entry 已经规范化，调用方不需要再做什么（不发起
    模型调用）。"""
    declared_indexes = [
        index for index in entry.get("segment_indexes") or []
        if 1 <= index <= len(segments)
    ]
    entry["segment_indexes"] = declared_indexes
    declared_text = "\n".join(segments[index - 1].text for index in declared_indexes)
    words = _prop_coverage_retrieval_words(entry, declared_text, cards_by_name)
    if not words:
        return None
    hits = {
        index for index, segment in enumerate(segments, start=1)
        if any(word in segment.text for word in words)
    }
    candidate_indexes = sorted(hits - set(declared_indexes))
    if not candidate_indexes:
        return None
    return {
        "entry": entry, "declared_indexes": declared_indexes,
        "candidate_indexes": candidate_indexes, "words": words,
    }


# 候选摘录窗口（2.0.13）：围绕命中词前后各留出的字数，供确认调用判断"这段
# 候选是不是同一件实物"。跟全仓同类"摘录原文给模型复核"的既有尺度同一量级
# （chunk_extraction.py 的 quote 约60字、plot_significant_quote 约40字）；
# 取 60 是留出足够上下文看清物主/场景归属（例如辨认"自己的外套"里的"自己"
# 指谁），同时不把整段原文（可能几百字）都摆进提示词。
_CANDIDATE_EXCERPT_WINDOW_CHARS = 60


def _first_matching_word(words: list[str], text: str) -> str:
    return next((word for word in words if word in text), words[0] if words else "")


def _candidate_excerpt(word: str, segment_text: str) -> str:
    pos = segment_text.find(word)
    if pos < 0:
        return segment_text
    start = max(0, pos - _CANDIDATE_EXCERPT_WINDOW_CHARS)
    end = min(len(segment_text), pos + len(word) + _CANDIDATE_EXCERPT_WINDOW_CHARS)
    return segment_text[start:end]


class _PropCoverageVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prop_id: str
    segment_index: int
    same_object: bool


class _PropCoverageConfirmResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[_PropCoverageVerdict]


def _prop_coverage_confirm_prompt(
    ids: list[str], requests: list[dict[str, Any]], segments: list[SourceSegment],
) -> str:
    blocks: list[str] = []
    for req_id, request in zip(ids, requests):
        entry = request["entry"]
        name = entry.get("canonical_name") or entry.get("label") or ""
        evidence = "；".join(segments[i - 1].text for i in request["declared_indexes"])
        lines = [f"[{req_id}] 名称：{name}｜描述：{entry.get('description') or ''}",
                 f"  已核验证据原文：{evidence}", "  候选段落："]
        for index in request["candidate_indexes"]:
            word = _first_matching_word(request["words"], segments[index - 1].text)
            excerpt = _candidate_excerpt(word, segments[index - 1].text)
            lines.append(f"    - segment {index}：……{excerpt}……")
        blocks.append("\n".join(lines))
    catalog = "\n\n".join(blocks)
    return f"""你在给一集短剧的道具做段号补全确认：每件道具已经在原文里被核验过至少一次
（下面"已核验证据原文"就是那次核验锚定的原文），现在用它已核验的写法在全文做了一次
字面检索，额外命中了下面列出的候选段落——命中只是因为那个词/写法出现在这段原文里，
不代表候选段落说的和已核验的是同一件实物：同一个写法完全可能指不同人身上、不同场合
的不同东西（例如"外套"可以是任何人穿的任何一件外套）。

逐条候选只判断一件事：候选段落里说的，和这件道具的"已核验证据原文"描述的，是不是
同一件实物——看候选段落自己写的物主/场景/描述是否与已核验证据一致：物主明确是另一个
人（例如已核验证据写的是某人"自己的"东西，候选段落写的是另一个人的同款）、或材质/
颜色/样式等描述明显矛盾，都判定不是同一件（same_object=false）；候选段落里没有任何
能分辨物主或外观的线索、你无法确定时，同样判定不是同一件——宁可漏补一次召回，也不要
把两件不同的东西并成一张卡。只有候选段落的描述与已核验证据吻合、或至少没有任何矛盾
迹象、且情节上明显是同一件东西在不同场合再次出现时，才判定是同一件（same_object=
true）。

{catalog}

对上面列出的每一条候选段落都要输出一条 {{"prop_id": "...", "segment_index": ...,
"same_object": true/false}}，prop_id/segment_index 必须原样取自上面列出的候选，
覆盖全部候选、不要遗漏，也不要输出列表之外的组合。
"""


def _apply_coverage_verdicts(
    ids: list[str], requests: list[dict[str, Any]],
    verdicts: list[_PropCoverageVerdict], episode_id: str | None,
) -> dict[str, set[int]]:
    """核验模型返回的 (prop_id, segment_index) 只能取自本次真实提供的候选集合
    （结构闸，同 ``_prep_pack_gate_segment_indexes`` 一脉——schema 层面不设
    per-item 动态 enum，这里做代码侧核验，道理同 card_match.py"模型提名、
    代码核验"）；缺席的候选打一条可见信号，不静默当作"不是同一件"悄悄放过。"""
    offered = {
        (req_id, index)
        for req_id, request in zip(ids, requests)
        for index in request["candidate_indexes"]
    }
    seen: set[tuple[str, int]] = set()
    confirmed: dict[str, set[int]] = {req_id: set() for req_id in ids}
    for verdict in verdicts:
        pair = (verdict.prop_id, verdict.segment_index)
        if pair not in offered:
            continue
        seen.add(pair)
        if verdict.same_object:
            confirmed[verdict.prop_id].add(verdict.segment_index)
    missing = offered - seen
    if missing:
        log.warning(
            "[PREP_PACK_PROP_COVERAGE_UNCONFIRMED][未拦截] 道具段号补全候选确认"
            "返回不完整，%d/%d 组候选缺席响应，缺席的一律不并入 segment_indexes，"
            "请人工核查 episode=%s 缺席组合=%s",
            len(missing), len(offered), episode_id, sorted(missing),
        )
    return confirmed


async def _confirm_prop_coverage_candidates(
    ids: list[str], requests: list[dict[str, Any]], segments: list[SourceSegment],
    *, run_id: str | None, episode_id: str | None,
) -> dict[str, set[int]]:
    """一次批量调用覆盖本次全部有新候选的道具（文本调用免费，不按道具逐条
    调用）。调用本身失败：不并入任何候选，打一条可见信号；不因此抛出异常
    阻断映射台发布——同 .prop_recheck.attach_prop_recheck 的处置。"""
    prompt = _prop_coverage_confirm_prompt(ids, requests, segments)
    try:
        response = await _call_structured(
            run_id=run_id,
            step_key="episode_prep_pack_prop_coverage_confirm",
            prompt=prompt,
            model_type=_PropCoverageConfirmResponse,
            schema_name="episode_prep_pack_prop_coverage_confirm_v1",
            operation_id=f"episode_prep_pack:{episode_id}:prop_coverage_confirm",
            max_tokens=4000,
            call_meta={
                "stage_key": "episode_prep_pack_prop_coverage_confirm",
                "episode_id": episode_id,
            },
        )
    except Exception:  # noqa: BLE001 - 确认失败不阻断映射台发布，见模块 docstring
        log.warning(
            "[PREP_PACK_PROP_COVERAGE_UNCONFIRMED][未拦截] 道具段号补全候选确认"
            "调用失败，本次新增候选一律不并入 segment_indexes，请人工核查 episode=%s",
            episode_id, exc_info=True,
        )
        return {}
    return _apply_coverage_verdicts(ids, requests, response.verdicts, episode_id)


async def fill_prop_segment_coverage(
    props_payload: list[dict[str, Any]], segments: list[SourceSegment], *,
    cards: Sequence[Prop] = (), run_id: str | None = None, episode_id: str | None = None,
) -> list[dict[str, Any]]:
    """manifest 建好之后的二次检索 + 模型确认（完整说明见模块 docstring）。

    ``segments`` 必须是本集全部原文段落（下标 1-based，与 segment_indexes
    同一编号体系）——不能只传某个 chunk 的子集，否则"本集其它段落是否再次
    出现"这条判据无从谈起。原地更新每条 entry 的 ``segment_indexes`` 并整体
    返回 ``props_payload``（不改 label/description/provenance 等其它任何
    字段，不增删条目）。没有任何新增候选时不发起模型调用（结构上就是同步
    的纯字面检索+范围规范化，不等待任何 I/O）。"""
    cards_by_name = {str(card.name): card for card in cards if card.name}
    requests = [
        request for entry in props_payload
        if (request := _prop_coverage_request(entry, segments, cards_by_name)) is not None
    ]
    if not requests:
        return props_payload
    ids = [f"p{i}" for i in range(len(requests))]
    confirmed = await _confirm_prop_coverage_candidates(
        ids, requests, segments, run_id=run_id, episode_id=episode_id,
    )
    for req_id, request in zip(ids, requests):
        request["entry"]["segment_indexes"] = sorted(
            set(request["declared_indexes"]) | confirmed.get(req_id, set())
        )
    return props_payload
