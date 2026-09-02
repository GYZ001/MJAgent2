"""Prior-knowledge "suspected true name" hypothesis verification: building the
whole-book dossier for a candidate identity, the independent verdict model
call, and pinning the verdict's quote back into the dossier.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

import asyncio
from app.evidence import repository as evidence_repository
from app.schemas import Bible
from app.source_excerpt import index_source_segments
from pydantic import (
    BaseModel,
    ConfigDict,
)
from typing import (
    Any,
    Literal,
)

from .chunk_extraction import _call_structured
from .discovery import _load_project_bible
from .functional_candidates import _prep_pack_functional_candidate_roster


def _prep_pack_true_name_dossier(
    conn, project_id: str, alias: str, true_name: str,
) -> list[dict[str, Any]]:
    """1) 卷宗检索：零语义，纯字符串包含判断。扫描项目 chapters 全表
    （不限本集/前瞻窗口——EP2 的真实事故正是因为旧版只查了一个有限窗口，
    真正的反证/佐证段落可能在全书任何一章），按自然段
    （index_source_segments）逐段检查是否包含 alias 和/或 true_name。
    双词共现段全部保留；单词段超出预算时交给
    _prep_pack_sample_dossier_entries_within_budget 做确定性采样。每条记录
    额外带 ``entry_index``（1.10.0，缺陷 A 修复新增，见 PREP_PACK_VERSION
    上方大注释）：1-based、按本函数返回顺序（both 在前、single 采样结果在
    后）分配的扁平序号，供候选判别改用段号钉证（_prep_pack_true_name_pin_
    dossier_entry）——卷宗跨多章检索，(chapter_idx, segment_index) 二元组
    不是一个单值 enum 候选，需要一个扁平序号才能像 _prep_pack_functional_
    candidate_dossier 那样把钉证收紧成"选中的序号是否落在卷宗集合内"的结构
    判断。"""
    if not alias or not true_name:
        return []
    from app.portraits import CAST_DISCOVERY_SOURCE_BUDGET

    both: list[dict[str, Any]] = []
    single: list[dict[str, Any]] = []
    rows = conn.execute(
        "SELECT idx, content FROM chapters WHERE project_id=? ORDER BY idx",
        (project_id,),
    ).fetchall()
    for row in rows:
        chapter_idx = int(row["idx"])
        content = str(row["content"] or "")
        if alias not in content and true_name not in content:
            continue
        for segment_index, segment in enumerate(
            index_source_segments(content), start=1,
        ):
            has_alias = alias in segment.text
            has_true_name = true_name in segment.text
            if not has_alias and not has_true_name:
                continue
            entry = {
                "chapter_idx": chapter_idx, "segment_index": segment_index,
                "text": segment.text,
            }
            (both if has_alias and has_true_name else single).append(entry)
    dossier = list(both)
    used_chars = sum(len(item["text"]) for item in dossier)
    remaining_budget = max(0, CAST_DISCOVERY_SOURCE_BUDGET - used_chars)
    dossier.extend(
        _prep_pack_sample_dossier_entries_within_budget(single, remaining_budget)
    )
    for entry_index, item in enumerate(dossier, start=1):
        item["entry_index"] = entry_index
    return dossier


def _prep_pack_sample_dossier_entries_within_budget(
    entries: list[dict[str, Any]], char_budget: int,
) -> list[dict[str, Any]]:
    """单词段的确定性（非随机）等距采样：预算充足时全收；不足时按下标
    等距抽取，让样本铺满全书范围而不是只取前几章——同一份输入，任何时候
    重跑都得到一模一样的卷宗，可复现、可审计，这也是不能用随机采样的
    原因（审判程序的证据卷宗必须是确定性的，不能这次抽到反证下次抽不到）。
    """
    if not entries or char_budget <= 0:
        return []
    total_chars = sum(len(item["text"]) for item in entries)
    if total_chars <= char_budget:
        return list(entries)
    average_chars = max(1.0, total_chars / len(entries))
    approx_count = max(1, int(char_budget / average_chars))
    step = max(1.0, len(entries) / approx_count)
    picked_indexes = sorted({
        min(len(entries) - 1, int(i * step)) for i in range(approx_count)
    })
    selected: list[dict[str, Any]] = []
    used = 0
    for index in picked_indexes:
        entry = entries[index]
        entry_chars = len(entry["text"])
        if used + entry_chars > char_budget:
            continue
        selected.append(entry)
        used += entry_chars
    return selected


# 候选判别响应（1.10.0，缺陷 A 修复，见 PREP_PACK_VERSION 上方大注释）：
# 替换掉旧版 same/different/uncertain 是非题——selected_candidate 是一道
# 候选选择题（候选集 = suspected_true_name 本身 ∪ 人物谱/场景谱里在卷宗
# 文本中有字面命中的其它候选 ∪ 显式"都不是/无法确定"），跟
# _PrepPackFunctionalCandidateVerdict 同一范式，两者独立定义，互不复用
# （问的语义/候选来源不同，见 _prep_pack_true_name_verdict_candidates 与
# _prep_pack_true_name_verdict 的说明）。supporting_entry_index 钉的是
# 卷宗目录里的候选编号（entry_index，见 _prep_pack_true_name_dossier），
# 不是逐字引句——真实生产数据证明逐字引句钉证在跨章场景下会被模型的
# 拼接/摘要噪音系统性误杀。supporting_quote 保留为可选观测字段，不参与
# 判定。
class _PrepPackTrueNameVerdictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_candidate: str
    supporting_entry_index: int | None = None  # 选「都不是/无法确定」时填 null（ERR-20260902-205c51 同族）
    supporting_quote: str = ""


_PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL = "都不是/无法确定"

# 裁决提示词按 subject_kind 分域的措辞表（独立评审 blocker：本函数被角色
# 分支 resolve_fn=_resolve_portrait_id 与场景分支 resolve_fn=
# _resolve_scene_reference_id 共用，旧版提示词硬编码"是否指同一个人"——
# 场景假设走到这里时模型被问"这两个是不是同一个人"，语义错误，裁决不可靠。
# noun_label 是候选的名词身份；same_subject 是任务句里"是否指同一 X"的
# X，结构（卷宗引用、显式拒绝出口）跟 1.10.0 改动前完全一致，只换名词。）
_TRUE_NAME_VERDICT_SUBJECT_COPY: dict[str, dict[str, str]] = {
    "character": {"noun_label": "人名", "same_subject": "同一个人"},
    "scene": {"noun_label": "地点名", "same_subject": "同一个场景或地点"},
}


def _prep_pack_true_name_verdict_roster(
    bible: Bible, subject_kind: Literal["character", "scene"],
) -> dict[str, list[str]]:
    """候选面快照（1.10.0，缺陷 A 修复）：规范名 -> [规范名, 已确认别名...]，
    按 subject_kind 分流。character 分支直接复用 _prep_pack_functional_
    candidate_roster（人物谱同一构造，避免重复实现）；scene 分支同构，读
    bible.scenes[].aliases（纯字符串列表，跟 Character.aliases 的
    CharacterAlias 对象列表结构不同，见 app.schemas.Scene 字段说明）。"""
    if subject_kind == "character":
        return _prep_pack_functional_candidate_roster(bible)
    return {scene.name: [scene.name, *scene.aliases] for scene in bible.scenes}


def _prep_pack_true_name_verdict_candidates(
    dossier: list[dict[str, Any]], roster: dict[str, list[str]], true_name: str,
) -> list[str]:
    """候选判别候选集（1.10.0，缺陷 A 修复）：确定性、零语义——人物谱/
    场景谱（按 subject_kind 对应的 roster）里，规范名或已确认别名在卷宗
    （已经检索出的真实原文段落，覆盖全书范围）文本里逐字命中的候选。这样
    候选永远有真实卷宗材料支撑，不会出现"选项本身卷宗里毫无证据"的名存
    实亡选择题。``true_name``（即 suspected_true_name，被验证的假设）永远
    强制在候选集内——dossier 检索本身就是按"含 alias 和/或 true_name"筛选
    出来的，卷宗非空时通常已经命中，这里防御性再保证一次，候选判别不能连
    被测假设本身都问不出来。不针对任何具体人名/场景名做特判——candidates
    完全来自卷宗文本与人物谱/场景谱两份结构化数据的逐字包含判断，跟
    _prep_pack_functional_candidate_names 同一纪律。"""
    dossier_text = "".join(item["text"] for item in dossier)
    candidates = [
        name for name, forms in roster.items()
        if any(form and form in dossier_text for form in forms)
    ]
    if true_name not in candidates:
        candidates.insert(0, true_name)
    return candidates


async def _prep_pack_true_name_verdict(
    *, run_id: str | None, episode_id: str, project_id: str | None,
    subject_kind: Literal["character", "scene"],
    alias: str, true_name: str, dossier: list[dict[str, Any]],
    candidates: list[str],
) -> _PrepPackTrueNameVerdictResponse:
    """2) 裁决：唯一一次模型调用，只给卷宗原文 + 候选名单，不携带任何
    "我怀疑 X 就是 Y"的推理引导——问"称谓 alias 最可能指候选中的哪一位/
    哪一处"，候选集之外强制一个"都不是/无法确定"选项（1.10.0，缺陷 A
    修复，见 PREP_PACK_VERSION 上方大注释：旧版 same/different/uncertain
    是非题诱发确认偏误，本项目已有四次真实误绑事故）。``subject_kind`` 只
    决定问的是"同一个人"还是"同一个场景或地点"这一个名词，卷宗引用/
    显式拒绝出口等结构完全不变（见 _TRUE_NAME_VERDICT_SUBJECT_COPY
    上方注释）。"""
    copy = _TRUE_NAME_VERDICT_SUBJECT_COPY[subject_kind]
    noun_label = copy["noun_label"]
    same_subject = copy["same_subject"]
    catalog = "\n\n".join(
        f"[候选{item['entry_index']}][第{item['chapter_idx']}章·段{item['segment_index']}] "
        f"{item['text']}"
        for item in dossier
    )
    entry_indexes = [item["entry_index"] for item in dossier]
    candidate_options = [*candidates, _PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    prompt = f"""下面是从原著全书范围内检索到的原文段落，与称谓"{alias}"或候选{noun_label}
