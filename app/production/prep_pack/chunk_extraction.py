"""Step/telemetry bookkeeping (_begin_step/_finish_step/_run_sync_step/
_run_async_step) and the structured model-call wrapper and per-chunk
extraction call (_call_structured/_extract_chunk).

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from app.evidence import repository as evidence_repository
from app.harness import model_gateway
from app.harness.contracts import get_contract
from app.observability.tracing import (
    bind_trace,
    current_trace,
)
from app.orchestration.state_machine import transition_step
from app.source_excerpt import SourceSegment
from contextlib import nullcontext
from pydantic import BaseModel
from typing import Any

from .chunking import _render_chunk
from .schemas import (
    _ChunkResponse,
    _response_format,
)


def _begin_step(run_id: str | None, step_key: str, *, iteration_no: int = 1) -> str | None:
    if not run_id:
        return None
    step_id = evidence_repository.create_step(
        run_id, step_key,
        iteration_no=iteration_no,
        agent_name="episode_prep_pack",
        contract_version=get_contract("screenplay").version,
    )
    transition_step(step_id, "PENDING", "READY", "输入已就绪", conn=None)
    transition_step(step_id, "READY", "RUNNING", "步骤开始", conn=None)
    return step_id


def _finish_step(step_id: str | None, exc: BaseException | None) -> None:
    if not step_id:
        return
    if exc is not None:
        transition_step(
            step_id, "RUNNING", "FAILED", str(exc)[:1000],
            decision="escalate", error_code=type(exc).__name__.upper(), conn=None,
        )
        return
    transition_step(step_id, "RUNNING", "SUCCEEDED", "步骤完成", decision="accept", conn=None)


def _run_sync_step(run_id: str | None, step_key: str, fn):
    """Wrap one deterministic (non-model-call) unit of work as an observable
    step, reusing the same create_step/transition_step machinery as the
    model-calling steps below -- so it shows up in the same observability
    trace with a registered business name (see
    app.orchestration.engine._STEP_PRESENTATIONS)."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _run_async_step(run_id: str | None, step_key: str, fn):
    """Async twin of ``_run_sync_step`` for one observable awaited unit of
    work (e.g. an app.portraits/app.scenes discovery call) that is not itself
    a structured model call through ``_call_structured``."""
    step_id = _begin_step(run_id, step_key)
    try:
        result = await fn()
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _call_structured(
    *,
    run_id: str | None,
    step_key: str,
    prompt: str,
    model_type: type[BaseModel],
    schema_name: str,
    operation_id: str,
    max_tokens: int,
    call_meta: dict[str, Any],
    iteration_no: int = 1,
    output_schema: dict[str, Any] | None = None,
    temperature: float = 0.2,
) -> Any:
    """``output_schema`` (1.10.0, 缺陷 A 修复引入)：可选的手写 JSON Schema
    覆盖——真名裁决候选判别需要把 selected_candidate/supporting_entry_index
    收紧到本次卷宗实际算出的 enum（参照 _prep_pack_functional_candidate_call
    对 model_gateway.chat_structured 的直接调用写法），而不是走
    ``_response_format``/``require_response_format`` 这条固定 schema 路径。
    传入时用 ``output_schema`` 直接驱动 provider 调用；不传（默认）时行为与
    改动前逐字节一致。这是同一个 step 封装（_begin_step/_finish_step 观测
    埋点）下的一个可选分支，不是新建一条调用路径。"""
    step_id = _begin_step(run_id, step_key, iteration_no=iteration_no)
    trace = current_trace()
    ctx = bind_trace(run_id, step_id, trace.trace_id) if run_id else nullcontext()
    try:
        with ctx:
            if output_schema is not None:
                result = await model_gateway.chat_structured(
                    [{"role": "user", "content": prompt}],
                    model_type=model_type,
                    validate=None,
                    operation_id=operation_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    format_retry_limit=1,
                    semantic_retry_limit=1,
                    call_meta=call_meta,
                    output_schema=output_schema,
                )
            else:
                result = await model_gateway.chat_structured(
                    [{"role": "user", "content": prompt}],
                    model_type=model_type,
                    validate=None,
                    operation_id=operation_id,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    format_retry_limit=1,
                    semantic_retry_limit=1,
                    call_meta=call_meta,
                    response_format=_response_format(model_type, schema_name),
                    require_response_format=True,
                )
    except BaseException as exc:
        _finish_step(step_id, exc)
        raise
    _finish_step(step_id, None)
    return result