有关（出现顺序不代表任何推断结论），每段前标了候选编号：
{catalog}

候选{noun_label}名单（判别范围仅限以下几项，不要引入名单之外的{same_subject}）：
{candidate_list}

任务：仅依据以上原文段落本身，判断称谓"{alias}"是否与候选名单中的某一位属于
{same_subject}，是的话具体是哪一位。
- selected_candidate 必须从候选名单中选一个精确的{noun_label}；原文不足以确定
  "{alias}"具体对应候选中的哪一个时，选"{_PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL}"，
  不要勉强给出确定结论；不要因为某个候选在段落里出现次数多、看起来眼熟就倾向选它，
  只依据原文是否真的能确定二者是{same_subject}；
- supporting_entry_index：选了具体候选时必须填上面某个候选编号（取值只能是 {entry_indexes} 之一），
  选你得出这个结论最主要依据的那一段；选"{_PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL}"时填 null；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    schema = _PrepPackTrueNameVerdictResponse.model_json_schema()
    # 参照 _prep_pack_functional_candidate_call 对 output_schema 注入 enum
    # 的写法：候选段号、候选名单都收紧到本次实际可用的集合，模型在协议层面
    # 就选不出卷宗外的编号或候选集之外的人/地；真正生效的核验仍在
    # _prep_pack_true_name_pin_dossier_entry 与
    # _prep_pack_verify_true_name_hypothesis 里做代码侧结构校验。
    schema["properties"]["supporting_entry_index"] = {"type": ["integer", "null"], "enum": [*entry_indexes, None]}
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_true_name_verdict",
        prompt=prompt,
        model_type=_PrepPackTrueNameVerdictResponse,
        schema_name="episode_prep_pack_true_name_verdict_v2",
        operation_id=(
            f"episode_prep_pack:{episode_id}:true_name_verdict:"
            + evidence_repository.content_hash({
                "subject_kind": subject_kind,
                "alias": alias, "true_name": true_name,
                "candidates": candidates,
                "dossier": [item["entry_index"] for item in dossier],
            })
        ),
        max_tokens=500,
        output_schema=schema,
        # 低温：这道闸的语义判断要稳定——同一份卷宗重跑不该一次选中一次
        # 不确定，跟 _prep_pack_functional_candidate_call 同一考量。
        temperature=0.0,
        call_meta={
            "stage_key": "episode_prep_pack_true_name_verdict",
            "episode_id": episode_id,
            "subject_kind": subject_kind,
            "project_id": project_id,
            "candidates": candidates,
        },
    )


def _prep_pack_true_name_pin_dossier_entry(
    dossier: list[dict[str, Any]], entry_index: Any,
) -> dict[str, Any] | None:
    """3) 钉证：结构性核验，模型只需引用卷宗目录里某个候选编号
    （entry_index），不要求逐字复述原文（1.10.0，缺陷 A 修复，见
    PREP_PACK_VERSION 上方大注释）——真实生产数据（provider_calls
    id=9700/10498）证明旧版逐字引句比对会被模型的跨段拼接/摘要噪音系统性
    误杀（同一失败模式 stages.py._alias_verdict_pin_segment 已经修过一次，
    见该函数 docstring），跟 _prep_pack_functional_candidate_pin_segment
    同一修法：卷宗内容本身就是代码检索出的真实原文，模型选中某一条不存在
    "编造"或"转录出错"的空间，钉证退化为一次整数是否落在集合内的结构性
    判断。非法输入（不是整数、或不在本次卷宗集合内）一律返回 None。"""
    try:
        target = int(entry_index)
    except (TypeError, ValueError):
        return None
    for item in dossier:
        if item["entry_index"] == target:
            return item
    return None


# K/M 共用的并发失败语义（任务②，见 PREP_PACK_VERSION 上方大注释"并发闸"
# 一节）：不用 asyncio.gather 的默认异常语义——默认模式下第一个抛异常的
# 任务会立刻让 gather 重新抛出，但其它还没跑完的任务不会被取消，会在后台
# "孤儿"运行到自己结束，它们的返回值/异常因为没人再等待而被静默丢弃（这是
# asyncio.gather 本身有文档记载的既有行为，不是这里才有的新坑）。改用
# return_exceptions=True 让 gather 等全部任务真正跑完（成功或失败）才
# 返回，再按传入顺序扫一遍结果，遇到第一个异常就原样重新抛出（``raise
# result`` 重新抛出的是同一个异常对象，自带原始 traceback，不是包一层
# 新异常）——不吞、不改写、不静默降级，只是把"谁先失败就立刻甩出、其它
# 任务放养"改成"全部等完再决定失败"，避免孤儿任务与未被读取的异常。没有
# 任何任务失败时原样按输入顺序返回全部结果（跟 asyncio.gather 默认返回值
# 同形状——asyncio.gather 本身就保证结果顺序等于传入顺序，不是完成顺序，
# 这里复用这个既有保证，不需要额外排序）。K（真名核验）、M（候选判别）
# 两条并发化的循环共用这一份失败语义，不分别各写一套。
async def _prep_pack_gather_concurrent(coros: list) -> list:
    results = await asyncio.gather(*coros, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


# K：真名核验并发化（任务②，见 PREP_PACK_VERSION 上方大注释）——单集耗时
# 里 provider_calls 延迟占墙钟 99.4%，_pass() 内角色/场景两支
# suspected_true_name 核验此前对每条提及各自 await 一次
# _prep_pack_verify_true_name_hypothesis，彼此互不依赖却排成一条串行链
# （10 集测量：41 次调用、串行 204.6 秒，6 并发估算 57.8 秒）。核验函数
# 自己已经按 (subject_kind, alias, suspected_true_name) 三元组去重
# （true_name_verdict_cache，见该函数 docstring）——直接把它塞进
# asyncio.gather 会破坏这条去重语义：并发下同一个三元组的多次调用会同时
# 未命中缓存、同时发起模型调用，重复裁决同一件事。修法不是改核验函数本身
# （去重逻辑本来就是对的），而是在真正发起并发调用之前先做一次去重——本
# 函数只做纯读的收集：扫一遍 events，按跟 _pass() 内两处调用点完全相同的
# 判据（角色分支不看 discovery 改名、场景分支要求未经 discovery 改名，
# 逐条对齐，不是重新发明一套判据）算出这一遍 _pass() 需要核验的全部
# (subject_kind, alias, suspected_true_name) 三元组，用 dict 保序去重（同一
# 三元组在多个事件里重复出现是常态，如 1.10.0 缺陷 B 注释里"许姓女子"在
# 4 个事件都出现的真实案例）。调用方（_pass 顶部）用这份去重后的清单过滤掉
# true_name_verdict_cache 里已经有的键（跨 pass1/pass2 复用，语义不变），
# 对剩余的键一次性 asyncio.gather——键已经去重，gather 内不会有两个任务
# 争抢同一个三元组。gather 跑完后，_pass() 原有的逐条 await 调用完全不动
# （见下面两处调用点及其上方大段既有注释）：它们会命中刚刚写热的缓存，
# 同步立即返回，不产生第二次模型调用，也不改变原有的任何一行判定/写回
# 逻辑——并发只发生在"值都还没算出来"的那一刻，一旦缓存写好，_pass() 剩下
# 的全部代码（characters/scenes/functional_extras 等共享字典的写回顺序）
# 100% 保持原来的确定性单线程顺序不变，不需要为并发单独设计写回排序规则。
def _prep_pack_collect_true_name_verification_requests(
    character_mentions: list[dict[str, Any]],
    scene_mentions: list[dict[str, Any]],
    character_rename: dict[str, str],
    scene_rename: dict[str, str],
) -> list[tuple[Literal["character", "scene"], str, str]]:
    """收集这一遍 _pass() 会触发核验的全部 (subject_kind, alias,
    suspected_true_name) 三元组，去重、保插入顺序（顺序只影响 gather 的
    任务提交顺序，不影响任何最终写回结果——见本函数上方注释）。判据必须
    跟 _pass() 内角色/场景两处调用点的既有 if 条件逐字对齐，这里不是重新
    定义一套判据，只是把同一个判据提前算一遍、抽出需要核验的键。2.0.0：
    入参从按事件分组的 ``events`` 改为扁平的 ``character_mentions``/
    ``scene_mentions``（事件分组已随 event_chain 一起撤销），判据本身
    逐字未变。"""
    requests: dict[tuple[Literal["character", "scene"], str, str], None] = {}
    for mention in character_mentions:
        name = str(mention["display_name"] or "").strip()
        if not name:
            continue
        resolved_name = character_rename.get(name, name)
        suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
        if suspected_true_name and suspected_true_name != resolved_name:
            requests[("character", name, suspected_true_name)] = None
    for mention in scene_mentions:
        name = str(mention["display_name"] or "").strip()
        if not name:
            continue
        resolved_via_discovery = name in scene_rename
        resolved_name = scene_rename.get(name, name)
        suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
        if (
            suspected_true_name
            and suspected_true_name != resolved_name
            and not resolved_via_discovery
        ):
            requests[("scene", name, suspected_true_name)] = None
    return list(requests)


async def _prep_pack_verify_true_name_hypothesis(
    conn, *, project_id: str, episode_id: str, episode_no: int, source_text: str,
    alias: str, suspected_true_name: str,
    subject_kind: Literal["character", "scene"], resolve_fn, run_id: str | None,
    bible: Bible | None = None,
    verdict_cache: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Verify (not trust) a model-declared ``suspected_true_name`` guess via
    the dossier trial procedure documented above. Returns a dict:
    ``accepted``；不通过时的 ``reason``（rejected_no_dossier/
    rejected_verdict_different/rejected_verdict_uncertain/
    rejected_entry_not_pinned/rejected_pinned_entry_missing_alias/
    rejected_dual_anchor_available_not_pinned/
    rejected_degraded_pin_out_of_episode，都归入观测的 rejected_verdicts
    概念，1.10.0 缺陷 A 修复新增后三种，见 PREP_PACK_VERSION 上方大注释）；
    通过时的 ``pinned_quote``/``pinned_chapter_idx``（分别供 provenance.
    anchor_phrase 与 method="resolution"/"resolution_forward" 判定）与
    ``dual_anchor``（1.10.0 新增：钉证命中的是双锚定条目还是退化后的
    集内别名指代条目——不可观测的静默降级本项目明令禁止，调用方须把这个
    布尔值写进 provenance/true_name_hints，见 _pass 里两处调用点）。
    ``subject_kind`` 区分角色分支（resolve_fn=_resolve_portrait_id）与
    场景分支（resolve_fn=_resolve_scene_reference_id）——两者共用本函数
    与下面的裁决调用，但问的语义不同（"同一个人" vs "同一个场景或地点"，
    见 _prep_pack_true_name_verdict 的 _TRUE_NAME_VERDICT_SUBJECT_COPY），
    调用点各自传对。``bible`` 由调用方（_resolve_assets 的 _pass 闭包）
    传入已加载好的项目圣经，避免每次核验重复查库；缺省时现查一次（防御性
    兜底，理论上调用点都会传）。``verdict_cache`` 是 _resolve_assets 级别
    按 (subject_kind, alias, suspected_true_name) 缓存的判决结果，同一次
    生成内重复出现的同一对提及不重复发起模型调用；subject_kind 纳入键是
    因为角色循环与场景循环共用同一个缓存字典，不按域隔离会导致跨域撞名
    时复用错误域的裁决（独立评审发现的 minor）。"""
    empty = {
        "accepted": False, "reason": "", "pinned_quote": "",
        "pinned_chapter_idx": None, "dual_anchor": False,
    }
    if not suspected_true_name:
        return empty
    if resolve_fn(conn, project_id, suspected_true_name, episode_no) is None:
        return empty
    cache_key = (subject_kind, alias, suspected_true_name)
    if verdict_cache is not None and cache_key in verdict_cache:
        return verdict_cache[cache_key]

    def _reject(reason: str) -> dict[str, Any]:
        result = {**empty, "reason": reason}
        if verdict_cache is not None:
            verdict_cache[cache_key] = result
        return result

    dossier = _prep_pack_true_name_dossier(conn, project_id, alias, suspected_true_name)
    if not dossier:
        return _reject("rejected_no_dossier")

    project_bible = bible if bible is not None else _load_project_bible(conn, project_id)
    roster = _prep_pack_true_name_verdict_roster(project_bible, subject_kind)
    candidates = _prep_pack_true_name_verdict_candidates(dossier, roster, suspected_true_name)
    # 双锚定是否结构上可能存在（1.10.0，缺陷 A 修复第②③点）：全卷宗（不只是
    # 模型最终钉中的那一条）是否存在同时逐字含 alias 与 suspected_true_name
    # 的条目——这份判断只用 both 桶天然的性质（budget 裁剪只影响 single 桶，
    # both 桶全收，见 _prep_pack_true_name_dossier docstring），不依赖模型
    # 这次选了哪一条，是纯粹的既有材料事实。
    dual_anchor_available = any(
        alias in item["text"] and suspected_true_name in item["text"] for item in dossier
    )
    response = await _prep_pack_true_name_verdict(
        run_id=run_id, episode_id=episode_id, project_id=project_id,
        subject_kind=subject_kind, alias=alias, true_name=suspected_true_name,
        dossier=dossier, candidates=candidates,
    )
    if response.selected_candidate != suspected_true_name:
        reason = (
            "rejected_verdict_uncertain"
            if response.selected_candidate == _PREP_PACK_TRUE_NAME_VERDICT_NO_MATCH_LABEL
            else "rejected_verdict_different"
        )
        return _reject(reason)
    pinned = _prep_pack_true_name_pin_dossier_entry(dossier, response.supporting_entry_index)
    if pinned is None:
        return _reject("rejected_entry_not_pinned")
    # 钉证至少要求引句逐字包含被解析的那个别名本身（1.10.0，缺陷 A 修复
    # 第②点）：这是"零保护"的主要来源——生产数据实测 114 条真实 same 判决
    # 里，56 条（49%）引用的支撑句缺 alias/true_name 至少一个；只看明确
    # 询问人名的，18/75（24%）里 2 条连 alias 本身都不含，钉的是一句跟
    # 待判标签毫无关系的话。这一条对合法的跨章绑定（EP5"许姓女子"→"许清"
    # 那类，见下面 dual_anchor_available 分支）零伤害——集内指代段落
    # 天然含 alias 本身。
    if alias not in pinned["text"]:
        return _reject("rejected_pinned_entry_missing_alias")
    if dual_anchor_available:
        # 优先要求双锚定引句（1.10.0，缺陷 A 修复第③点）：卷宗结构上确实
        # 存在能同时证明 alias 与 true_name 的桥接句时，钉证必须钉在其中
        # 一条上——不能在更强证据摆在模型眼前时，仍然只钉一句弱证据（真实
        # 数据：18/75 里另有一部分是"卷宗里其实有更强证据，模型没用上"的
        # 形状，即使这次不专门统计，收紧钉证目标本身就同时堵住了这一类）。
        if suspected_true_name not in pinned["text"]:
            return _reject("rejected_dual_anchor_available_not_pinned")
        dual_anchor_used = True
    else:
        # 退化：全卷宗都不存在双锚定证据（结构性事实，不是这次没找到——
        # 真实 EP5 案例："许清"这个名字要到第34章才第一次在原著里出现，
        # 跟"许姓女子"永远不会同段共现，dual anchor 在这本书里对这对
        # (alias, true_name) 原理上不可能存在）。允许退化为"仅含别名的
        # 集内指代段落"——但必须真的是本集自己的段落，不是全书别处巧合
        # 复现的同一个短语（真实数据坐实的风险：proj_3ac0b627fa46 第981章
        # 也有一处"许姓女子"，却是完全不相关的转世预言片段，跟 EP5 本集
        # 语境毫无关系——不做这条限制，钉证可能钉在这类不相关的巧合复现
        # 上）。dual_anchor_used=False 是显式的可观测降级标记（本项目明令
        # 禁止静默降级），调用方须写进 provenance/true_name_hints。
        if pinned["text"] not in source_text:
            return _reject("rejected_degraded_pin_out_of_episode")
        dual_anchor_used = False

    result = {
        "accepted": True, "reason": "", "pinned_quote": pinned["text"],
        "pinned_chapter_idx": pinned["chapter_idx"], "dual_anchor": dual_anchor_used,
    }
    if verdict_cache is not None:
        verdict_cache[cache_key] = result
    return result