async def _extract_chunk(
    *,
    episode_id: str,
    episode_no: int,
    chunk_index: int,
    chunk: list[tuple[int, SourceSegment]],
    known_characters: list[str],
    known_scenes: list[str],
    attempt_hint: str,
    run_id: str | None,
    confirmed_title_indexes: set[int] | None = None,
) -> _ChunkResponse:
    """2.0.0: 映射台的唯一模型调用——不再产出叙事内容（无 summary/事件/
    台词/hook/cliffhanger），只做资源发现+映射所需的原始素材申报：本段
    出现的人物/场景/道具，以及它们各自真正"画面出场"（不是被提及/回忆/
    转述）的段号。segment_indexes 是模型自己的语义判断（见 PREP_PACK_
    VERSION 上方 2.0.0 大注释"名字出现≠人在场"一节），下游
    _prep_pack_gate_segment_indexes 只做结构性核验（申报的段号是否落在
    这次调用真正看到的 chunk 范围内），不重复要求逐字命中——那道更细的
    证据闸留给 _resolve_assets 按 method 分支各自核验（见该函数与
    _prep_pack_gate_segment_indexes 各自的完整说明）。"""
    rendered = _render_chunk(chunk)
    hint = f"\n上一次尝试未通过校验，请修正：{attempt_hint}\n" if attempt_hint else ""
    # 1.9.0（继续保留，见 PREP_PACK_VERSION 上方大注释）：把确定性算出的
    # 章节标题段号作为既成事实告知模型，而不是让它再判断一遍——这些段号
    # 已经被 app.source_excerpt.chapter_title_segment_indexes 从
    # chapters.title 这个数据库锚点确定性算出，会无条件计入 coverage_
    # ledger.paratext（见 _prep_pack_build_coverage_ledger）。本 chunk
    # 不含任何这类段号时 chunk_title_indexes 为空，confirmed_title_
    # section 保持空字符串。
    chunk_title_indexes = sorted(
        index for index, _segment in chunk
        if index in (confirmed_title_indexes or ())
    )
    confirmed_title_section = ""
    if chunk_title_indexes:
        shown = "、".join(str(index) for index in chunk_title_indexes)
        confirmed_title_section = (
            f"编号 {shown} 已由系统确定性判定为本集所属章节的标题（排版元素，不是"
            "故事内容），已计入副文本账：不要为它们申报人物/场景/道具。\n\n"
        )
    prompt = f"""你在为一部网络小说改编的短剧做素材映射准备（不改编台词、不生成分镜、不写剧情摘要）。

任务：通读下面按顺序编号的原文片段（编号即 segment_index，本段范围 {chunk[0][0]}~{chunk[-1][0]}），
申报三类素材：

- characters：本段原文中画面里真正出场（不是被别人提起、回忆、转述、听说）的角色，每个给
  {{"display_name": "角色称谓", "suspected_true_name": "你认为的真名，不确定就填 null",
  "segment_indexes": [该角色真正在画面中出场的编号列表]}}；
  已登记角色名（仅供拼写对齐——如果原文本身就是这样称呼这个角色的，写法要跟登记名
  保持一致；原文没有这样称呼，就不要往上面靠）：{known_characters}；
- scenes：本段原文中角色实际所在的场景/地点，每个给 {{"display_name": "场景名",
  "suspected_true_name": "你认为的正名，不确定就填 null", "segment_indexes": [该场景实际
  在画面中出现的编号列表], "quote": "从上面 segment_indexes 任一编号原文中逐字摘录的一段
  原文（不超过约60字），要能证明这里写的就是这个地点——不得改写/概括/跨编号拼接；这个场景
  在本段确实没有可摘录的原文依据就填空字符串，绝不编造"}}；已登记场景名（仅供拼写对齐，同
  上一条的原则）：{known_scenes}；
- props：本段原文中画面里明确出现、有辨识度的物品/道具（不是随口一提，例如武器、信物、
  法宝、书信等），每个给 {{"label": "道具名称", "description": "这个道具的外观/特征简述",
  "segment_indexes": [该道具实际出现的编号列表]}}；没有就给空列表，不要为了填满而虚构。

segment_indexes 判据（硬性，对 characters/scenes/props 都适用）：只申报这个人物/场景/道具
真正在画面中出场/出现的编号——原文只是提到这个名字、被别的角色回忆/转述/听说、或只是
背景知识提及，都不算"出场"，不要申报那个编号；反之，只要真的在画面中出场，哪怕只是一句带过，
也要如实申报，不要漏报。

场景的持续性（仅适用于 scenes，硬性）：一个场景一旦在某个编号成立，只要后续编号里情节仍在
同一地点发生——哪怕那些编号没有再次提到地点名称或做任何环境描写，只是人物的对话/动作/
心理，这些编号依然属于这个场景，要一并计入它的 segment_indexes，不能因为某个编号本身没有
复述地点就漏报；只有当情节明确转移到另一个地点、或原文本身已经写明离开/切换（例如出门、
关门、赶路前往别处），才停止把新的编号计入这个场景、改记到新地点名下。一段原文里，人物
所在的地点几乎总是连续的，不要把 scenes 的申报窄化成"只在地点被提到的那一句"。

命名纪律（关于 characters/scenes 的 display_name，硬性）：
- display_name 必须逐字使用本段原文中出现的称谓——原文写"灰袍老者"就填"灰袍老者"，
  禁止填任何本段原文没有出现过的名字，哪怕你认为自己知道这个人物/地点的"真名"；
  display_name 永远不能被下面这条替换；
- characters 的 display_name 取词优先级（硬性）：如果本段原文中这个角色存在任何
  称谓性表述——人名、尊称、绰号等原文里其他人或旁白直接用来称呼这个人本人、
  能把他从人群里单独指认出来的说法——display_name 必须逐字采用其中一个，不允许
  改用你自己综合出的外貌、衣着、动作等描述性短语去代替，哪怕那个描述性短语同样
  逐字出现在原文里、哪怕你觉得它在这段更醒目或更容易辨认；只是概括这个人所属
  群体/类别、换成同一类别里的另一个人也同样适用的泛称（哪怕形式上像称呼），不算
  称谓性表述，仍属于描述性表述。原因：称谓才可能跟这个角色积累的其它信息对上号，
  描述性短语（含泛称）哪怕逐字为真，也只是这一段独有、认不出具体是谁的说法，会让
  一个本来可以确定身份的角色被迫退化成每次都要重新判断一遍是谁；只有当这个角色
  在本段原文里通篇只有描述性表述、完全没有任何称谓性表述时，才允许 display_name
  使用描述性短语——这种情况合法，不要因为这条优先级去勉强杜撰一个称谓出来；
- 如果这个角色在本段原文中存在不止一种称谓性表述，取本段内出现次数最多的那一个；
  次数相同就取最先出现的那一个；同一角色在本段全程都按这条规则统一取值一个
  display_name，不要换着用不同的称谓（可以有多个 segment_indexes，但 display_name
  只有一个）；
- 先验知识申报通道：你有可能在训练语料里读过这部小说——如果知道某个称谓背后的真名
  或正式名称，把它填进对应 mention 的 suspected_true_name（不确定就填 null，不要瞎猜
  硬填）；这只是申报，你的猜测会被本集原文/后续章节的文本证据核验，核验不过就不会
  被采用，绝不会被静默相信；
- 场景地点的 display_name 一律使用原文自己的描述词，不得替换成你认为等价的其他
  地名（哪怕原文的地点和你知道的某个地名指的是同一个地方，也只能照抄原文怎么说，
  真名假设同样走 suspected_true_name）。
{confirmed_title_section}{hint}
原文（本段共 {len(chunk)} 个编号片段）：
{rendered}
"""
    return await _call_structured(
        run_id=run_id,
        step_key="episode_prep_pack_event_chain_chunk",
        iteration_no=chunk_index,
        prompt=prompt,
        model_type=_ChunkResponse,
        schema_name="episode_prep_pack_chunk_v4",
        operation_id=f"episode_prep_pack:{episode_id}:chunk:{chunk_index}",
        max_tokens=8000,
        call_meta={
            "stage_key": "episode_prep_pack_event_chain",
            "episode_id": episode_id,
            "chunk_index": chunk_index,
        },
    )



# ---------------------------------------------------------------------------
# One generation attempt
# ---------------------------------------------------------------------------

# 2.0.0 新增：coverage_ledger 五账投影，直接基于已核验的 segment_indexes
# 并集计算，不再经过 app.validators.build_prep_pack_span_ledger 那套事件
# 跨度账本（该函数留在原地不动，见 PREP_PACK_VERSION 上方 2.0.0 大注释）。
# 语义：
#   delivered = 至少一条已核验（_prep_pack_gate_segment_indexes 通过）的
#     人物/场景/道具提及真正在此段出场；
#   paratext = 确定性章节标题段（deterministic_title_indexes）∪ 模型自报
#     且未与 delivered 冲突的 paratext_segments（冲突时 delivered 优先，
#     冲突记录进 rejected_paratext_claims 供观测，不静默吞掉）；
#   retained_as_context = 既不 delivered 也不 paratext 的其余全部段——
#     纯叙事推进、无新增可映射资产的段落，合法状态，不是缺陷；
#   merged/proven_duplicates 恒空（沿用既有惯例，这两账从未真正使用过）；
#   uncovered = 上面三账之外的段——结构上必然为空（total_segments 内每一
#     段要么 delivered、要么 paratext、要么 retained_as_context，穷尽三分
#     没有第四种可能），保留这个账户 + assert_prep_pack_coverage_complete
#     调用是纵深防御，不是这个函数本身可能产出非空 uncovered。
