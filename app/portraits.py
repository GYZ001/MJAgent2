"""人物定妆照（跨集一致性增强，PRD §5.4 第 2 层的时间维扩展）。

定妆照按"适用集区间"分段存于 character_portraits（ep_start/ep_end，ep_end=NULL 表示开区间=当前最新版）。
两条反应式产生路径都按集触发、不做全量轮询。新角色发现挂在【剧本阶段】并在正式剧本校验前完成，
分镜阶段保留幂等兜底；已有角色外观漂移仍在分镜展开前处理：
  ① 新角色发现：剧本里出现、人物谱里没有、戏份够的角色 → 建卡 + 定妆，适用集从首次出场那集起开放。
  ② 已有角色按集漂移：剧本里出现、本集之前已有定妆照的角色 → 用【本集源文】判断外观相比当前锚点
     是否明显变化：
       - 变化不大 → 沿用当前定妆照（开区间自然向后覆盖），不重绘、不花钱；
       - 变化很大 → 关闭当前定妆照右区间（= 本集-1），以当前定妆照为底【图生图】重绘新定妆照
         （左区间=本集、右区间开放），并把 bible 该角色锚点同步成最新（供人物谱 UI 展示）。

生成台/关键帧出图时按集号选用覆盖该集的定妆照与外观锚点：图走 portrait_for_episode，文字锚点走
bible_for_episode（把 bible 换成"本集视图"），二者同段同源（见 app.refs / app.video_modes / app.worker）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import config, hiagent, textmatch
from app.atomic_io import atomic_write_bytes
from app.character_policy import resolution_declares_functional_identity
from app.db import get_conn, get_setting, new_id, now, set_setting
from app.evidence import repository as evidence_repository
from app.evidence.media import record_reference_asset
from app.errors import ContentGenerationError, code_ref
from app.harness import model_gateway
from app.harness.types import EvidenceArtifact
from app.identity_authority import (
    IdentityAuthorityConflictError,
    identity_authority_registry,
    identity_resolution_is_authoritative,
    normalize_character_resolution,
    normalize_character_resolutions,
)
from app.orchestration.state_machine import StateConflict
from app.ingest import chapter_is_stub, chapter_titles_match
from app.refs import (
    PRODUCTION_APPEARANCE_MAX_CHARS,
    PRODUCTION_APPEARANCE_MIN_CHARS,
    _safe_name,
    portrait_prompt,
    production_appearance_anchor,
)
from app.schemas import Bible, Character, EpisodeScreenplay, extract_json
from app.source_excerpt import (
    SourceSegment,
    align_source_excerpt,
    index_source_segments,
)

FRAGMENT_WINDOW = 220   # 命中角色名前后各取多少字
FRAGMENT_BUDGET = 4000  # 单角色单段送审片段总字数预算
APPEARANCE_MIN = PRODUCTION_APPEARANCE_MIN_CHARS
APPEARANCE_MAX = PRODUCTION_APPEARANCE_MAX_CHARS
STAGED_INITIAL_EP_START = 2_147_483_647  # 候选包不得命中任何真实集号
CAST_DISCOVERY_SOURCE_BUDGET = 18000
CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET = 8000
CHARACTER_CARD_MAX_TOKENS = 4096
IDENTITY_DISCOVERY_CONTRACT_VERSION = "screenplay-identity-discovery.v16"
CURRENT_IDENTITY_DECISION_VERSION = "screenplay-current-identity.v18"  # v18:
# 真实 EP1 回归 ERR-20260826-d6fba4（proj_3ac0b627fa46/ep_3d523ff4d0a4，
# run_c313b5138699，provider_calls.id=11909，contract_version=screenplay-
# identity-discovery.v16）：K:E001:2690631a491d4e5ef3729ebf 把
# ['孟才子','孟兄']、K:E024:a3d42d9e45e09ef8776d0901 把 ['王伯的儿子']、
# K:E052:38f03a2cabff8be22d106f12 把 ['许师姐'] 填进了各自的
# absorbed_functional_keys，全部命中 v17 那道越界核验（安全默认，见
# _project_current_identity_response K 循环注释）被拒绝，整集 quality_gate
# 硬失败停跑。四个 token 均不在本批 F 声明、前批 P token 或既有 functional
# 组任一来源里——它们是有名有姓角色的其它称谓（孟才子/孟兄=孟浩、王伯的
# 儿子=王有材、许师姐=许清），模型想表达"这是同一个人"语义不算错，但用
# 错了通道。这是与 v17 同族但不同症状的变体：v17 只堵了"K 决议吸收自己的
# 锚定 source_label"这一个具体写法，从未正面陈述 absorbed_functional_
# keys 的完整合法取值域，模型换一种越界方式（吸收别人的称谓而非自己的）
# 照样命中同一道核验——补了实例、没补判据。真正根因还是 prompt 规则 9：
# 只改了下方规则 9 的措辞，把"禁止某个具体写法"改成"正面陈述可填值的完整
# 判据"（合法域=本批 F 声明过的 key/前批 P token/既有 functional 组，且
# 这三类来源的共同前提是背后实体仍处于"真名未定"的功能性占位状态；已有
# 确定真名之人的其它称谓从一开始就不满足这个前提，不得为了吸收而现造一条
# f 项）。不放宽越界核验本身，也不改判成可重采样的格式族——这两条结论 v17
# 已经写死，本次不推翻。换版本号只是为了让这条 prompt 变化生效——不换会让
# current_evidence_catalog_hash 相同、current_identity_version 仍是 v17
# 的旧输入，命中 discover_character_candidates 里 screenplay_identity_
# discovery 的已验证缓存工件（cached.get("current_identity_version") ==
# CURRENT_IDENTITY_DECISION_VERSION 那段），把旧 prompt 下的候选静默当成
# 新 prompt 下的结果复用，与 v14/v15/v16/v17 换版本号是同一个理由。
# v17:
# 真实 EP5 回归 ERR-20260825-0d8a29（proj_3ac0b627fa46/ep_0a7130b7b402，
# provider_calls.id=11141）：K 决议把自己的锚定 source_label（即同一
# decision_id 在本批 K 目录里自带的 source_label，如「许师姐」「孟浩」）也
# 填进了自己的 absorbed_functional_keys，被 _project_current_identity_
# response 的越界核验拒绝（安全默认，见该函数 K 循环注释），整集 quality_
# gate 硬失败停跑。核对真实 request/response：absorbed_functional_keys 在
# wire schema 里没有 enum（批内 f 项自造的 F1/F2 key 在 schema 构建时还不
# 存在，无法预先枚举，见 CurrentKnownIdentityDecision.absorbed_functional_
# keys 字段注释）——这条越界是纯 Python 侧跨字段核验，不是 wire-schema 已
# 声明约束，按既有先例（task #35，_CurrentIdentitySchemaViolation 只覆盖
# 真正的 enum/required/additionalProperties 违规）必须留在语义族硬失败，
# 不得改判格式族重采样，也不放宽这道核验本身。真正根因是 prompt 规则 9
# 没说清楚"决议自己的锚定 source_label 不需要、也不允许再吸收自己"：只改
# 了下方规则 9 的措辞。换版本号只是为了让这条 prompt 变化生效——不换会让
# current_evidence_catalog_hash 相同、current_identity_version 仍是 v16
# 的旧输入，命中 discover_character_candidates 里 screenplay_identity_
# discovery 的已验证缓存工件（cached.get("current_identity_version") ==
# CURRENT_IDENTITY_DECISION_VERSION 那段），把旧 prompt 下的候选静默当成
# 新 prompt 下的结果复用，与 v14/v15 换版本号是同一个理由。
# v16:
# 人物谱持久别名（Character.aliases）并入 identity_authority_registry 的
# source_labels，且 _project_current_identity_response 里 name_kind!=
# personal_name 的短路新增"命中 reserved_authority_labels 则放行"分支（见
# 该函数内注释）。两者都改变了这份契约的决议语义：前者让 K 决议目录/
# reserved_authority_labels 内容本身变化（已随 evidence_catalog_hash 的
# contract_version 输入自然失效缓存），后者改变了对同一份 raw provider
# 响应中 n 项的后端解读结果——即使某一集的人物谱还没人登记别名、catalog
# 内容不变，这条解读规则本身也变了。contract_version 直接进 evidence_
# catalog_hash 的 hash 输入（见 _project_current_identity_response 调用处），
# 不换版本号会让同一份缓存 raw response 被新逻辑静默复用/重新解读，
# 与 v14/v15 换版本号是同一个理由。
# v15:
# k 项新增 absorbed_functional_keys（RCA ERR-20260824-bc3d14，见
# docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §2.7/§4.2 "同批折叠通道"）：模型
# 借此声明某个 K 决议吸收了本批/前批的哪些 functional 称谓组，替代此前
# 唯一能表达"这是同一个人"的违规写法（在 n 里重复申报已由 k 覆盖的身份）。
# schema 与 prompt 都变了，必须换版本号——不换会让 operation_id 撞上旧版
# 缓存的 response，静默复用不含 absorbed_functional_keys 的旧结果，本次
# 修复形同虚设（与 v14 的 scope_qualifier 换版本号是同一个理由）。
# v14: f 项新增 scope_qualifier（真实第18轮 EP10 回归 ERR-20260824-b16bb4，
# 结构性方案 a：唯一性判定键改为 (source_label, scope_qualifier) 复合键，
# 见 prompt 规则8与 _project_current_identity_response 的 by_label 分组
# 注释）。
CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION = (
    "screenplay-current-identity-evidence-receipt.v2"
)
CURRENT_IDENTITY_LITERAL_PROVENANCE = "owned_current_literal.v1"
CURRENT_IDENTITY_SYNTHETIC_PROVENANCE = "provider_synthetic_functional.v1"
IDENTITY_ADJUDICATION_SOURCE_PROVENANCE = "owned_ir_identity_adjudication.v2"
FUTURE_IDENTITY_DECISION_VERSION = "screenplay-future-identity.v14"  # v14:
# 事故 RCA（EP2「绿袍男子」误并入「李富贵」，proj_3ac0b627fa46）：当某个
# 待消歧组的标签在整段未来文本里从未逐字出现时，resolve_future_identity_
# candidates 原先仍会盲抓未来文本开头约 900 字符当作该组的证据窗口（纯
# 兜底，只为让 N: 分支仍有文本可看），但铸造可选决议目录时没有把"这段
# 证据是不是兜底取得"这件事考虑进去——窗口里偶然出现的任何已登记角色的
# 别名/真名，都会被当成"这就是该标签的身份证据"铸出 K: 选项，模型再据此
# 选中，就把两个不同的人错误地并成了一个。现在这种纯兜底证据不再铸造任何
# K: 选项，该组的可选项收窄到只剩 F:（证据不足）与 N:（若确实首次揭示了
# 新真名）。这改变的是发给模型的可选决议目录内容与后端对同一份未来文本的
# 解读结果——不换版本号，本次事故里已经生成并持久化的错误 K 决议
# （decision_contract_version 仍是 v13）会被 screenplay_identity_
# resolution_is_current_for_scope 判定为"仍然当前"而不会被重新解析，
# 修复形同虚设（与 CURRENT_IDENTITY_DECISION_VERSION 历次换版本号是
# 同一个理由）。
# 归一规则专用 resolution_kind（真实第26轮 EP5 回归 ERR-20260824-88ece5，见
# resolve_future_identity_candidates 内 normalize_identity_payload 的完整
# 说明）：跟 "known_named"/"new_named" 并列的第三种决议种类——模型把
# "引用已有身份"误说成 NEW（authority_ids 唯一命中，冗余而非幻觉），后端
# 确定性降格为对该已有身份的引用，不要求重新逐字锚定真名（锚点在该身份
# 初次签发时已经验过）。不出现在任何 provider 可选枚举里——纯后端内部
# 归一标记，从不作为 schema token 暴露给模型，不占用 FUTURE_IDENTITY_
# DECISION_VERSION 的契约版本号（wire schema/prompt 都未改变）。
REISSUE_KNOWN_RESOLUTION_KIND = "reissue_known"
STRUCTURAL_IDENTITY_COVERAGE_VERSION = (
    "screenplay-identity-structural-coverage.v6"
)
# 身份标签（source_label / 未来揭示的真名）的防御性长度上限。这不是业务约束
# 本身，只用来拦截模型输出中明显失控的超长值（如整段抄录原文）；真正的业务
# 约束是禁止携带 _IDENTITY_LIST_SEPARATOR_PATTERN 命中的分隔符标点或空白——
# 下游（plot_spine.who / dialogue speaker / information_ledger.speaker_id /
# voice_bible.speaker_id / scene.characters）按该 pattern 切分身份列表，源头
# 混入分隔符会让一个人被错误切成多段身份。生产事故：EP7 的
# source_label='一只约莫一人大小，样子如猴般的凶兽'（17 字）曾因超过旧
# max_length=16 被 pydantic 直接拒绝，而真正应该拒绝的原因是其中的全角逗号，
# 不是长度——10 字带逗号一样危险，30 字不带分隔符反而无害。与同文件
# functional_identity_key 的 max_length=64 对齐。
IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH = 64
# 称谓形态的优先级阶梯：真名 > 尊称 > 代称。
#
# 生产事故：第 1 集的后续窗口只写出「许师姐」，模型据此签发了一张全新人物卡
# ``bible:许师姐``，与人物谱里本来就有的「许清」构成同一个人的身份分裂，随后
# 第 5 集的场次身份注册表因为同一个称谓指向两个 canonical identity 而 fail-closed。
#
# 判据不能靠后缀词表（本项目明令禁止），只能由读得懂原文的模型给出形态判断；
# 后端拿到形态后确定性执行阶梯：只有真名可以签发新的人物权威，尊称与代称一律
# 先落为功能身份。这样「有真名就不能单独成角色」，而真名尚未出现时该人物仍然
# 是一个独立身份，等真名真正出现在证据里再由 K 决议认领同一个人。
# 身份合同的输出预算。推理模型的 reasoning token 计入 completion_tokens，
# 所以"够写下答案"并不等于"够跑完这次调用"：生产上换成推理模型后，
# 4096 的预算被推理吃光，returned finish_reason=length / completion_tokens=4097，
# 每一集都在人物预检确定性截断（EP4）。这里按输出上限的量级给足余量，
# 真正的成本仍由实际用量结算，预算只是不让推理把答案挤掉。
IDENTITY_REQUEST_MAX_TOKENS = 16384

IDENTITY_NAME_FORM_PERSONAL = "personal_name"
IDENTITY_NAME_FORM_HONORIFIC = "honorific"
IDENTITY_NAME_FORM_REFERENTIAL = "referential"
IDENTITY_NAME_FORMS = (
    IDENTITY_NAME_FORM_PERSONAL,
    IDENTITY_NAME_FORM_HONORIFIC,
    IDENTITY_NAME_FORM_REFERENTIAL,
)
IDENTITY_NAME_FORM_RULE = (
    "称谓形态优先级：真名 > 尊称 > 代称。"
    "personal_name=人物的真实姓名（姓+名或单名）；"
    "honorific=姓氏或关系加称呼（如「某师姐」「某爷」），不是真名；"
    "referential=只描述外形、衣着、身份或方位的代称。"
    "只有 personal_name 才能签发新的人物身份；"
    "尊称与代称必须留作功能身份，等真名在证据中出现后再由 K 决议认领同一个人。"
)


AUTOMATIC_IDENTITY_DECISION_PROVENANCE = "automatic_identity_discovery.v1"
DURABLE_IDENTITY_DECISION_PROVENANCE = frozenset({"manual", "bible"})


def screenplay_identity_scope_fingerprint(
    episode_no: int,
    source_text: str,
) -> str:
    return evidence_repository.content_hash({
        "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "episode_no": episode_no,
        "source_text": source_text,
    })


def _identity_operation_retry_epoch() -> str:
    """Fence raw provider idempotency to one authorized workflow attempt."""
    try:
        from app.observability.tracing import current_trace

        return str(current_trace().run_id or "").strip()
    except Exception:  # noqa: BLE001 - tracing is optional in isolated helpers
        return ""


# The identity contracts forbid structured retries because a second sample that
# is *shown* the first one's failure can be coached into fabricating compliance,
# and because a wrong-but-coherent answer must never be re-rolled until it
# happens to pass.  Both of those are about answers the provider actually
# authored.  An *undelivered* answer is a different animal: in production the
# response derailed mid-object into `{"decision_id": "f" : [` and no JSON object
# ever decoded, so there was no identity judgement to preserve, the outcome is
# known-bad rather than outcome-unknown, and nothing from the failed attempt
# reaches the next one.  Re-issuing the identical prompt under a fresh operation
# id is then the same act as an operator pressing retry, minus re-running the
# whole upstream pipeline.  The provider is asked for a strict json_schema
# response_format and demonstrably does not always honour it, so without this
# one clean resample a single corrupt sample costs a whole episode.
#
# A transport stall which delivered zero characters is the same animal, only
# more clearly so: the provider never authored anything at all.  The blueprint
# shard path already gives that case a fresh attempt for exactly this reason.
#
# Schema-invalid and semantically-invalid answers keep the strict one-call rule.
IDENTITY_UNUSABLE_RESPONSE_RESAMPLES = 1

# Real incident ERR-20260826-93c8e3 (run_f8a23b28d098/ep_e4b00ccc7db5, provider_calls
# id 12985/12986): the resample above was firing a byte-identical request -- same
# messages, same temperature, same schema -- as the failed first attempt (verified via
# provider_request_hash: both attempts hashed to f373f285a84961da09d2aa54).  Two calls
# with the same input landed on the same decision point and failed the same way, which
# means the "resample" was spending a second provider call to relearn the same outcome,
# not actually giving the model a second, different shot.  A resample must change
# something about the request, or it is not a retry, it is a repeat.
#
# This only fires once the first attempt is already known to carry zero identity
# judgement (unparseable format failure or an undelivered transport stall -- see the
# docstring below), so nudging the second attempt is not "re-rolling a wrong answer
# until it passes": there is no answer yet to preserve or bias.
#
# +0.2 keeps the resample's temperature well above the identity contracts' normal
# 0.05-0.1 range (enough to plausibly avoid repeating the same premature stop) while
# staying far short of a value that would let the model wander off the identity rules
# themselves -- the rules, schema and evidence requirements are untouched; only the
# sampling entropy and a narrow formatting reminder change.
IDENTITY_RESAMPLE_TEMPERATURE_BUMP = 0.2
IDENTITY_RESAMPLE_TEMPERATURE_CAP = 1.0
# Targets the specific failure shape seen in the incident: the model stopped
# (finish_reason=stop, not a token-budget truncation) after writing a complete-looking
# value but before closing every open container ('{'/'[' left unmatched at EOF). This
# reminder does not touch a single word of the identity-judgement rules -- only asks
# for syntactically closed JSON.
IDENTITY_RESAMPLE_FORMAT_REMINDER = (
    "\n\n【重试须知】上一次尝试没有交付一个语法完整的 JSON 对象（可能是提前结束、"
    "遗漏了收尾的 } 或 ]）。本次请确保输出的 JSON 里每一个 { [ 都有对应的 } ] 严格"
    "闭合，写完最后一个字段后立即补齐所有尚未闭合的容器，不要在结构闭合之前结束"
    "响应，也不要输出 JSON 对象之外的任何文字。除此之外，判断规则、证据要求与输出"
    "内容本身不变。"
)


async def _identity_structured_with_resample(
    messages: list[dict[str, str]],
    *,
    model_type: Any,
    validate: Callable[[Any], list[str]],
    max_tokens: int,
    operation_id_for_attempt: Callable[[int], str],
    call_meta: dict[str, Any],
    **kwargs: Any,
) -> Any:
    """Run one identity contract, resampling only an undelivered response.

    Each attempt still goes through the gateway's strict identity fence with
    ``format_retry_limit=0`` and ``semantic_retry_limit=0``; the resample is an
    authored second attempt at the caller, with its own operation id so cost
    and idempotency accounting stay exact.  Only an answer the provider never
    delivered is resampled -- a response that decoded into no JSON object at
    all, or a transport stall that produced zero characters.  Schema-invalid
    answers and business-validation failures are re-raised on the first
    attempt.

    A resample attempt (``attempt > 0``) must never send the byte-identical
    request the failed attempt sent -- see
    ``IDENTITY_RESAMPLE_TEMPERATURE_BUMP`` above for why replaying the same
    request is not a real second chance.  The bumped temperature and appended
    reminder apply only from the second attempt onward; the first attempt is
    unchanged from the caller's exact input.
    """
    last_error: Exception | None = None
    for attempt in range(IDENTITY_UNUSABLE_RESPONSE_RESAMPLES + 1):
        attempt_messages = messages
        attempt_kwargs = kwargs
        if attempt > 0:
            attempt_messages = list(messages)
            if attempt_messages:
                last_message = dict(attempt_messages[-1])
                last_message["content"] = (
                    str(last_message.get("content") or "")
                    + IDENTITY_RESAMPLE_FORMAT_REMINDER
                )
                attempt_messages[-1] = last_message
            attempt_kwargs = dict(kwargs)
            base_temperature = float(attempt_kwargs.get("temperature") or 0.0)
            attempt_kwargs["temperature"] = min(
                IDENTITY_RESAMPLE_TEMPERATURE_CAP,
                base_temperature + IDENTITY_RESAMPLE_TEMPERATURE_BUMP,
            )
        try:
            # model_type/validate/max_tokens stay explicit here so the
            # gateway call-site contract test still sees them named.
            return await model_gateway.chat_structured(
                attempt_messages,
                model_type=model_type,
                validate=validate,
                max_tokens=max_tokens,
                operation_id=operation_id_for_attempt(attempt),
                call_meta={**call_meta, "resample_attempt": attempt},
                **attempt_kwargs,
            )
        except model_gateway.StructuredFormatError as exc:
            if not getattr(exc, "unparseable", False):
                raise
            last_error = exc
        except hiagent.ProviderError as exc:
            # An answer the provider never delivered -- a stall before the
            # first character, or a stream cut before its own ``[DONE]`` --
            # holds no identity judgement to preserve, so this is the
            # undelivered case above rather than an answer being re-rolled
            # until it passes.  Every other failure class still fails closed
            # on the first call.
            if not hiagent.provider_answer_undelivered(exc):
                raise
            last_error = exc
    assert last_error is not None
    if isinstance(last_error, hiagent.ProviderError):
        raise hiagent.deterministic_undelivered_error(
            last_error,
            attempts=IDENTITY_UNUSABLE_RESPONSE_RESAMPLES + 1,
        ) from last_error
    raise last_error


def _canonical_named_authority_id(canonical_name: str) -> str:
    """Return the final backend authority used once a named card is committed."""
    value = str(canonical_name or "").strip()
    if not value:
        raise ValueError("named identity authority requires canonical_name")
    return f"bible:{value}"


def _named_candidate_materialization_compatible(item: dict) -> bool:
    """Whether adding the candidate's named card preserves one authority/group."""
    canonical_name = str(item.get("name") or item.get("canonical_name") or "").strip()
    authority_id = str(item.get("authority_id") or "").strip()
    if not canonical_name or not authority_id:
        return bool(item.get("materialization_compatible", True))
    canonical_authority = _canonical_named_authority_id(canonical_name)
    return bool(
        authority_id == canonical_authority
        and item.get("materialization_compatible", True)
    )


def _bounded_owned_identity_evidence(
    evidence_text: str,
    *,
    anchors: list[str],
    max_chars: int = 120,
) -> str:
    """Return one exact bounded window which retains an authority anchor."""
    text = str(evidence_text or "")
    limit = max(1, int(max_chars))
    matches = [
        (text.find(anchor), -len(anchor), anchor)
        for anchor in dict.fromkeys(
            str(value or "").strip() for value in anchors
        )
        if anchor and anchor in text
    ]
    if not matches:
        return ""
    offset, _negative_length, anchor = min(matches)
    if len(text) <= limit:
        return text.strip()
    left_room = max(0, (limit - len(anchor)) // 2)
    start = max(0, offset - left_room)
    end = min(len(text), start + limit)
    start = max(0, end - limit)
    excerpt = text[start:end].strip()
    return excerpt if anchor in excerpt else ""


# ---------- 原文片段抽取（纯本地，不调模型） ----------

def extract_character_fragments(text: str, name: str, *, window: int = FRAGMENT_WINDOW,
                                budget: int = FRAGMENT_BUDGET) -> str:
    """从正文里抽取提及 name 的片段（命中处前后 window 字），合并重叠区间，封顶 budget 字。"""
    if not name or not text:
        return ""
    spans: list[tuple[int, int]] = []
    for m in re.finditer(re.escape(name), text):
        spans.append((max(0, m.start() - window), min(len(text), m.end() + window)))
    if not spans:
        return ""
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out: list[str] = []
    used = 0
    for s, e in merged:
        if used >= budget:
            break
        piece = text[s:e].strip()[: max(0, budget - used)]
        if piece:
            out.append(piece)
            used += len(piece)
    return "\n……\n".join(out)


# ---------- 外观变化判定（调模型，按集一次批量判定） ----------

async def screen_appearance_changes(entries: list[dict], ep_label: str) -> dict[str, dict]:
    """一次调用，批量判断本集里哪些【已有定妆照】角色外观相比各自当前锚点发生【明显视觉变化】。

    entries: [{"name", "current_appearance", "fragments"}]（fragments 为空者会被忽略）。
    返回 {name: {new_appearance, reason, change_dimensions, persistence, evidence_excerpt}}，
    仅含确实变化、且给出了新锚点的角色。"""
    entries = [e for e in entries if (e.get("fragments") or "").strip()]
    if not entries:
        return {}
    blocks = []
    for i, e in enumerate(entries, 1):
        blocks.append(
            f"角色{i}「{e['name']}」\n当前定妆照外观锚点：{e.get('current_appearance') or '（无）'}\n"
            f"本集提及该角色的原文片段：\n{(e.get('fragments') or '')[:FRAGMENT_BUDGET]}")
    body = "\n\n".join(blocks)
    prompt = f"""任务：逐个判断下列小说人物在新一段剧情（{ep_label}）里，外观相比各自【既有定妆照】是否发生【明显视觉变化】。

{body}

判断口径：只依据原文与当前定妆照的可见、稳定、可跨镜复现差异；
不得用姓名、题材、称谓或固定词表猜测变化。没有直接证据时 changed=false。

对 changed=true 的角色，给出整合后的【新外观锚点串】new_appearance：40~60 字，沿用既有锚点未变部分，只改真正变化处；保留性别年龄感/发型发色/服装款式与颜色/标志性特征。
- 外观锚点只写中性站姿下直接可见、可跨镜稳定复现的静态形态，不写行为、关系或镜头状态。
同时给出：
- change_dimensions：开放的稳定变化维度数组，名称应直接描述本次结构化差异，不套固定分类词表
- persistence：persistent（跨集持续）/ episode（仅本集）/ shot_only（单镜临时，不应更新人物谱）
- evidence_excerpt：原文短片段依据
- identity_change_authorized：只有原文证据明确支持持久身份形态变化时为 true，否则为 false

只输出一个 JSON 对象：{{"changes": [{{"name": "角色名", "changed": true/false, "new_appearance": "", "change_dimensions": [str], "identity_change_authorized": bool, "persistence": "persistent", "reason": "一句话依据", "evidence_excerpt": "原文短片段"}}]}}"""
    raw = await model_gateway.chat(
        [{"role": "user", "content": prompt}], temperature=0.2, max_tokens=1600,
        call_meta={"stage": "screen_appearance_changes"},
    )
    obj = extract_json(raw)
    valid = {e["name"] for e in entries}
    out: dict[str, dict] = {}
    from app.multiview import normalize_appearance_change
    for item in (obj.get("changes") or []):
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if name not in valid or not bool(item.get("changed")):
            continue
        new_app = (item.get("new_appearance") or "").strip()
        if not new_app:
            continue  # 说变了却没给新锚点 → 保守沿用，不重绘
        normalized = normalize_appearance_change({**item, "character": name, "new_appearance": new_app})
        if normalized.get("persistence") == "shot_only":
            continue  # 临时状态不更新人物谱
        out[name] = {
            "new_appearance": normalized["new_appearance"][:APPEARANCE_MAX],
            "reason": normalized["reason"],
            "change_dimensions": normalized["change_dimensions"],
            "identity_change_authorized": normalized["identity_change_authorized"],
            "persistence": normalized["persistence"],
            "evidence_excerpt": normalized["evidence_excerpt"],
        }
    return out


# ---------- 新角色发现（剧本阶段反应式：按需检索原文判断戏份，够分量才建卡） ----------
#
# 设计：人物谱只在进项目时谱写一次；之后由剧本阶段触发——剧本里出现、人物谱里没有的名字，
# 向后检索若干章原文判断戏份，画面够多才单独建卡 + 定妆。必须在【分镜展开前】完成，
# 否则 validate_storyboard 会因"角色圣经中不存在"把新角色从分镜里刷掉。

IDENTITY_DISCOVERY_FORWARD_CHAPTERS = 10
CHARACTER_IMPORTANCE_FORWARD_CHAPTERS = 20
DISCOVERY_REJUDGE_WINDOW = 20     # 判过"戏份不足"的名字，隔多少集才重新评估一次（避免对龙套反复调模型）

# 同名角色卡的建卡互斥锁（逐集分镜并行时，两集可能同时发现同一新角色）。
_card_locks: dict[tuple[str, str], asyncio.Lock] = {}
_card_locks_guard = asyncio.Lock()
_bible_locks: dict[str, asyncio.Lock] = {}
_bible_locks_guard = asyncio.Lock()


async def _card_lock(project_id: str, name: str) -> asyncio.Lock:
    async with _card_locks_guard:
        key = (project_id, name)
        lock = _card_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _card_locks[key] = lock
        return lock


async def _bible_lock(project_id: str) -> asyncio.Lock:
    async with _bible_locks_guard:
        lock = _bible_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _bible_locks[project_id] = lock
        return lock


def _non_character_skip_key(project_id: str, name: str) -> str:
    """Durable record that the card layer judged this name not a character.

    The in-place demotion below only reaches in-process consumers, and
    ``ensure_cards_for_text`` copies its candidate dicts.  Structural coverage
    reads persisted artifacts, so the decision has to survive as its own
    durable fact or coverage will keep demanding a card that must never exist.
    """
    return f"char_not_character:{project_id}:{name}"


def _discovery_skip_key(project_id: str, name: str) -> str:
    return f"char_discovery_skip:{project_id}:{name}"


def _name_in_bible(conn, project_id: str, name: str) -> bool:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    return any((c.get("name") or "") == name for c in json.loads(row["bible_json"]).get("characters", []))


def _forward_fragments(
    conn, project_id: str, name: str, from_episode_no: int,
) -> tuple[str, str, dict[int, str]]:
    """保留原有人物重要性评估窗口，不与"未来 10 章找真名"耦合。

    王有材事故修复新增第三个返回值 chapters_by_idx（idx -> 完整章节原文，未经窗口化/
    预算截断）：供 assess_new_character 核验 source_evidence 使用——evidence_chapter_index
    要能对应到完整原文，不能只对应下面 fragments 里的窗口化片段。fragments 本身改为
    【第 N 章】分块标记格式（仿照同文件 _future_chapter_context 已用的方式），原格式是
    多章直接拼接成一整块文本，模型看不出章节边界，没法准确申报 evidence_chapter_index。
    """
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, from_episode_no)).fetchone()
    src = json.loads(ep["source_chapters"] or "[]") if ep and ep["source_chapters"] else []
    lo, hi = (min(src), max(src)) if src else (0, 0)
    rows = conn.execute(
        "SELECT idx, content FROM chapters WHERE project_id=? AND idx>=? AND idx<=? ORDER BY idx",
        (project_id, lo, hi + CHARACTER_IMPORTANCE_FORWARD_CHAPTERS)).fetchall()
    chapters_by_idx: dict[int, str] = {}
    blocks: list[str] = []
    used = 0
    for row in rows:
        content = row["content"] or ""
        try:
            idx = int(row["idx"])
        except (TypeError, ValueError):
            continue
        if content.strip():
            chapters_by_idx[idx] = content
        piece = extract_character_fragments(content, name)
        if not piece or used >= FRAGMENT_BUDGET:
            continue
        block = f"【第 {idx} 章】\n{piece}"
        blocks.append(block)
        used += len(block)
    return (
        "\n\n".join(blocks),
        f"第 {from_episode_no} 集相关章节 +{CHARACTER_IMPORTANCE_FORWARD_CHAPTERS} 章",
        chapters_by_idx,
    )


def _future_chapter_context(
    conn,
    project_id: str,
    from_episode_no: int,
) -> tuple[str, str]:
    """读取本集源章节之后的小段原文，只用于角色姓名消歧。

    后续文本不会作为本集剧情素材传入剧本生成；它只在人物发现的
    受限 Prompt 中出现，用来回答“大汉/老者/黑衣人后来叫什么”。
    """
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, from_episode_no),
    ).fetchone()
    try:
        source_chapters = json.loads(ep["source_chapters"] or "[]") if ep else []
        chapter_indexes = [int(value) for value in source_chapters]
    except (TypeError, ValueError, json.JSONDecodeError):
        chapter_indexes = []
    if not chapter_indexes:
        return "", "无后续章节线索"
    last_source_chapter = max(chapter_indexes)
    last_discovery_chapter = last_source_chapter + IDENTITY_DISCOVERY_FORWARD_CHAPTERS
    rows = conn.execute(
        "SELECT idx, content FROM chapters "
        "WHERE project_id=? AND idx>? AND idx<=? ORDER BY idx",
        (project_id, last_source_chapter, last_discovery_chapter),
    ).fetchall()
    blocks = [
        f"【第 {row['idx']} 章】\n{(row['content'] or '').strip()}"
        for row in rows
        if (row["content"] or "").strip()
    ]
    return (
        "\n\n".join(blocks),
        f"第 {last_source_chapter + 1}-{last_discovery_chapter} 章（仅姓名消歧）",
    )


def _draft_identity_projection(draft_text: str) -> str:
    """Project only typed identity carriers from a screenplay draft."""
    if not draft_text:
        return ""
    try:
        script = EpisodeScreenplay.model_validate_json(draft_text)
    except (TypeError, ValueError):
        return json.dumps(
            {"parse_status": "invalid", "identity_mentions": []},
            ensure_ascii=False,
        )

    mentions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    structured_turn_surfaces: set[tuple[str, str]] = set()

    def add(value: object, path: str, *, line_context: str = "") -> None:
        text = str(value or "").strip()
        key = (text, path)
        if text and key not in seen:
            seen.add(key)
            mention = {"value": text, "path": path}
            context = str(line_context or "").strip()[:160]
            if context:
                mention["line_context"] = context
            mentions.append(mention)

    for scene_index, scene in enumerate(script.scene_outline or []):
        for character in scene.characters or []:
            add(character, f"scene_outline[{scene_index}].characters")
    for chain_index, chain in enumerate(script.dialogue_chains or []):
        for turn_index, turn in enumerate(chain.turns or []):
            speaker = str(turn.speaker or "").strip()
            line = str(turn.line or "").strip()
            add(
                speaker,
                f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker",
                line_context=line,
            )
            structured_turn_surfaces.add((
                _identity_carrier_annotation_base(speaker) or speaker,
                line,
            ))
    for item_index, item in enumerate(script.information_ledger or []):
        add(item.speaker_id, f"information_ledger[{item_index}].speaker_id")
    for voice_index, voice in enumerate(script.voice_bible or []):
        add(voice.speaker_id, f"voice_bible[{voice_index}].speaker_id")

    from app.validators import _script_dialogue_turns

    for scene_no, speaker, line in _script_dialogue_turns(
        script.full_script_text or "",
    ):
        if (speaker, str(line or "").strip()) in structured_turn_surfaces:
            continue
        add(
            speaker,
            f"full_script_text.scene[{scene_no}].speaker",
            line_context=line,
        )

    plan = script.narrative_plan
    if plan is not None:
        for contract_index, contract in enumerate(plan.identity_contracts or []):
            add(
                contract.identity_id,
                f"narrative_plan.identity_contracts[{contract_index}].identity_id",
            )
            add(
                contract.display_name,
                f"narrative_plan.identity_contracts[{contract_index}].display_name",
            )
            for voice_id in contract.voice_ids or []:
                add(
                    voice_id,
                    f"narrative_plan.identity_contracts[{contract_index}].voice_ids",
                )
        for state_index, state in enumerate(plan.character_states or []):
            add(
                state.character_id,
                f"narrative_plan.character_states[{state_index}].character_id",
            )
        for belief_index, belief in enumerate(plan.character_beliefs or []):
            add(
                belief.character_id,
                f"narrative_plan.character_beliefs[{belief_index}].character_id",
            )
        for scene_index, scene in enumerate(plan.scene_contracts or []):
            add(
                scene.point_of_view_character_id,
                f"narrative_plan.scene_contracts[{scene_index}].point_of_view_character_id",
            )

    return json.dumps(
        {"parse_status": "typed", "identity_mentions": mentions},
        ensure_ascii=False,
        separators=(",", ":"),
    )


_IDENTITY_CARRIER_ANNOTATION_RE = re.compile(
    r"^(?P<base>[^()（）]+?)\s*[（(][^()（）]+[）)]\s*$"
)


def _identity_carrier_annotation_base(value: object) -> str:
    match = _IDENTITY_CARRIER_ANNOTATION_RE.fullmatch(
        str(value or "").strip()
    )
    return match.group("base").strip() if match else ""


def _aligned_identity_source_label(
    source_label: str,
    identity_haystack: str,
) -> str:
    """Recover a provider-expanded label only when source alignment is strong."""
    label = str(source_label or "").strip()
    if not label:
        return ""
    if label in identity_haystack:
        return label
    condensed = textmatch.condense(label)
    if len(condensed) < 4:
        return ""
    aligned = align_source_excerpt(
        label,
        identity_haystack,
        min_match_chars=max(3, int(len(condensed) * 0.6)),
    )
    if aligned is None:
        return ""
    excerpt = str(aligned.excerpt or "").strip()
    excerpt_chars = len(textmatch.condense(excerpt))
    if (
        excerpt_chars < 3
        or excerpt_chars > len(condensed) + 4
        or textmatch.longest_run_ratio(label, excerpt) < 0.65
        or textmatch.bigram_coverage(label, excerpt) < 0.55
    ):
        return ""
    return excerpt


def _distributed_identity_fragments(
    text: str,
    label: str,
    *,
    known_names: list[str],
    window: int,
    budget: int,
) -> str:
    """Prefer identity windows containing known names, then span the timeline."""
    spans = [
        (max(0, match.start() - window), min(len(text), match.end() + window))
        for match in re.finditer(re.escape(label), text)
    ]
    if not spans or budget <= 0:
        return ""
    # Rank the individual occurrences before de-duplicating overlap.  Merging a
    # dense run first can create one multi-kilobyte span whose leading slice
    # hides the decisive late occurrence (for example, the moment a recurring
    # label finally states a name).
    ranked = sorted(
        enumerate(spans),
        key=lambda item: (
            -sum(
                name != label and name in text[item[1][0]:item[1][1]]
                for name in known_names
            ),
            0 if item[0] == 0 else 1,
            0 if item[0] == len(spans) - 1 else 1,
            item[0],
        ),
    )
    pieces: list[tuple[int, str]] = []
    selected_spans: list[tuple[int, int]] = []
    used = 0
    for index, (start, end) in ranked:
        if used >= budget:
            break
        if any(
            max(0, min(end, other_end) - max(start, other_start))
            >= int(min(end - start, other_end - other_start) * 0.6)
            for other_start, other_end in selected_spans
        ):
            continue
        piece = text[start:end].strip()[:budget - used]
        if piece:
            pieces.append((index, piece))
            selected_spans.append((start, end))
            used += len(piece)
    pieces.sort(key=lambda item: item[0])
    return "\n……\n".join(piece for _index, piece in pieces)


def _future_identity_context(
    future_text: str,
    source_labels: list[str],
    *,
    known_names: list[str] | None = None,
    current_text: str = "",
) -> str:
    """Return bounded future excerpts only where a current identity label occurs."""
    if not str(future_text or "").strip():
        return ""
    blocks: list[str] = []
    known = list(dict.fromkeys(
        str(name or "").strip()
        for name in (known_names or [])
        if str(name or "").strip()
    ))
    remaining = CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET
    needs_boundary_handoff = any(
        str(label or "").strip()
        and str(label or "").strip() not in future_text
        for label in source_labels
    )
    boundary = (
        "\n".join(filter(None, [
            str(current_text or "").strip()[-700:],
            str(future_text or "").strip()[:900],
        ]))
        if needs_boundary_handoff else ""
    )
    if boundary:
        block = f"【章节边界身份交接】\n{boundary[:remaining]}"
        blocks.append(block)
        remaining -= len(block)
    # Do not search and resend every known Bible name.  Candidate authorities
    # are projected separately; future prose is limited to unresolved labels
    # and the chapter-boundary handoff that can actually resolve them.
    for source_label in dict.fromkeys(source_labels):
        if remaining <= 0:
            break
        fragments = _distributed_identity_fragments(
            future_text,
            source_label,
            known_names=known,
            window=180,
            budget=min(900, remaining),
        )
        if not fragments:
            continue
        cooccurring = [
            name for name in known
            if name != source_label and name in fragments
        ]
        authority_hint = (
            "\n人物谱真名：" + "、".join(cooccurring)
            if cooccurring else ""
        )
        block = f"【当前称谓：{source_label}】\n{fragments}{authority_hint}"
        blocks.append(block)
        remaining -= len(block)
    return "\n\n".join(blocks)


def _source_identity_contexts(source_text: str, *, budget: int) -> list[str]:
    """Split the complete current source into bounded paragraph-preserving batches."""
    text = str(source_text or "").strip()
    if not text:
        return ["（本集原文为空）"]
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    for paragraph in paragraphs:
        if len(paragraph) > budget:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_chars = 0
            start = 0
            while start < len(paragraph):
                end = min(len(paragraph), start + budget)
                chunks.append(paragraph[start:end])
                start = end
            continue
        added = len(paragraph) + (1 if current else 0)
        if current and current_chars + added > budget:
            chunks.append("\n".join(current))
            current = [paragraph]
            current_chars = len(paragraph)
        else:
            current.append(paragraph)
            current_chars += added
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


def _current_identity_evidence_payload(record: dict) -> dict:
    """Canonical payload sealed into one backend-owned current evidence ID."""
    return {
        "receipt_version": CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION,
        "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
        "origin": str(record.get("origin") or ""),
        "source_hash": str(record.get("source_hash") or ""),
        "source_segment_id": str(record.get("source_segment_id") or ""),
        "start_offset": int(record.get("start_offset") or 0),
        "end_offset": int(record.get("end_offset") or 0),
        "path": str(record.get("path") or ""),
        "text": str(record.get("text") or ""),
    }


def _seal_current_identity_evidence(record: dict) -> dict:
    payload = _current_identity_evidence_payload(record)
    if (
        payload["origin"] not in {"current_source", "draft_identity_projection"}
        or not payload["source_hash"]
        or not payload["source_segment_id"]
        or not payload["text"].strip()
        or payload["end_offset"] <= payload["start_offset"]
    ):
        raise ValueError("current identity evidence receipt is incomplete")
    evidence_id = "CE:" + evidence_repository.content_hash(payload)[:24]
    return {**payload, "evidence_id": evidence_id}


def _current_identity_evidence_records(
    source_text: str,
    *,
    draft_text: str = "",
) -> list[dict]:
    """Build owned raw-source or typed-draft evidence, never prompt prose."""
    if str(draft_text or "").strip():
        try:
            projection = json.loads(_draft_identity_projection(draft_text))
        except (TypeError, ValueError, json.JSONDecodeError):
            projection = {}
        if projection.get("parse_status") != "typed":
            return []
        source_hash = evidence_repository.content_hash(draft_text)
        records: list[dict] = []
        for index, raw in enumerate(
            projection.get("identity_mentions") or [], start=1
        ):
            if not isinstance(raw, dict):
                continue
            value = str(raw.get("value") or "").strip()
            path = str(raw.get("path") or "").strip()
            context = str(raw.get("line_context") or "").strip()
            if not value or not path:
                continue
            text = value if not context else f"{value}\n{context}"
            records.append(_seal_current_identity_evidence({
                "origin": "draft_identity_projection",
                "source_hash": source_hash,
                "source_segment_id": f"DRF{index:04d}",
                # DRF offsets are stable positions in the typed projection,
                # not byte offsets into JSON serialization.
                "start_offset": index - 1,
                "end_offset": index,
                "path": path,
                "text": text,
            }))
        return records

    source_hash = evidence_repository.content_hash(source_text)
    return [
        _seal_current_identity_evidence({
            "origin": "current_source",
            "source_hash": source_hash,
            "source_segment_id": segment.segment_id,
            "start_offset": segment.start_offset,
            "end_offset": segment.end_offset,
            "path": "",
            "text": segment.text,
        })
        for segment in index_source_segments(source_text)
        if str(segment.text or "").strip()
    ]


def _current_identity_evidence_batches(
    source_text: str,
    *,
    draft_text: str = "",
) -> list[list[dict]]:
    """Pack owned evidence into bounded calls without resending raw source."""
    records = _current_identity_evidence_records(
        source_text,
        draft_text=draft_text,
    )
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for record in records:
        projected = {
            key: record[key]
            for key in (
                "evidence_id",
                "origin",
                "source_segment_id",
                "start_offset",
                "end_offset",
                "path",
                "text",
            )
        }
        record_chars = len(json.dumps(
            projected, ensure_ascii=False, separators=(",", ":")
        ))
        if current and current_chars + record_chars > CAST_DISCOVERY_SOURCE_BUDGET:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(record)
        current_chars += record_chars
    if current:
        batches.append(current)
    return batches


def _current_identity_evidence_catalog_hash(
    source_text: str,
    *,
    draft_text: str = "",
) -> str:
    return evidence_repository.content_hash({
        "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
        "batches": _current_identity_evidence_batches(
            source_text,
            draft_text=draft_text,
        ),
    })


def _current_identity_known_decision_catalog(
    evidence_by_ref: dict[str, dict],
    *,
    authorities: list[dict],
) -> dict[str, dict]:
    """Sign exact current-evidence/registered-authority label decisions."""
    decisions: dict[str, dict] = {}
    authority_by_ref_label: dict[tuple[str, str], str] = {}
    for evidence_ref, record in evidence_by_ref.items():
        evidence_text = str(record.get("text") or "")
        for authority in authorities:
            if str(authority.get("identity_kind") or "") == "functional":
                continue
            authority_id = str(authority.get("authority_id") or "").strip()
            canonical_name = str(
                authority.get("canonical_name") or ""
            ).strip()
            if not authority_id or not canonical_name:
                continue
            signed_identity_group = str(
                authority.get("identity_group") or authority_id
            ).strip()
            registered_labels = list(dict.fromkeys(
                str(label or "").strip()
                for label in (
                    canonical_name,
                    *(authority.get("source_labels") or []),
                )
                if str(label or "").strip()
            ))
            for source_label in registered_labels:
                if (
                    len(source_label) > IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH
                    or _identity_source_label_has_list_separator(source_label)
                    or source_label not in evidence_text
                ):
                    continue
                pair = (evidence_ref, source_label)
                previous_authority = authority_by_ref_label.setdefault(
                    pair, authority_id
                )
                if previous_authority != authority_id:
                    raise ContentGenerationError(
                        "current registered label 对应多个 authority："
                        f"{source_label}"
                    )
                payload = {
                    "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
                    "decision_type": "registered_authority",
                    "evidence_ref": evidence_ref,
                    "evidence_id": str(record.get("evidence_id") or ""),
                    "authority_id": authority_id,
                    "canonical_name": canonical_name,
                    "source_label": source_label,
                    "materialization_compatible": bool(
                        authority_id
                        == _canonical_named_authority_id(canonical_name)
                        and signed_identity_group == authority_id
                    ),
                }
                decision_id = (
                    f"K:{evidence_ref}:"
                    + evidence_repository.content_hash(payload)[:24]
                )
                decisions[decision_id] = {
                    **payload,
                    "decision_id": decision_id,
                    "identity_group": str(
                        signed_identity_group
                    ),
                    "source_instance_key": str(
                        authority.get("source_instance_key") or authority_id
                    ).strip(),
                }
    return decisions


def _current_identity_prior_decision_catalog(
    evidence_by_ref: dict[str, dict],
    *,
    prior_candidates: list[dict],
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Sign explicit cross-batch reuse choices without label auto-merging."""
    prior_named: dict[str, dict] = {}
    functional_groups: dict[str, dict] = {}
    for candidate in prior_candidates:
        if (
            candidate.get("source_label_provenance")
            == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        ):
            continue
        source_label = str(candidate.get("source_label") or "").strip()
        identity_group = str(candidate.get("identity_group") or "").strip()
        identity_kind = str(candidate.get("identity_kind") or "").strip()
        if not source_label or not identity_group:
            continue
        if identity_kind == "named":
            canonical_name = str(candidate.get("name") or "").strip()
            # Registered authorities are re-signed against each batch by the
            # normal K catalog.  P:N is only for a request-local new literal
            # name that has no authority yet; exposing both would make two
            # different tokens claim the same registered decision.
            if (
                not canonical_name
                or str(candidate.get("authority_id") or "").strip()
                or canonical_name != source_label
            ):
                continue
            for evidence_ref, record in evidence_by_ref.items():
                if source_label not in str(record.get("text") or ""):
                    continue
                payload = {
                    "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
                    "decision_type": "prior_named",
                    "evidence_ref": evidence_ref,
                    "evidence_id": str(record.get("evidence_id") or ""),
                    "source_label": source_label,
                    "canonical_name": canonical_name,
                    "identity_group": identity_group,
                    "authority_id": str(
                        candidate.get("authority_id") or ""
                    ).strip(),
                    "known_authority": bool(
                        str(candidate.get("authority_id") or "").strip()
                    ),
                    "materialization_compatible": bool(
                        candidate.get("_current_materialization_compatible")
                    ),
                }
                decision_id = (
                    f"P:N:{evidence_ref}:"
                    + evidence_repository.content_hash(payload)[:20]
                )
                prior_named[decision_id] = {
                    **payload,
                    "decision_id": decision_id,
                }
        elif identity_kind == "functional":
            existing = next((
                item for item in functional_groups.values()
                if item["identity_group"] == identity_group
            ), None)
            if existing is not None:
                if source_label not in existing["source_labels"]:
                    existing["source_labels"].append(source_label)
                response_group_key = str(
                    candidate.get("_current_response_group_key") or ""
                ).strip()
                if (
                    response_group_key
                    and response_group_key
                    not in existing["response_group_keys"]
                ):
                    existing["response_group_keys"].append(response_group_key)
                continue
            payload = {
                "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
                "decision_type": "prior_functional_group",
                "identity_group": identity_group,
                "existing_route_name": str(
                    candidate.get("existing_route_name") or ""
                ).strip(),
            }
            decision_id = (
                "P:F:" + evidence_repository.content_hash(payload)[:24]
            )
            functional_groups[decision_id] = {
                **payload,
                "decision_id": decision_id,
                "source_labels": [source_label],
                "response_group_keys": [
                    value for value in [str(
                        candidate.get("_current_response_group_key") or ""
                    ).strip()] if value
                ],
            }
    return prior_named, functional_groups


def _current_identity_evidence_receipt_is_valid(
    value: object,
    *,
    source_text: str = "",
    draft_text: str = "",
) -> bool:
    """Verify the backend seal and, when available, its owned input epoch."""
    if not isinstance(value, dict):
        return False
    if (
        value.get("receipt_version")
        != CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION
        or value.get("contract_version")
        != CURRENT_IDENTITY_DECISION_VERSION
    ):
        return False
    try:
        payload = _current_identity_evidence_payload(value)
    except (TypeError, ValueError):
        return False
    if str(value.get("evidence_id") or "") != (
        "CE:" + evidence_repository.content_hash(payload)[:24]
    ):
        return False
    origin = payload["origin"]
    if origin == "current_source":
        if payload["source_hash"] != evidence_repository.content_hash(source_text):
            return False
        start = payload["start_offset"]
        end = payload["end_offset"]
        segments_by_id = {
            segment.segment_id: segment
            for segment in index_source_segments(source_text)
        }
        owned_segment = segments_by_id.get(payload["source_segment_id"])
        return bool(
            0 <= start < end <= len(source_text)
            and source_text[start:end].strip() == payload["text"]
            and owned_segment is not None
            and owned_segment.start_offset == start
            and owned_segment.end_offset == end
            and owned_segment.text == payload["text"]
        )
    if origin == "draft_identity_projection":
        if draft_text and payload["source_hash"] != evidence_repository.content_hash(
            draft_text
        ):
            return False
        if not payload["path"] or not payload["text"].strip():
            return False
        if not draft_text:
            # The seal was already checked against the owned draft at the
            # current-discovery boundary.  Later stages only need to preserve
            # that immutable backend receipt.
            return True
        return any(
            str(record.get("evidence_id") or "")
            == str(value.get("evidence_id") or "")
            and record == value
            for record in _current_identity_evidence_records(
                source_text,
                draft_text=draft_text,
            )
        )
    return False


def _current_identity_semantic_signature(item: dict) -> tuple[str, ...]:
    """Return the identity fields which must agree across owned occurrences."""
    return (
        str(item.get("source_label") or "").strip(),
        str(item.get("identity_kind") or "").strip(),
        str(item.get("name") or "").strip(),
        str(item.get("identity_group") or "").strip(),
        str(item.get("authority_id") or "").strip(),
        str(item.get("existing_route_name") or "").strip(),
        str(item.get("source_label_provenance") or "").strip(),
        "1" if item.get("materialization_compatible") else "0",
        str(item.get("_current_response_group_key") or "").strip(),
    )


def _current_identity_durable_signature(item: dict) -> tuple[str, ...]:
    """Stable identity signature after request-local F/P tokens are resolved."""
    return _current_identity_semantic_signature(item)[:-1]


def _current_identity_declared_signature(item: dict) -> tuple[str, ...]:
    """模型自己申报的内容签名（第27轮真实回归 ERR-20260824-079190，见
    _project_current_identity_response 的 by_label 合并循环上方完整
    说明）：排除 identity_group/source_label_provenance 这两个后端为
    "这次具体出现"单独推导、会随手气浮动的字段（identity_group 在
    synthetic 分支下按 evidence_id 生成哈希；source_label_provenance
    取决于这次引用是否恰好落在"全批唯一逐字锚点"自动改写的判定窗口
    内），只保留模型真正申报的身份判断本身：identity_kind、canonical_
    name（named 分支）、authority_id、existing_route_name、
    functional_identity_key（f 分支，即 _current_response_group_key）、
    kind（onscreen/mentioned）。跟 _current_identity_durable_signature
    是两把不同的尺子：durable signature 问"这两次出现在后端眼里是不是
    完全一样"，declared signature 问"模型自己申报的内容是不是完全一样"
    ——同一 source_label 下两次出现 durable signature 不一致，不代表
    模型自相矛盾，可能只是后端对其中一次的证据判定恰好没找到全批唯一
    逐字锚点。"""
    return (
        str(item.get("identity_kind") or "").strip(),
        str(item.get("name") or "").strip(),
        str(item.get("authority_id") or "").strip(),
        str(item.get("existing_route_name") or "").strip(),
        str(item.get("_current_response_group_key") or "").strip(),
        str(item.get("kind") or "").strip(),
    )


def _current_identity_receipt_sort_key(value: dict) -> tuple[object, ...]:
    return (
        str(value.get("origin") or ""),
        str(value.get("source_hash") or ""),
        int(value.get("start_offset") or 0),
        int(value.get("end_offset") or 0),
        str(value.get("evidence_id") or ""),
    )


def _merge_current_identity_occurrences(options: list[dict]) -> dict:
    """Merge one exact semantic identity while retaining every typed receipt."""
    if not options:
        raise ValueError("current identity occurrence merge requires candidates")
    ordered = sorted(
        options,
        key=lambda item: _current_identity_receipt_sort_key(
            item.get("source_evidence_receipt") or {}
        ),
    )
    strongest = next(
        (item for item in ordered if item.get("kind") == "onscreen"),
        ordered[0],
    )
    receipt_by_id: dict[str, dict] = {}
    for item in ordered:
        raw_receipts = item.get("source_evidence_receipts")
        receipts = (
            raw_receipts
            if isinstance(raw_receipts, list)
            else [item.get("source_evidence_receipt")]
        )
        for raw in receipts:
            if not isinstance(raw, dict):
                continue
            evidence_id = str(raw.get("evidence_id") or "").strip()
            if evidence_id:
                receipt_by_id.setdefault(evidence_id, dict(raw))
    receipts = sorted(receipt_by_id.values(), key=_current_identity_receipt_sort_key)
    # The singular receipt is compatibility-only in RF11.  Keep it derivable
    # from the durable v2 list: the canonical first receipt is the primary,
    # while the candidate kind is still the strongest occurrence across all
    # receipts.  We intentionally do not encode an unverifiable per-receipt
    # kind inside the backend evidence seal.
    primary_receipt = receipts[0] if receipts else None
    source_segment_ids = list(dict.fromkeys(
        str(receipt.get("source_segment_id") or "").strip()
        for receipt in receipts
        if str(receipt.get("source_segment_id") or "").strip()
    ))
    merged = {
        **strongest,
        "source_evidence_receipts": receipts,
        "source_segment_ids": source_segment_ids,
    }
    if primary_receipt is not None:
        primary_text = str(primary_receipt.get("text") or "")
        source_label = str(merged.get("source_label") or "").strip()
        primary_evidence = _bounded_owned_identity_evidence(
            primary_text,
            anchors=[source_label] if source_label in primary_text else [],
            max_chars=80,
        ) or primary_text.strip()[:80]
        merged.update({
            "source_evidence_receipt": dict(primary_receipt),
            "source_segment_id": str(
                primary_receipt.get("source_segment_id") or ""
            ),
            "source_quote": primary_text,
            "evidence": primary_evidence,
        })
    return merged


def _current_identity_disambiguation_key(item: dict) -> str:
    """模型是否已经用结构性字段区分了"这是哪个人"的键（第31轮 EP5
    ERR-20260824-614276）：功能身份（f 分支）看 functional_identity_key
    （即 _current_response_group_key，append_candidate 只在
    identity_kind=="functional" 时才填这个字段——prompt 规则6"同一
    functional_identity_key=同一人"，模型自己申报的分组信号）；已登记
    具名身份（k 分支）append_candidate 时 _current_response_group_key
    恒为空串（见该调用点），改看 authority_id——k 分支的 decision_id 精确
    复用同一个已登记决议才会解析出同一个 authority_id，这是同一层级的
    "模型精确复用同一个 ID = 申报同一个人"信号，decision_id 本身在
    _project_current_identity_response 里已经被消费掉、没有原样保留到
    这一层，authority_id 是它解析后的等价代理。两者都拿不到时返回空串
    ——空串一律各自成组（没有任何区分信号，不能假装它们是被区分开的）。
    """
    group_key = str(item.get("_current_response_group_key") or "").strip()
    if group_key:
        return group_key
    authority_id = str(item.get("authority_id") or "").strip()
    if authority_id:
        return authority_id
    return f"__no_key__:{id(item)}"


def _current_identity_reconcile_as_single(options: list[dict]) -> dict | None:
    """尝试把一组"同一 (source_label, scope_qualifier) 复合键"下的候选
    归一成一条身份，复用既有①②判据（见 _project_current_identity_response
    的 by_label 合并循环）：durable signature 全等 -> 直接合并；申报字段
    签名全等 + 至少一条逐字锚定 -> 归一（标记 _current_identity_
    normalized_duplicate）。两条都不满足则返回 None（调用方决定是当作
    "不同人只是缺限定语"继续按子组拆分，还是真矛盾致命）——本函数纯判定，
    不写 errors，不认识"是第几层调用"。"""
    signatures = {_current_identity_durable_signature(item) for item in options}
    synthetic_repeat = len(options) > 1 and any(
        item.get("source_label_provenance") == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        for item in options
    )
    if len(signatures) == 1 and not synthetic_repeat:
        return _merge_current_identity_occurrences(options)
    declared_signatures = {
        _current_identity_declared_signature(item) for item in options
    }
    has_literal_anchor = any(
        item.get("source_label_provenance") == CURRENT_IDENTITY_LITERAL_PROVENANCE
        for item in options
    )
    if len(declared_signatures) == 1 and has_literal_anchor:
        normalized = _merge_current_identity_occurrences(options)
        normalized["_current_identity_normalized_duplicate"] = True
        return normalized
    return None


# The one field a K decision may echo without adding anything: its token
# already binds the evidence, so ``evidence_ref`` alongside it restates a
# receipt the backend owns outright (production EP4: ``k.0.evidence_ref Extra
# inputs are not permitted`` failed a whole episode over that echo).  Naming it
# is the point -- "drop whatever the K schema does not declare" reads like the
# same rule but is a different, much wider one, and it deletes model-authored
# content too.  Real incident run_690cebdd45a7 (ep_bf9051d167a7 EP1): the model
# nested the whole K/N/F wire one level too deep, writing ``f``/``k``/``n``
# inside ``k[0]``; the wider rule swept all three away, leaving ``{"decision_id":
# ...}``, and the failure surfaced as "k.0.kind Field required" -- a missing
# field, with no trace of the misplaced N branch that was the actual fault.
_CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS = frozenset({"evidence_ref"})


def _normalize_current_identity_payload(payload: dict) -> dict:
    """Drop fields the K/N/F wire declares redundant before strict validation.

    Only keys the model has no authority over are removed (see
    ``_CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS``); anything else it wrote stays
    and still fails closed, on K exactly as on N/F, because those bytes are
    model-authored content the backend cannot second-guess -- and because a
    strict-schema rejection that names what the model actually sent is the
    only thing that makes the next failure diagnosable.
    """
    if not isinstance(payload, dict):
        return payload
    known = payload.get("k")
    if not isinstance(known, list):
        return payload
    cleaned: list[dict] = []
    changed = False
    for item in known:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        trimmed = {
            key: value for key, value in item.items()
            if key not in _CURRENT_KNOWN_BACKEND_OWNED_ECHO_KEYS
        }
        changed = changed or trimmed != item
        cleaned.append(trimmed)
    if not changed:
        return payload
    return {**payload, "k": cleaned}


def _resolved_evidence_ref(raw: str, expected_refs: set[str]) -> str:
    """Repair a zero-padding slip in an otherwise in-catalog evidence ref.

    The schema pins ``evidence_ref`` to a closed enum, but the provider does
    not always honour strict mode: production delivered ``E01`` for a catalog
    holding ``E001``.  Re-padding is pure formatting -- it selects an existing
    backend-owned receipt and only when exactly one matches, so it can never
    move a decision onto a different span.  Anything ambiguous is returned
    unchanged and still fails closed.
    """
    ref = str(raw or "").strip()
    if ref in expected_refs:
        return ref
    match = re.fullmatch(r"([A-Za-z]+)([0-9]+)", ref)
    if match is None:
        return ref
    prefix, digits = match.group(1), match.group(2).lstrip("0") or "0"
    candidates = [
        candidate for candidate in expected_refs
        if candidate.startswith(prefix)
        and (candidate[len(prefix):].lstrip("0") or "0") == digits
    ]
    return candidates[0] if len(candidates) == 1 else ref


def _identity_form_functional_key(identity_label: str) -> str:
    """Stable per-label group key for an appellation demoted out of N."""
    return "NF" + evidence_repository.content_hash(
        str(identity_label or "").strip()
    )[:12]


_IDENTITY_DISAMBIGUATING_ORDINALS = "甲乙丙丁戊己庚辛壬癸"


def _identity_disambiguating_suffix(collision_index: int) -> str:
    """真实第20轮 EP4 回归 ERR-20260824-407c9b：两个不同的 identity_group
    退回同一个裸功能性标签（"外宗弟子"）当 route_name 时的确定性区分后缀。
    1-based collision_index -> 甲/乙/丙...；超出十天干（第11次及以后撞车，
    极端情况）退化为阿拉伯数字，保证任意数量的碰撞都有确定性且互不相同的
    后缀，不会自己再撞车。"""
    if 1 <= collision_index <= len(_IDENTITY_DISAMBIGUATING_ORDINALS):
        return _IDENTITY_DISAMBIGUATING_ORDINALS[collision_index - 1]
    return str(collision_index)


# k/n/f 计数帽的下限：与批规模脱钩前的历史基线（见 _current_identity_decision_cap
# 的完整推导），任何批次都不会比这更严。
_CURRENT_IDENTITY_DECISION_CAP_FLOOR = 64
# 每个 evidence ref 允许的决策密度倍数：见 _current_identity_decision_cap 的
# 完整推导与真实校准数据。
_CURRENT_IDENTITY_DECISION_CAP_PER_REF = 3


def _current_identity_decision_cap(evidence_ref_count: int) -> int:
    """k/n/f 单分支计数帽（第22轮总审计 ERR-20260824-aeee2d 修复，参数重推导）。

    考古：这道帽子最早（commit 5accd39/a2aeab2）是 RF10 "每个 evidence ref
    自带一份 k/n/f、各自封顶 64" 形状下的产物——分母是"一个 evidence ref"
    （≤900 字的一个自然段），64 对一个短段落里能出现的人数已经非常宽松。
    紧接着的下一次改动把响应形状压平成"整批只输出一次全局 k/n/f"（RF11，
    _current_identity_schema 的 docstring："RF10 要求每个 evidence span 都配一
    份 K/N/F……RF11 只在三个全局数组里输出被选中的身份"），分母从"一个
    evidence ref"变成了"一整批 evidence ref"，但那个 64 的数字被原样照抄过来，
    从未重新推导——批规模一旦变大，同一个常量就变得越来越紧，跟它原本要防的
    "单段文本里的失控值"已经不是同一件事。

    真实回归 ERR-20260824-aeee2d（第 22 轮 EP3，provider_calls.id=8964）：该批
    对话密集，共 84 个 evidence ref（许多角色反复对话，每次出场都是独立的
    evidence ref），模型如实为已登记角色选了 78 条合法 k 决议（78/84≈0.93
    条/ref），全部指向 known_decisions 目录里真实存在的条目——不是幻觉或
    失控，只是这一集对话轮次天然多。旧的固定 64 硬把这批合法输出当成失控拒绝。

    公式：max(_CURRENT_IDENTITY_DECISION_CAP_FLOOR, 批次 evidence ref 数量 *
    _CURRENT_IDENTITY_DECISION_CAP_PER_REF)。
    - 下限 64：保留历史基线，≤21 个 evidence ref 的批次（绝大多数集数）行为
      与改动前完全一致，不放宽任何既有防护。
    - 倍数 3：以 ERR-20260824-aeee2d 的真实密度（≈1 条/ref）为校准点，
      3 倍留出多人同段出场的余量，同时仍是一个会拒绝真正失控值（比如模型
      对同一批复读上千条重复决议）的真实上限，不是形同虚设的"数据字段"。
    该帽子只做资源/失控防线：每一条 k/n/f 决议是否真的锚定到后端自己的证据
    目录，由 _project_current_identity_response 里逐条的"越界"校验独立把关
    （k：decision_id 必须命中 known_decisions；n/f：evidence_ref 必须落在
    expected_refs 内），计数帽拿掉也不会削弱那道锚定护栏。
    """
    return max(
        _CURRENT_IDENTITY_DECISION_CAP_FLOOR,
        int(evidence_ref_count) * _CURRENT_IDENTITY_DECISION_CAP_PER_REF,
    )


def _visual_entity_id_for_resolution_safe(value: dict[str, Any]) -> str | None:
    """延迟导入 ``app.identity_authority.visual_entity_id_for_resolution``。

    该函数按 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.2 冻结的签名由另一
    条并行改动落地在 app.identity_authority——本文件不实现、只消费，调用点
    延迟到函数体内部（而非模块顶层 import），避免在依赖尚未落地的窗口期让
    整个 app.portraits 模块导入失败。依赖缺失或调用异常时返回 None：命名侧
    折叠（K 决议本身）不依赖这个返回值，只是暂时跳过 visual 侧记账，等依赖
    落地后自动补齐，不需要再改这里。"""
    try:
        from app.identity_authority import visual_entity_id_for_resolution
    except ImportError:
        return None
    try:
        result = visual_entity_id_for_resolution(value)
    except Exception:  # noqa: BLE001 - 防御性：绝不让记账失败拖垮身份预检
        return None
    return str(result).strip() or None


class _CurrentIdentitySchemaViolation(str):
    """A projection error whose violated constraint is declared in the wire
    JSON schema (``_current_identity_schema()``'s ``enum``/``required``/
    ``additionalProperties`` -- the keywords that actually survive
    ``_identity_strict_provider_schema``'s whitelist and reach the provider;
    ``maxItems``/``minLength``/``maxLength`` do not and so never qualify).

    RCA ERR-20260824-e3628f: a supplier occasionally violates its own
    strict-mode ``enum`` contract at low frequency (~0.5%) on deep array
    elements. That is a format defect of the sampled answer, not a business
    disagreement -- resampling the whole episode is safe. Behaves exactly
    like ``str`` everywhere (equality, ``in``, ``"；".join(...)``, f-strings);
    the one addition is letting the two callers below classify it via
    ``isinstance`` instead of pattern-matching message text (判据必须是结构性
    的，不是错误文案名单 -- only the handful of append_error(...,
    schema_violation=True) call sites below ever construct one; every other
    call site keeps appending a plain ``str`` and stays in the semantic/
    fail-closed family).
    """

    __slots__ = ()


def _current_identity_is_schema_violation(error: str) -> bool:
    return isinstance(error, _CurrentIdentitySchemaViolation)


def _project_current_identity_response(
    value: CurrentIdentityCandidateResponse,
    *,
    evidence_by_ref: dict[str, dict],
    known_decisions: dict[str, dict],
    prior_functional_groups: dict[str, dict] | None = None,
    reserved_authority_labels: set[str] | None = None,
    group_scope: str,
    existing_functional_routes: set[str],
    existing_functional_route_labels: dict[str, str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Resolve the RF10 K/N/F wire through backend-owned evidence receipts.

    Returned ``errors`` stays a flat ``list[str]`` for backward compatibility
    (every existing caller/test does ``"..." in errors`` or ``"；".join(errors)``);
    entries that violate a wire-schema-declared constraint (see
    ``_CurrentIdentitySchemaViolation``) are instances of that ``str``
    subclass instead of plain ``str`` so callers can tell them apart with
    ``_current_identity_is_schema_violation`` without touching message text.
    """
    errors: list[str] = []
    projected: list[dict] = []
    expected_refs = set(evidence_by_ref)
    if set(value.model_fields_set) != {"k", "n", "f"}:
        errors.append("current identity root keys 非闭合")
    # 第22轮总审计 ERR-20260824-aeee2d：帽子随本批 evidence ref 数量缩放，
    # 见 _current_identity_decision_cap 的完整推导。
    decision_cap = _current_identity_decision_cap(len(expected_refs))
    for branch, items in (("k", value.k), ("n", value.n), ("f", value.f)):
        if len(items) > decision_cap:
            errors.append(f"current identity {branch} decisions 过多")

    # rule 6 makes functional_identity_key the model's own explicit "this is
    # the same person" signal: two F entries that repeat both the identical
    # source_label *and* the identical functional_identity_key are the model
    # asserting one entity, not two.  That declared-repeat shape is narrower
    # than "any non-literal functional citation" -- a single, unrepeated F
    # entry whose cited E happens not to contain its label stays exactly the
    # legitimate synthetic observation prompt rule 4 describes (never
    # auto-rebound; see test_current_identity_literal_label_isolated_as_synthetic_once).
    functional_repeat_pairs: dict[tuple[str, str], int] = {}
    for item in value.f:
        pair = (
            str(item.source_label or "").strip(),
            str(item.functional_identity_key or "").strip(),
        )
        functional_repeat_pairs[pair] = functional_repeat_pairs.get(pair, 0) + 1
    declared_repeat_labels = {
        label for (label, key), count in functional_repeat_pairs.items()
        if count > 1 and label and key
    }

    # 反过来，同一个 source_label 被分给**不同**的 functional_identity_key，是
    # 模型在说「本集有好几个人都这么称呼」。这样的称谓在本集就不指向唯一身份，
    # 下面的「冒用已登记身份」判据对它不成立——那条判据默认了「称谓字面相同即
    # 身份相同」，这对真名成立，对外貌类描述不成立。
    #
    # 生产 EP1：人物谱里存着一张主名为「绿袍男子」的卡（描述性称呼建卡，本身
    # 就是上游的问题），而第 1 章原文写的是「两个穿着绿色长袍的男子」。模型判
    # 得完全正确——两条 functional，F4/F5，scope_qualifier 分别是「两个绿袍男子
    # 之一/之二」——却被按「冒用」硬失败，整集映射包卡死且重试必然再失败。
    #
    # 判据取自本次输入里模型自己的产出，不含任何词表：一个称谓是不是通称，由
    # 它在这批证据里指向几个个体决定。王有材那类真正的降级误判仍然被拦：那种
    # 情形下模型只会报一条，label 不会跨 key 复用。
    functional_keys_by_label: dict[str, set[str]] = {}
    for item in value.f:
        label = str(item.source_label or "").strip()
        key = str(item.functional_identity_key or "").strip()
        if label and key:
            functional_keys_by_label.setdefault(label, set()).add(key)
    labels_shared_across_individuals = {
        label for label, keys in functional_keys_by_label.items() if len(keys) > 1
    }

    # 同批折叠通道（absorbed_functional_keys，见设计文档 §4.2 "同批折叠
    # 通道"）需要反查每个可吸收 token 背后的 (source_label, scope_qualifier)，
    # 用于纯函数式计算该 functional 组当时会被分配到的 visual_entity_id。
    # 这里只建一份 token -> pairs 索引，不改变下面 f 循环本身的既有行为。
    batch_functional_label_sources: dict[str, list[tuple[str, str]]] = {}
    for item in value.f:
        key = str(item.functional_identity_key or "").strip()
        if not key:
            continue
        pair = (
            str(item.source_label or "").strip(),
            str(item.scope_qualifier or "").strip(),
        )
        batch_functional_label_sources.setdefault(key, []).append(pair)
    absorbable_functional_tokens = (
        set(batch_functional_label_sources)
        | set(prior_functional_groups or {})
        | set(existing_functional_routes or set())
    )

    def append_candidate(
        *,
        source_label: str,
        canonical_name: str,
        identity_kind: str,
        functional_key: str,
        kind: str,
        record: dict,
        authority_id: str = "",
        authority_group: str = "",
        known_authority: bool = False,
        materialization_compatible: bool = False,
        fixed_identity_group: str = "",
        scope_qualifier: str = "",
    ) -> None:
        source_label = str(source_label or "")
        canonical_name = str(canonical_name or "")
        functional_key = str(functional_key or "")
        scope_qualifier = str(scope_qualifier or "").strip()
        if source_label != source_label.strip():
            errors.append(f"source_label 含首尾空白：{source_label!r}")
        if (
            not source_label
            or len(source_label) > IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH
            or _identity_source_label_has_list_separator(source_label)
        ):
            errors.append(f"source_label 非法：{source_label!r}")
        evidence_text = str(record.get("text") or "")
        literal = bool(source_label and source_label in evidence_text)
        eligible_for_rebind = identity_kind == "named" or (
            identity_kind == "functional" and source_label in declared_repeat_labels
        )
        if not literal and eligible_for_rebind and source_label:
            # 模型只是在一批 backend-owned 证据里挑下标，证据本身始终由后端拥有。
            # 一个逐字称谓如果确实逐字出现在本批另一条证据里，那是选错了 E，不是
            # 凭空捏造：直接改绑到真正承载它的那条证据，而不是让整集预检硬失败
            # （否则模型每挑错一次下标，整集剧本就必须人工重试一次）。
            # named 一直如此。functional 只在模型自己用同一 source_label +
            # 同一 functional_identity_key 重复声明「这是同一个人」时才享有同样
            # 的改绑（rule 6：不同 source_label 若明确是同一人必须共用同一 ID —
            # 同一 source_label 重复同一 ID 是更强的同一性声明）。单次、未重复的
            # 非逐字 functional 引用（如「门卫」被错误但仅一次地绑到无关证据）
            # 仍然是 prompt rule 4 允许的合法 synthetic 观察，不做改绑
            # （见 test_current_identity_literal_label_isolated_as_synthetic_once /
            # cross_f gate）。
            # 生产 EP5：两条同 key「男子」都被错误绑到了不含该词的段落，唯一真正
            # 逐字出现「男子」的段落反而没有被引用，导致本应合并的一个人被按证据
            # 分别隔离出不同 identity_group，触发 source_label 重复硬失败。
            # 只在全批唯一匹配时才自动改绑；命中多条视为歧义，不得静默挑一个可能
            # 错的目标——这种情况维持原判（named 硬失败，functional 隔离为
            # synthetic）。
            literal_matches = [
                owned
                for owned in evidence_by_ref.values()
                if source_label in str(owned.get("text") or "")
            ]
            if len(literal_matches) == 1:
                record = literal_matches[0]
                evidence_text = str(record.get("text") or "")
                literal = True
        if canonical_name != canonical_name.strip():
            errors.append(f"canonical_name 含首尾空白：{source_label}")
        if identity_kind == "named":
            if not known_authority and canonical_name != source_label:
                errors.append(
                    "current named 只允许逐字自称谓，别名必须留待 typed authority："
                    f"{source_label}->{canonical_name}"
                )
            if (
                not known_authority
                and source_label in (reserved_authority_labels or set())
            ):
                errors.append(
                    "current 已登记身份必须选择 K decision："
                    f"{source_label}"
                )
            if not literal:
                errors.append(
                    f"current named 缺少逐字 owned evidence：{source_label}"
                )
            if (
                known_authority
                and kind == "onscreen"
                and not materialization_compatible
            ):
                errors.append(
                    "current K authority 不可直接物化人物卡："
                    f"{source_label}->{canonical_name}"
                )
        if functional_key != functional_key.strip():
            errors.append(
                f"functional_identity_key 含首尾空白：{source_label}"
            )
        if identity_kind == "functional" and not functional_key:
            errors.append(f"functional_identity_key 为空：{source_label}")
        if (
            identity_kind == "functional"
            and source_label in (reserved_authority_labels or set())
            and source_label not in labels_shared_across_individuals
        ):
            errors.append(
                "current functional 不得冒用已登记身份称谓："
                f"{source_label}"
            )

        prior_functional_group = (
            (prior_functional_groups or {}).get(functional_key)
            if identity_kind == "functional"
            else None
        )
        if (
            identity_kind == "functional"
            and functional_key.startswith("P:")
            and prior_functional_group is None
        ):
            errors.append(
                f"current prior functional decision 越界：{functional_key}"
            )
        prior_groups_for_label = [
            group
            for group in (prior_functional_groups or {}).values()
            if source_label in set(group.get("source_labels") or [])
        ]
        if (
            identity_kind == "functional"
            and prior_functional_group is None
            and prior_groups_for_label
        ):
            errors.append(
                "current 后续batch的同称谓必须用P token显式复用 prior group："
                f"{source_label}"
            )
        if prior_functional_group is not None and not literal:
            errors.append(
                "current synthetic functional 不得复用 prior group："
                f"{source_label}"
            )
        existing_route_name = (
            str(prior_functional_group.get("existing_route_name") or "")
            if prior_functional_group is not None
            else (
                functional_key
                if identity_kind == "functional"
                and literal
                and functional_key in existing_functional_routes
                else ""
            )
        )
        if identity_kind == "named" and fixed_identity_group:
            identity_group = fixed_identity_group
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        elif identity_kind == "named" and authority_id:
            identity_group = authority_group or authority_id
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        elif identity_kind == "named":
            identity_group = (
                f"{group_scope}:named:"
                + evidence_repository.content_hash(source_label)[:16]
            )
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        elif not literal:
            identity_group = (
                f"{group_scope}:synthetic:"
                + evidence_repository.content_hash({
                    "source_label": source_label,
                    "evidence_id": str(record.get("evidence_id") or ""),
                })[:16]
            )
            provenance = CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        else:
            identity_group = (
                str(prior_functional_group.get("identity_group") or "")
                if prior_functional_group is not None
                else (
                    f"existing:{existing_route_name}"
                    if existing_route_name
                    else f"{group_scope}:{functional_key}"
                )
            )
            provenance = CURRENT_IDENTITY_LITERAL_PROVENANCE
        evidence = _bounded_owned_identity_evidence(
            evidence_text,
            anchors=[source_label] if literal else [],
            max_chars=80,
        )
        if not evidence:
            evidence = evidence_text.strip()[:80]
        projected.append({
            "name": canonical_name or source_label,
            "source_label": source_label,
            "identity_kind": identity_kind,
            "identity_group": identity_group,
            "authority_id": authority_id,
            "existing_route_name": existing_route_name,
            "kind": (
                "mentioned" if kind == "mentioned" else "onscreen"
            ),
            "evidence": evidence,
            "future_evidence": "",
            "source_segment_id": str(record.get("source_segment_id") or ""),
            "source_quote": evidence_text,
            "source_label_provenance": provenance,
            "source_evidence_receipt": dict(record),
            "source_evidence_receipts": [dict(record)],
            "source_segment_ids": [str(record.get("source_segment_id") or "")],
            "_current_materialization_compatible": bool(
                materialization_compatible or not authority_id
            ),
            "materialization_compatible": bool(
                materialization_compatible or not authority_id
            ),
            "_current_response_group_key": (
                functional_key if identity_kind == "functional" else ""
            ),
            "scope_qualifier": scope_qualifier,
            "_typed_source_evidence_owned": True,
        })

    # 第35轮真实回归 ERR-20260824-bc3d14（EP10，李富贵）：模型在同一次响应里
    # 既在 k 里为 (source_label, evidence_ref) 正确签发了合规决议，又在 n 里
    # 重复申报同一 (identity_label, evidence_ref) 作为「新」具名声明——这是
    # 冗余回显，不是未经核验的具名注入。记录本响应内每条合规 K 决议实际锚定
    # 的 (source_label, evidence_ref) 复合键，供下面 n 循环判断是否为冗余
    # 回显；键里同时要求 label 与 evidence_ref 都一致（第35轮用例 C：同
    # label 不同 ref 不算——那种情况下 k 决议并未覆盖 n 这条具体声明所引用
    # 的证据，仍然维持硬失败，见下方 append_candidate 里 known_authority
    # 闸门）。
    redundant_n_echo_k_pairs: dict[tuple[str, str], dict] = {}
    for item in value.k:
        decision_id = str(item.decision_id or "")
        selected = known_decisions.get(decision_id)
        evidence_ref = str((selected or {}).get("evidence_ref") or "")
        record = evidence_by_ref.get(evidence_ref)
        if selected is None or record is None:
            # decision_id is enum-declared in _current_identity_schema()
            # (known_item["properties"]["decision_id"]["enum"] = decision_ids)
            # and that enum keyword survives _identity_strict_provider_schema's
            # whitelist, so it really is sent to the provider -- a decision_id
            # outside it is a wire-schema violation (selected is None).
            # `record is None` can only fire when `selected` is not None, but
            # both _current_identity_known_decision_catalog and
            # _current_identity_prior_decision_catalog only ever mint a
            # known_decisions entry by iterating evidence_by_ref.items()
            # itself, so every entry's evidence_ref is structurally guaranteed
            # to already be a key of evidence_by_ref -- that branch is dead
            # defensive code, not a second reachable failure mode, so the
            # whole check is schema-declared in practice.
            errors.append(
                _CurrentIdentitySchemaViolation(
                    f"current K decision 越界：{decision_id}"
                )
            )
            continue
        append_candidate(
            source_label=str(selected.get("source_label") or ""),
            canonical_name=str(selected.get("canonical_name") or ""),
            identity_kind="named",
            functional_key="",
            kind=item.kind,
            record=record,
            authority_id=str(selected.get("authority_id") or ""),
            authority_group=str(selected.get("identity_group") or ""),
            known_authority=bool(
                selected.get("decision_type") == "registered_authority"
                or selected.get("known_authority")
            ),
            materialization_compatible=bool(
                selected.get("materialization_compatible")
            ),
            fixed_identity_group=(
                str(selected.get("identity_group") or "")
                if selected.get("decision_type") == "prior_named"
                else ""
            ),
        )
        k_source_label = str(selected.get("source_label") or "")
        if k_source_label and evidence_ref:
            redundant_n_echo_k_pairs[(k_source_label, evidence_ref)] = projected[-1]
        absorbed_tokens = [
            token for token in (
                str(raw or "").strip()
                for raw in (item.absorbed_functional_keys or [])
            )
            if token
        ]
        if absorbed_tokens:
            invalid_tokens = [
                token for token in absorbed_tokens
                if token not in absorbable_functional_tokens
            ]
            if invalid_tokens:
                # 安全默认：核验不过就拒绝该声明（硬失败强制重采样），不得
                # 静默接受——伪造的 token 不得混入合法折叠通道。
                errors.append(
                    "current K decision absorbed_functional_keys 越界："
                    f"{decision_id}->{invalid_tokens}"
                )
            else:
                projected[-1]["_current_identity_absorbed_functional_keys"] = (
                    list(absorbed_tokens)
                )
                canonical_name = str(selected.get("canonical_name") or "").strip()
                to_visual_entity_id = (
                    _visual_entity_id_for_resolution_safe({
                        "resolution": "future_identity",
                        "canonical_name": canonical_name,
                    })
                    if canonical_name else None
                ) or (f"bible:{canonical_name}" if canonical_name else "")
                merges: list[dict] = []
                if to_visual_entity_id:
                    label_pairs: list[tuple[str, str]] = []
                    for token in absorbed_tokens:
                        label_pairs.extend(
                            batch_functional_label_sources.get(token, [])
                        )
                        prior_group = (prior_functional_groups or {}).get(token)
                        if prior_group is not None:
                            for label in prior_group.get("source_labels") or []:
                                label_pairs.append(
                                    (str(label or "").strip(), "")
                                )
                        existing_label = (
                            existing_functional_route_labels or {}
                        ).get(token)
                        if existing_label:
                            label_pairs.append((existing_label, ""))
                    seen_pairs: set[tuple[str, str]] = set()
                    for source_label_pair, scope_qualifier_pair in label_pairs:
                        if not source_label_pair:
                            continue
                        pair_key = (source_label_pair, scope_qualifier_pair)
                        if pair_key in seen_pairs:
                            continue
                        seen_pairs.add(pair_key)
                        from_visual_entity_id = (
                            _visual_entity_id_for_resolution_safe({
                                "source_label": source_label_pair,
                                "scope_qualifier": scope_qualifier_pair,
                            })
                            or ""
                        )
                        if (
                            from_visual_entity_id
                            and from_visual_entity_id != to_visual_entity_id
                        ):
                            merges.append({
                                "from_visual_entity_id": from_visual_entity_id,
                                "to_visual_entity_id": to_visual_entity_id,
                                "canonical_name": canonical_name,
                                "merge_rule": "same_batch_k_absorption",
                            })
                if merges:
                    projected[-1][
                        "_current_identity_absorbed_visual_merges"
                    ] = merges
    for item in value.n:
        evidence_ref = _resolved_evidence_ref(
            item.evidence_ref, expected_refs
        )
        record = evidence_by_ref.get(evidence_ref)
        if evidence_ref not in expected_refs or record is None:
            # evidence_ref is enum-declared on CurrentNewNamedIdentityDecision
            # in _current_identity_schema() (new_item["properties"]
            # ["evidence_ref"]["enum"] = refs) and that enum keyword is on
            # _identity_strict_provider_schema's whitelist, so it really is
            # sent to the provider -- see _resolved_evidence_ref's own
            # docstring ("The schema pins evidence_ref to a closed enum, but
            # the provider does not always honour strict mode"). expected_refs
            # == set(evidence_by_ref), so `record is None` cannot fire once
            # evidence_ref is in expected_refs; the only reachable failure
            # mode is the enum violation -- RCA ERR-20260824-e3628f (0.5%
            # supplier-side non-strict-mode sampling defect on a deep array
            # enum), safe to resample the whole episode instead of halting
            # for human RCA.
            errors.append(
                _CurrentIdentitySchemaViolation(
                    f"current N evidence_ref 越界：{evidence_ref}"
                )
            )
            continue
        identity_label = str(item.identity_label or "").strip()
        if (
            str(item.name_kind or "") != IDENTITY_NAME_FORM_PERSONAL
            and identity_label not in (reserved_authority_labels or set())
        ):
            # 尊称与代称永远不能签发新的人物权威。它们先落为功能身份，保留原文
            # 里的逐字称谓，等真名真正出现在证据中时再由 K 决议认领同一个人。
            #
            # 例外：identity_label 命中 reserved_authority_labels 时不适用这条
            # 短路。该集合只收录人物谱已登记的真名/别名，以及本集之前批次已由
            # K/N 决议确认过的身份称谓——都是经过核验的既成事实，不是模型这次
            # 现场的臆测。真名>尊称>代称这条阶梯是为了拦截模型凭空签发新人物卡
            # （生产事故：模型据"许师姐"擅自签发过一张全新人物卡），对已核验
            # 事实继续套用同一条防臆测规则就是把规则用错了对象——未登记的尊称/
            # 代称仍然一律在这里落 functional，不受影响。命中后放行到下面与
            # personal_name 相同的处理路径，由已有的 reserved_authority_labels
            # 命中逻辑（含"必须选择 K decision"的强制与 K/N 冗余回显丢弃）接管。
            append_candidate(
                source_label=identity_label,
                canonical_name="",
                identity_kind="functional",
                functional_key=_identity_form_functional_key(identity_label),
                kind=item.kind,
                record=record,
            )
            continue
        if identity_label in (reserved_authority_labels or set()) and not any(
            identity_label in str(owned.get("text") or "")
            for owned in evidence_by_ref.values()
        ):
            # 模型是从上下文认出了一位已登记人物，而不是读到了逐字姓名。合同要求
            # 这类「称谓 A 其实是名字 B」的判断先落为 functional，可这里没有任何
            # 逐字称谓可以留下来当 source_label。后面的结构化身份覆盖审计会用原文
            # 中真正出现的称谓把这个人补回来，所以丢弃这一条声明，而不是让整集
            # 预检硬失败。
            continue
        if identity_label in (reserved_authority_labels or set()):
            k_echo_candidate = redundant_n_echo_k_pairs.get(
                (identity_label, evidence_ref)
            )
            if k_echo_candidate is not None:
                # 第35轮 ERR-20260824-bc3d14：这条 n 声明命中的 (identity_label,
                # evidence_ref) 复合键已经在本响应的 k 数组里拿到一份合规决议
                # ——模型对同一个人签发了两份声明，k 是权威、n 是冗余回显。
                # 静默丢弃这条 n（不采信其中任何字段，包括它自己可能携带的
                # canonical_name/identity_kind 判断），身份仍以那条 K 决议为
                # 准；在 K 决议对应的候选上留一个可观测标记（风格对齐
                # _current_identity_synthesized_qualifier），供测试/回归核验
                # 丢弃确实发生，而不是让整集因模型自己已经正确处理过的人复读
                # 一次就预检硬失败。
                k_echo_candidate[
                    "_current_identity_redundant_n_echo_dropped"
                ] = True
                continue
        append_candidate(
            source_label=identity_label,
            canonical_name=identity_label,
            identity_kind="named",
            functional_key="",
            kind=item.kind,
            record=record,
        )
    for item in value.f:
        evidence_ref = _resolved_evidence_ref(
            item.evidence_ref, expected_refs
        )
        record = evidence_by_ref.get(evidence_ref)
        if evidence_ref not in expected_refs or record is None:
            # 同上 N 分支：evidence_ref 在 CurrentFunctionalIdentityDecision 上
            # 同样是 enum 声明（functional_item["properties"]["evidence_ref"]
            # ["enum"] = refs），且 enum 在 provider 白名单内，真正发给了供应商。
            # ERR-20260824-e3628f 的 F evidence_ref="E0" 就是这条分支：目录
            # 84 项、strict=true 全部具备，供应商依然低频（约 0.5%）吐出枚举外
            # 的值——是供应商侧非严格解码的格式缺陷，不是我们的信息缺口，
            # 安全可重采样整集，不需要人工 RCA。
            errors.append(
                _CurrentIdentitySchemaViolation(
                    f"current F evidence_ref 越界：{evidence_ref}"
                )
            )
            continue
        append_candidate(
            source_label=item.source_label,
            canonical_name="",
            identity_kind="functional",
            functional_key=item.functional_identity_key,
            kind=item.kind,
            record=record,
            scope_qualifier=item.scope_qualifier,
        )

    merged: list[dict] = []
    # 唯一性判定键（真实第18轮 EP10 回归 ERR-20260824-b16bb4，结构性方案 a）：
    # 复合键 (source_label, scope_qualifier)，不再是裸 source_label。关系
    # 称谓（"师弟"类）天然可以在同一章合法指向不同人——旧的裸 source_label
    # 唯一键假设对这类称谓不成立（模型行为正确，是契约键设计过窄）。
    # scope_qualifier 是模型自己按 prompt 规则8申报的区分限定语，默认空串
    # （未申报=沿用旧行为，同一 source_label 仍然只有一个唯一性域，见
    # test_two_distinct_people_same_label_different_key_still_hard_fails
    # ——那条测试没有用到 scope_qualifier，必须继续因同一复合键
    # ("男子","") 硬拒，不受本次改动影响）。判据是结构性的，不认识
    # "师弟"这个具体词形，也不需要模型说明"这是关系称谓"，模型只要在
    # 自己判断可能有歧义时给出限定语即可。
    by_label: dict[tuple[str, str], list[dict]] = {}
    for item in projected:
        key = (
            str(item.get("source_label") or "").strip(),
            str(item.get("scope_qualifier") or "").strip(),
        )
        by_label.setdefault(key, []).append(item)
    for (source_label, _scope_qualifier), options in by_label.items():
        reconciled = _current_identity_reconcile_as_single(options)
        if reconciled is not None:
            merged.append(reconciled)
            continue
        # 第31轮真实回归 ERR-20260824-614276（EP5，两条"老者"）：跟马脸
        # 青年案（申报逐字段雷同→归一合并）方向相反——这次模型用不同
        # functional_identity_key（或不同 decision_id，见
        # _current_identity_disambiguation_key）明确申报了两个人（第 5 章
        # 确实有两位老者），只是没填人类可读的 scope_qualifier，导致两条
        # 都落进同一个 (source_label, "") 复合键、彼此"撞车"。区分的事实
        # 判断模型已经做出（不同 F 键本身就是模型自己的区分信号，跟"jason
        # 逐字段雷同"是完全相反的申报形状），拒绝重来是浪费——按各子组
        # （子组内部仍然分别用①②同一套判据核验，子组内部若还自相矛盾，
        # 说明连"是不是同一个人"这个最基本的申报都不自洽，那才是真矛盾，
        # 见下方 subgroup_conflict 分支）各自的最早证据首现顺序，用第20轮
        # 既有 _identity_disambiguating_suffix 机制（甲/乙/丙...）确定性
        # 补足 scope_qualifier，标记 synthesized（观测计数），复合键唯一性
        # 随即满足，不需要模型重新申报。
        identity_subgroups: dict[str, list[dict]] = {}
        for item in options:
            identity_subgroups.setdefault(
                _current_identity_disambiguation_key(item), [],
            ).append(item)
        subgroup_conflict = False
        resolved_subgroups: list[dict] = []
        if len(identity_subgroups) > 1:
            for subgroup_options in identity_subgroups.values():
                sub_reconciled = _current_identity_reconcile_as_single(
                    subgroup_options,
                )
                if sub_reconciled is None:
                    # 同一 F 键/decision_id 内部仍然自相矛盾（马脸青年案的
                    # ②分支，本次不动）：这不是"两个人缺限定语"的形状，是
                    # 模型对同一个身份的申报本身自相矛盾，跌回下面原有的
                    # 致命反馈路径，用完整的 options（不是子组）报冲突。
                    subgroup_conflict = True
                    break
                resolved_subgroups.append(sub_reconciled)
        if len(identity_subgroups) > 1 and not subgroup_conflict:
            resolved_subgroups.sort(
                key=lambda item: _current_identity_receipt_sort_key(
                    item.get("source_evidence_receipt") or {},
                ),
            )
            for index, resolved in enumerate(resolved_subgroups, start=1):
                resolved["scope_qualifier"] = _identity_disambiguating_suffix(index)
                resolved["_current_identity_synthesized_qualifier"] = True
                merged.append(resolved)
            continue
        # ②实质分歧：申报内容本身就不一致（不同 kind、不同 functional_
        # identity_key、不同 canonical_name……），真的没法确定是不是同一个
        # 人，维持致命——但反馈必须让模型看得懂"错在哪、怎么改"，不能只
        # 甩一个错误码：把冲突的每一条内容并排列出，并给出确定性的修复
        # 指令（同一称谓指多人 -> 各自给 scope_qualifier；指同一人 -> 合并
        # 为一条、共用同一个 functional_identity_key/decision_id）。
        conflict_dump = json.dumps(
            [
                {
                    "identity_kind": item.get("identity_kind"),
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "functional_identity_key": item.get(
                        "_current_response_group_key"
                    ),
                    "authority_id": item.get("authority_id"),
                }
                for item in options
            ],
            ensure_ascii=False, separators=(",", ":"),
        )
        errors.append(
            f"source_label 重复：{source_label}；冲突内容并排对比："
            f"{conflict_dump}；若这几条指的是不同的人，请为每一条各自的 "
            "scope_qualifier 填写能互相区分的限定语；若指的是同一个人，"
            "请合并为一条并共用同一个 functional_identity_key（f 分支）"
            "或同一个 decision_id（k 分支）"
        )
        groups = {
            str(item.get("identity_group") or "").strip() for item in options
        }
        if len(groups) > 1:
            errors.append(
                "current 同一 source_label 对应多个 identity_group："
                f"{source_label}"
            )
        merged.extend(options)
    return merged, errors


def _current_identity_projection_errors(candidates: list[dict]) -> list[str]:
    """Reject cross-batch projection conflicts instead of last/first wins.

    真实第20轮 EP4 回归 ERR-20260824-407c9b：这是 _project_current_identity_
    response（单批内按 (source_label, scope_qualifier) 复合键判定唯一性，见
    该函数的 by_label 分组注释）**之外**、独立的一道**跨批**一致性检查——
    长章节按证据切成多批分别调用模型，同一 source_label 若在不同批次里被
    判成不同 identity_group，说明模型自相矛盾，必须拒绝而不是"后者覆盖前者"
    静默吞掉。ERR-20260824-b16bb4（"师弟"关系称谓消歧）修复时只升级了
    单批内的判定键，这道跨批检查仍按裸 source_label 分组——上游单批内
    已经合法放行的"两个外宗弟子"（不同 scope_qualifier、不同 identity_
    group）跨批一比对，照样被这里当成"同一 source_label 冲突"重新拦下。
    键同样升级为 (source_label, scope_qualifier) 复合键，跟单批内判定用
    同一把尺子；没有 scope_qualifier（空串）的称谓不受影响，继续按裸
    label 生效。
    """
    by_label: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for item in candidates:
        label = str(item.get("source_label") or "").strip()
        qualifier = str(item.get("scope_qualifier") or "").strip()
        by_label.setdefault((label, qualifier), set()).add((
            str(item.get("identity_group") or "").strip(),
            str(item.get("identity_kind") or "").strip(),
            str(item.get("name") or "").strip(),
        ))
    return [
        f"current 投影后同一 source_label 冲突：{label}"
        for (label, _qualifier), signatures in by_label.items()
        if not label or len(signatures) != 1
    ]


def _record_visual_entity_merge(
    conn,
    project_id: str,
    *,
    from_visual_entity_id: str,
    to_visual_entity_id: str,
    canonical_name: str,
    evidence_episode_no: int,
    merge_rule: str = "same_batch_k_absorption",
) -> bool:
    """落一条视觉实体折叠记账（``visual_entity_merges``，设计文档 §4.2）。

    ``visual_entity_merges`` 表由并行改动在 ``app/db.py`` 落地迁移（本文件
    不实现该迁移）；表尚未存在时静默跳过——折叠本身（K 决议的命名侧结果）
    已经生效，记账是可补齐的审计侧信息，不构成折叠是否成立的前置条件，
    避免让本文件对尚未合并的迁移产生硬依赖。选图沿用设计文档 §4.2 的规则：
    优先复用 ``to`` 侧（规范权威）已有的 ready 定妆照。
    """
    if not project_id or not from_visual_entity_id or not to_visual_entity_id:
        return False
    if from_visual_entity_id == to_visual_entity_id:
        return False
    selected_portrait_id = None
    if canonical_name:
        try:
            row = conn.execute(
                "SELECT id FROM character_portraits WHERE project_id=? "
                "AND character_name=? AND pack_status='ready' "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, canonical_name),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            selected_portrait_id = row["id"]
    try:
        conn.execute(
            "INSERT INTO visual_entity_merges (id, project_id, "
            "from_visual_entity_id, to_visual_entity_id, canonical_name, "
            "merge_rule, selected_portrait_id, evidence_episode_no, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                new_id("visual_merge"),
                project_id,
                from_visual_entity_id,
                to_visual_entity_id,
                canonical_name,
                merge_rule,
                selected_portrait_id,
                evidence_episode_no,
                now(),
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return False
    return True


def _record_current_identity_absorbed_visual_merges(
    project_id: str | None,
    episode_no: int,
    candidates: list[dict],
) -> None:
    """遍历一批已通过校验的 current-identity 候选，落地同批折叠的视觉记账。

    折叠的核验与纯计算已经在 ``_project_current_identity_response`` 内完成
    （见该函数 k 循环里 ``_current_identity_absorbed_visual_merges`` 的写入）
    ——这里只做落库这一步的副作用，且只在整批（含跨批一致性检查）都通过后
    才调用（见 ``_discover_character_candidates_legacy`` 的调用点），避免为
    一个最终会被拒绝重试的响应写入记账。``project_id`` 缺失时（历史调用点
    尚未传入，见 ``discover_character_candidates`` 的可选形参）静默跳过——
    折叠的命名侧结果不受影响，只是暂不记账。
    """
    if not project_id:
        return
    conn = get_conn()
    for item in candidates:
        merges = item.get("_current_identity_absorbed_visual_merges") or []
        for merge in merges:
            _record_visual_entity_merge(
                conn,
                project_id,
                from_visual_entity_id=str(
                    merge.get("from_visual_entity_id") or ""
                ),
                to_visual_entity_id=str(
                    merge.get("to_visual_entity_id") or ""
                ),
                canonical_name=str(merge.get("canonical_name") or ""),
                evidence_episode_no=episode_no,
                merge_rule=str(
                    merge.get("merge_rule") or "same_batch_k_absorption"
                ),
            )


async def _discover_character_candidates_legacy(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    future_text: str = "",
    future_label: str = "",
    existing_resolutions: list[dict] | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Resolve the current episode's cast before/after screenplay generation.

    后续章节只能用来把当前章节的“大汉/老者/黑衣人”解析成稳定真名，
    不得把尚未出场的人物或剧情带回本集。身份模型确认的稳定真名必须完成
    最小人物卡；未确认真名的一次性人物保留来源称谓并签发 typed functional identity。
    """
    known_names = [c.name for c in bible.characters if c.name]
    known = "、".join(known_names) or "（无）"
    existing_functional_routes = {
        str(item.get("canonical_name") or "").strip()
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }
    existing_resolution_projection = [
        {
            "source_label": str(item.get("source_label") or "").strip(),
            "canonical_name": str(item.get("canonical_name") or "").strip(),
        }
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("source_label") or "").strip()
            and str(item.get("canonical_name") or "").strip()
        )
    ]
    # 同批折叠通道（absorbed_functional_keys）第三类可吸收来源——"本集已有
    # 功能身份决议"——的 canonical_name -> source_label 反查表：
    # existing_functional_routes（下方）只是扁平 canonical_name 集合，供
    # 成员关系核验；这里额外保留 source_label，供折叠命中后计算被吸收组的
    # visual_entity_id（见 _project_current_identity_response 的 k 循环）。
    existing_functional_route_labels = {
        item["canonical_name"]: item["source_label"]
        for item in existing_resolution_projection
        if item["canonical_name"] and item["source_label"]
    }
    current_authorities = identity_authority_registry(
        bible,
        existing_resolutions or [],
    )
    reserved_authority_labels = {
        str(label).strip()
        for authority in current_authorities
        if str(authority.get("identity_kind") or "").strip() != "functional"
        for label in (
            authority.get("canonical_name"),
            *(authority.get("source_labels") or []),
        )
        if str(label or "").strip()
    }
    current_haystack = f"{source_text or ''}\n{draft_text or ''}"
    current_evidence_batches = _current_identity_evidence_batches(
        source_text,
        draft_text=draft_text,
    )
    seen: set[tuple[str, str, str]] = set()
    candidates: list[dict] = []
    current_provider, current_model, current_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    current_semantic_settings = hiagent.text_request_semantic_settings(
        current_provider
    )

    def collect(
        raw: str,
        *,
        identity_haystack: str,
        group_scope: str,
    ) -> None:
        try:
            obj = extract_json(raw, repair_unescaped_inner_quotes=True)
        except ValueError as exc:
            raise ContentGenerationError(
                "人物身份模型返回了不可验证的非结构化结果，当前阶段已停止"
            ) from exc
        for item in obj.get("characters") or []:
            if not isinstance(item, dict):
                continue
            # 兼容旧模型形状 {name, kind, evidence}。新协议的身份判断完全以模型输出为准。
            legacy_name = str(item.get("name") or "").strip()
            model_source_label = str(
                item.get("source_label") or legacy_name
            ).strip()
            source_label = _aligned_identity_source_label(
                model_source_label,
                current_haystack,
            )
            identity_kind = str(item.get("identity_kind") or "named").strip().lower()
            canonical_name = str(item.get("canonical_name") or legacy_name).strip()
            if identity_kind not in {"named", "functional"}:
                continue
            future_evidence = str(
                item.get("future_evidence") or ""
            ).strip()
            if (
                group_scope in {"future", "coverage"}
                and canonical_name
                and future_evidence
                and canonical_name in known_names
            ):
                # Only an existing canonical Bible identity can repair provider
                # enum drift. Repeating a relation/description in source text
                # proves label presence, not a stable named identity.
                identity_kind = "named"
            elif identity_kind == "functional":
                canonical_name = ""
            dedupe_key = (source_label, canonical_name, identity_kind)
            if (
                not source_label
                or len(source_label) > 16
                or dedupe_key in seen
                or (
                    identity_kind == "named"
                    and (
                        not canonical_name
                        or len(canonical_name) > 16
                        or (
                            canonical_name not in identity_haystack
                            and canonical_name not in known_names
                        )
                    )
                )
            ):
                continue
            seen.add(dedupe_key)
            functional_identity_key = str(
                item.get("functional_identity_key") or ""
            ).strip()[:64]
            existing_route_name = (
                functional_identity_key
                if functional_identity_key in existing_functional_routes
                else ""
            )
            prior_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if (
                    str(candidate.get("source_label") or "").strip()
                    == source_label
                    and str(candidate.get("identity_group") or "").strip()
                )
            }
            declared_group = str(
                item.get("identity_group")
                or functional_identity_key
                or ""
            ).strip()
            existing_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if str(candidate.get("identity_group") or "").strip()
            }
            declared_matches = {
                group
                for group in existing_groups
                if (
                    declared_group
                    and (
                        group == declared_group
                        or group.endswith(f":{declared_group}")
                    )
                )
            }
            identity_group = (
                next(iter(prior_groups))
                if group_scope in {"future", "coverage"} and len(prior_groups) == 1
                else (
                    next(iter(declared_matches))
                    if (
                        group_scope in {"future", "coverage"}
                        and len(declared_matches) == 1
                    )
                    else (
                        f"existing:{existing_route_name}"
                        if existing_route_name
                        else f"{group_scope}:{declared_group or source_label}"
                    )
                )
            )
            candidates.append({
                "name": canonical_name or source_label,
                "source_label": source_label,
                "identity_kind": identity_kind,
                "identity_group": identity_group,
                "existing_route_name": existing_route_name,
                "kind": "mentioned" if item.get("kind") == "mentioned" else "onscreen",
                "evidence": str(item.get("evidence") or "").strip()[:80],
                "future_evidence": future_evidence[:120],
                "source_segment_id": str(
                    item.get("source_segment_id") or ""
                ).strip(),
                "source_quote": str(item.get("source_quote") or "").strip()[:240],
                "model_source_label": (
                    model_source_label
                    if model_source_label != source_label else ""
                ),
            })

    for current_batch, evidence_records in enumerate(
        current_evidence_batches, start=1
    ):
        evidence_by_ref = {
            f"E{index:03d}": record
            for index, record in enumerate(evidence_records, start=1)
        }
        evidence_catalog = [
            {
                "evidence_ref": evidence_ref,
                "origin": str(record.get("origin") or ""),
                "source_segment_id": str(
                    record.get("source_segment_id") or ""
                ),
                "path": str(record.get("path") or ""),
                "text": str(record.get("text") or ""),
            }
            for evidence_ref, record in evidence_by_ref.items()
        ]
        registered_decisions = _current_identity_known_decision_catalog(
            evidence_by_ref,
            authorities=current_authorities,
        )
        prior_named_decisions, prior_functional_groups = (
            _current_identity_prior_decision_catalog(
                evidence_by_ref,
                prior_candidates=candidates,
            )
        )
        known_decisions = {
            **registered_decisions,
            **prior_named_decisions,
        }
        known_decision_projection = [
            {
                "decision_id": decision_id,
                "decision_type": str(item.get("decision_type") or ""),
                "evidence_ref": str(item.get("evidence_ref") or ""),
                "source_label": str(item.get("source_label") or ""),
                "canonical_name": str(item.get("canonical_name") or ""),
                "allowed_kinds": (
                    ["onscreen", "mentioned"]
                    if item.get("materialization_compatible")
                    else ["mentioned"]
                ),
            }
            for decision_id, item in sorted(known_decisions.items())
        ]
        prior_functional_projection = [
            {
                "decision_id": decision_id,
                "source_labels": list(item.get("source_labels") or []),
                "existing_route_name": str(
                    item.get("existing_route_name") or ""
                ),
            }
            for decision_id, item in sorted(prior_functional_groups.items())
        ]
        prior_decision_catalog_hash = evidence_repository.content_hash({
            "named": [
                item for item in known_decision_projection
                if str(item.get("decision_type") or "").startswith("prior_")
            ],
            "functional": prior_functional_projection,
        })
        evidence_catalog_hash = evidence_repository.content_hash({
            "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
            "evidence": evidence_records,
            "known_decisions": known_decision_projection,
            "prior_functional_groups": prior_functional_projection,
        })
        current_schema = _current_identity_schema(
            list(evidence_by_ref),
            known_decision_ids=list(known_decisions),
        )
        current_response_format = _identity_strict_response_format(
            current_schema,
            name="screenplay_current_identity_discovery_v11",
        )
        prompt = f"""任务：为第 {episode_no} 集做人物身份增量预检。请用语义和上下文判断，
不要依赖服饰、性别、年龄或称谓后缀的固定词表。

当前人物谱已有角色：
{known}

前批已确定的 functional 分组 P 决议（后批判断为同一人时，functional_identity_key 必须精确复用 decision_id）：
{json.dumps(prior_functional_projection, ensure_ascii=False, separators=(',', ':'))}

本批 backend-owned 当前身份证据目录。E ref 已绑定完整证据 receipt，禁止跨 E 搬运人物：
{json.dumps(evidence_catalog, ensure_ascii=False, separators=(',', ':'))}

本批已登记身份 K 决议目录（只有这些 decision_id 可进入 k；目录为空则所有 k=[]）：
{json.dumps(known_decision_projection, ensure_ascii=False, separators=(',', ':'))}

本集已有功能身份决议（可为空；canonical_name 是已分配的本集稳定 ID）：
{json.dumps(existing_resolution_projection, ensure_ascii=False, separators=(',', ':'))}

规则：
1. root 只输出一次 k/n/f 三个全局数组，不得输出 decisions，也无需覆盖没有人物的 E。
   每个身份/称谓只输出一次，从它的 owned 证据中选最清晰的 E；同一 E 可支持多人。
2. 已登记身份只可选 k：decision_id 精确复制 K 目录，该 token 已绑定 E；
   kind 必须属于该 K 的 allowed_kinds；
   不得把 K 目录中的 source_label 写进 n/f。只允许 mentioned 的 K 没有可安全物化的最终人物卡
   authority；若人物实际出镜则必须停止而不能谎报 mentioned 或另造身份。
   本批 K 目录没有为「当前人物谱已有角色」中的某人签发 decision_id，说明本批证据没有
   逐字锚定他：此时既不得把他的真名写进 n，也不得据上下文推断，只能把你实际读到的
   逐字称谓按第 4 条放入 f，交给后续带 authority_id 的权威绑定去认领。人物谱名单只用于
   识别，不是可以直接书写的名字。
3. 当前阶段的新 named 只用于逐字自称谓：n 每项写 evidence_ref、identity_label、name_kind 与 kind，
   identity_label 必须是所选 E text 的连续逐字子串；后端会令 canonical_name=source_label。
   {IDENTITY_NAME_FORM_RULE}
   name_kind 只描述 identity_label 这个字符串本身的形态，与你是否认得这个人无关；
   尊称或代称请照实写 honorific/referential，后端会自动把它落为功能身份。
   任何“称谓 A 其实是名字 B”的别名判断，即使 A、B 同时出现在当前输入，也必须先判为
   functional，交由后续带 authority_id 的权威绑定；不得用同场共现代替同一性证据。
4. 若是一次性角色，别名待后续确认，或无法确认稳定真名，放入 f；每项填写
   evidence_ref、source_label、functional_identity_key、kind，不得携带 canonical/authority/evidence。
   source_label 尽量逐字复用所选 E text；若为区分同段多个无名实体而
   必须使用非逐字的稳定描述，只能保留为 functional，后端会隔离为 synthetic identity，
   不得将它当作别名或真名。
   若同一实体在证据里有多个逐字可用的称呼（如「凶兽」与「一只约莫一人大小，
   样子如猴般的凶兽」），必须选其中最短的那个稳定称谓：source_label 只是一个
   可复用的身份标签，不是用来证明你读到了完整描述。
   source_label 不得包含 、，,／/；;｜|＆&＋+ 等分隔符标点或空白：后端会按这些
   字符切分身份列表（如台词发言人、场次角色表），混入分隔符会让一个人被错误
   切成多段身份。
5. 若身份投影中的 source_label 混入动作或表演提示，必须结合对应 line_context 判断真正说话人；
   source_label 保留原始完整字符串，canonical_name/functional_identity_key 绑定到真正说话人。
   禁止按“说、喊、点头”等固定词表或后缀规则猜测。
6. 每个 f 项必须填写 functional_identity_key：
   - 若它与“前批已确定的 functional 分组”是同一人，必须精确复制该 P decision_id；
     不得重新使用前批原始分组字符串。
   - 若它与“本集已有功能身份决议”中的某人是同一人，精确填写该人的 canonical_name。
   - 否则填写本次响应内的不透明分组 ID（如 F1、F2）；不同 source_label 若明确是同一人必须共用同一 ID。
   - 无法确认是否同一人时必须使用不同 ID，禁止根据称谓字面相似猜测。
7. 每个人只输出一次；不得因多次出现重复输出，不得因共用证据合并人物，
   也不得把同一 source_label 放入多个分支。后端只会聚合语义签名完全相同的合法重复，
   任何跨分支、跨分组或非逐字 synthetic 重复都会硬失败。
8. f 每项还需要填写 scope_qualifier（默认可留空字符串）：如果同一个 source_label
   在本批不止一次出现、且这几次实际指的不是同一个人——比如「师弟」「师兄」「道友」
   「前辈」这类相对说话人或语境而定的关系称谓/身份指代，本就可能在同一批里对应
   不同人——必须给每一次单独填一句简短、能从对应证据里直接读出依据的限定语，说明
   这次具体是哪一个人（如取自证据的动作、对话对象或所在场景），确保同一 source_label
   下不同的人各自的 scope_qualifier 互不相同。如果这几次确实是同一个人反复出现，
   或这个称谓本就唯一指向一个人，scope_qualifier 留空即可。判断依据是"这次读到的
   是不是同一个人"，不是称谓字面是什么词；拿不准时倾向于填写限定语而不是留空，
   避免把两个不同的人误合并成一个人。同一 source_label 下用不同
   functional_identity_key 申报了多个人时，必须各自填写能互相区分的
   scope_qualifier——不要依赖后端的确定性降级补足（后端会用甲/乙/丙...
   兜底填一个可用但没有语义信息量的限定语，只是防止拒绝重来，不是让你
   可以不填）。
9. absorbed_functional_keys 的合法取值域只有三类，逐项必须精确复制其中
   之一——本批 f 项自己声明过的 functional_identity_key、前批 P token
   （prior functional 分组的 decision_id）、或本集已有功能身份决议的
   canonical_name；不是任意你认为"指代同一人"的称谓原文。后端只核验每个
   token 是否确实来自这三类来源，不做文本语义判断，越界或臆造的 token
   （包括任何未按上述三类之一先行声明过的称谓原文）都会导致本次响应被
   拒绝重试。

   这三类来源有一个共同前提：token 背后的实体在被吸收前必须处于"稳定真名
   尚未确认"的功能性占位状态——这正是规则4"若…无法确认稳定真名，放入 f"
   的适用范围。一个人只要已经有确定真名（不论是这条 k 决议刚揭晓的，还是
   人物谱/更早证据里早已确认的），TA 的其它称谓从一开始就不满足"功能性
   占位"这个前提，永远不构成合法的 f 项，也就永远不会出现在上述三类合法
   来源里——不得为了让某个称谓能被吸收，倒着现造一条 f 项把它包装成功能性
   占位；f 项存在的理由是"真名未定"，不是"我想吸收它"。这类已有确定真名
   之人的其它称谓，走称谓解析的正常渠道（n 的逐字自称谓声明、或人物谱别名
   登记），不进 absorbed_functional_keys。（真实事故：「孟才子」「孟兄」是
   孟浩的称谓、「王伯的儿子」是王有材的称谓、「许师姐」是许清的称谓——这
   四人都已有确定真名，从一开始就不是合法的 f 项，任何 k 决议都不得把这类
   称谓原文填入 absorbed_functional_keys。）

   合法用例：如果某个 k 决议揭晓的真名，其实就是一个仍处于 functional 状态
   的称谓组一路指代的同一个人——例如某绰号从更早的证据起就被追踪为
   functional，直到这条 k 决议对应的证据才第一次读到该人物的真名——不要
   把真名重复写进 n（那是这条 k 决议已经覆盖的重复声明，会被拒绝）：改为
   在这条 k 决议里填写 absorbed_functional_keys，逐项精确复制被吸收的
   functional_identity_key/P token/canonical_name。只有在你确实判断这些
   token 指代的是同一个人时才填写；拿不准是否为同一人时留空，不要吸收。

   absorbed_functional_keys 里禁止填入这条 k 决议自己的 source_label（即
   本批 K 决议目录里这个 decision_id 条目自带的 source_label 原文）：选中
   decision_id 本身已经表达了这个称谓属于该决议，重复列出会被判定为越界
   token 而拒绝，不是多填了一道保险。absorbed_functional_keys 只能用来
   吸收这个自身称谓之外的、真正处于功能性占位状态的其它称谓组（不是任何
   已有确定真名之人的称谓，见本条前半段）——如果某个这样的称谓只是你在
   证据里零散认出、还没有单独作为一条 f 项列出（source_label 与 functional_
   identity_key 均已确定），它就还不是合法的可吸收 token：必须先在本响应
   的 f 数组里为它单独声明一条 f 项（source_label 填该称谓本身，
   functional_identity_key 可以直接使用你打算吸收的同一个 key），再在
   absorbed_functional_keys 里精确复制那个 key。第7条"每个人只输出一次"
   约束的是同一个人不得被同时判给两个互相冲突的最终身份归属，不禁止你为
   将被吸收的称谓单独声明它自己的 f 项——被吸收的 f 项与吸收它的 k 决议
   共存，就是这条通道设计的正常形态。
只输出 response_format 约束的 JSON，不要复述证据、Schema 或规则。"""

        def validate_current_response(
            value: CurrentIdentityCandidateResponse,
        ) -> list[str]:
            _projected, errors = _project_current_identity_response(
                value,
                evidence_by_ref=evidence_by_ref,
                known_decisions=known_decisions,
                prior_functional_groups=prior_functional_groups,
                reserved_authority_labels=reserved_authority_labels,
                group_scope=f"current-{current_batch}",
                existing_functional_routes=existing_functional_routes,
                existing_functional_route_labels=existing_functional_route_labels,
            )
            # chat_structured has no format/semantic split on a validate()
            # callback's return value (strict_identity_substage forbids this
            # call from using format_retry_limit/semantic_retry_limit anyway,
            # see app.harness.model_gateway) -- any non-empty return here
            # raises StructuredSemanticError immediately. A response whose
            # *only* faults are wire-schema-declared (enum out of range) must
            # not trip that: filtering them out here lets chat_structured
            # accept the response, and the explicit re-check right after
            # `_identity_structured_with_resample` below raises
            # StructuredFormatError for them instead -- see that call site's
            # comment for the full reasoning. Any genuine semantic fault
            # (source_label 重复 等业务判断分歧) still returns non-empty here
            # and still halts on the first attempt, unchanged.
            return [
                error for error in errors
                if not _current_identity_is_schema_violation(error)
            ]

        response = await _identity_structured_with_resample(
            [{"role": "user", "content": prompt}],
            model_type=CurrentIdentityCandidateResponse,
            validate=validate_current_response,
            normalize_payload=_normalize_current_identity_payload,
            operation_id_for_attempt=lambda resample_attempt: (
                f"screenplay.identity.current.v6:{episode_no}:{current_batch}:"
                + evidence_repository.content_hash({
                    "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                    "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                    "current_evidence_catalog_hash": evidence_catalog_hash,
                    "prior_decision_catalog_hash": prior_decision_catalog_hash,
                    "provider": current_provider,
                    "model": current_model,
                    "requested_max_tokens": 8192,
                    "effective_max_tokens": current_effective_max,
                    "temperature": 0.1,
                    "provider_semantic_settings": current_semantic_settings,
                    "retry_epoch": _identity_operation_retry_epoch(),
                    "resample_attempt": resample_attempt,
                    "prompt": prompt,
                    "schema": current_schema,
                    "response_format": current_response_format,
                })
            ),
            temperature=0.1,
            max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
            format_retry_limit=0,
            semantic_retry_limit=0,
            call_meta={
                "stage": "discover_character_candidates",
                "stage_key": "screenplay_character_discovery",
                "substage": "current_identity",
                "episode_no": episode_no,
                "discovery_phase": "current",
                "source_batch": current_batch,
                "source_batches": len(current_evidence_batches),
                "reuse_successful_operation": False,
                "disable_provider_retries": True,
                "disable_provider_candidate_fallback": True,
                "disable_reasoning_fallback": True,
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                "current_evidence_catalog_hash": evidence_catalog_hash,
                "current_decision_catalog_hash": evidence_repository.content_hash(
                    known_decision_projection
                ),
                "prior_decision_catalog_hash": prior_decision_catalog_hash,
                "schema_hash": evidence_repository.content_hash(current_schema),
                "provider": current_provider,
                "model": current_model,
                "effective_max_tokens": current_effective_max,
                "provider_semantic_settings": current_semantic_settings,
                "retry_epoch": _identity_operation_retry_epoch(),
            },
            output_schema=current_schema,
            response_format=current_response_format,
            require_response_format=True,
        )
        batch_candidates, projection_errors = (
            _project_current_identity_response(
                response,
                evidence_by_ref=evidence_by_ref,
                known_decisions=known_decisions,
                prior_functional_groups=prior_functional_groups,
                reserved_authority_labels=reserved_authority_labels,
                group_scope=f"current-{current_batch}",
                existing_functional_routes=existing_functional_routes,
                existing_functional_route_labels=existing_functional_route_labels,
            )
        )
        if projection_errors:
            schema_violations = [
                error for error in projection_errors
                if _current_identity_is_schema_violation(error)
            ]
            semantic_violations = [
                error for error in projection_errors
                if not _current_identity_is_schema_violation(error)
            ]
            if semantic_violations:
                # 真正的业务判断分歧（如 source_label 重复），维持既有语义
                # 失败/即停：即便同批还夹带了 wire-schema 越界，也不得靠重
                # 采样蒙混过真信号，两类原文一并报出方便排障。
                raise ContentGenerationError(
                    "；".join(semantic_violations + schema_violations)
                )
            # 走到这里说明本批 projection_errors 只剩 wire-schema 已声明的
            # 越界——validate_current_response 已经把这些从 chat_structured
            # 的 validate() 反馈里过滤掉，所以 chat_structured 把 response 当
            # 成验证通过返回；这是它们第一次真正被拦下。供应商对深层数组
            # enum 的严格模式偶发失效（RCA ERR-20260824-e3628f，约 0.5%
            # 采样缺陷），改判为格式失败：不在本次调用内重采样
            # （strict_identity_substage 强制 format_retry_limit=0），而是把
            # StructuredFormatError 交给 scripts/yyft_serial10.py 的瞬时族
            # 分诊（exc_type=='StructuredFormatError'），本集 60s 后整体
            # 重发一次，不需要人工 RCA。
            raise model_gateway.StructuredFormatError(
                "；".join(schema_violations)
            )
        candidates.extend(batch_candidates)

    if projection_errors := _current_identity_projection_errors(candidates):
        raise ContentGenerationError("；".join(projection_errors))

    _record_current_identity_absorbed_visual_merges(
        project_id, episode_no, candidates
    )

    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and item.get("kind") == "onscreen"
            and str(item.get("identity_group") or "").strip()
        )
    }
    future_candidates = [
        item
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and (
                item.get("kind") == "onscreen"
                or str(item.get("identity_group") or "").strip()
                in unresolved_onscreen_groups
                or str(item.get("source_label") or "").strip()
                in future_text
            )
        )
    ]
    future_context = _future_identity_context(
        future_text,
        [item["source_label"] for item in future_candidates],
        known_names=known_names,
        current_text=source_text,
    )
    if future_context:
        current_identity_audit_source = str(source_text or "").strip()
        if len(current_identity_audit_source) > CAST_DISCOVERY_SOURCE_BUDGET:
            half = CAST_DISCOVERY_SOURCE_BUDGET // 2
            current_identity_audit_source = (
                current_identity_audit_source[:half]
                + "\n……（身份覆盖复核中段省略）……\n"
                + current_identity_audit_source[-half:]
            )
        future_prompt = f"""任务：只为当前集已经发现的人物称谓做后续姓名消歧。

当前人物谱已有角色：
{known}

当前集尚未确认真名的候选：
{json.dumps(future_candidates, ensure_ascii=False, separators=(',', ':'))}

当前集原文（同时复核第一遍是否漏掉独立出场/开口的实体）：
{current_identity_audit_source}

后续章节中命中这些称谓的局部窗口（{future_label or '后续章节'}）：
{future_context}

规则：
1. 优先输出当前集候选中 source_label 完全相同的项目；若第一遍遗漏了当前原文中可区分、
   独立出场或开口的实体，也必须补充输出，source_label 使用当前原文逐字称谓。
   称谓可以在章节边界发生变化；
   若当前集离场状态、后续开场承接和人物谱真名窗口共同形成唯一同一性证据，可据此确认，
   不要求旧称谓与真名必须出现在同一句。
2. canonical_name 必须出现在后续窗口或当前人物谱中；稳定唯一的法号、尊号、专属称号
   也属于 named identity，不要求必须是户籍式真名；有歧义就不输出。
3. 不得新增只在后续章节出场的人，不得复述与身份无关的剧情。

只输出 JSON：
{{"characters": [{{"source_label": "当前称谓", "canonical_name": "稳定真名", "identity_kind": "named", "kind": "onscreen|mentioned", "evidence": "本集身份依据", "future_evidence": "同一性依据"}}]}}"""
        future_raw = await model_gateway.chat(
            [{"role": "user", "content": future_prompt}],
            temperature=0.1,
            max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
            call_meta={
                "stage": "discover_character_candidates",
                "episode_no": episode_no,
                "discovery_phase": "future_identity",
                "reuse_successful_operation": True,
            },
        )
        collect(
            future_raw,
            identity_haystack=f"{current_haystack}\n{future_context}",
            group_scope="future",
        )
        if len(current_identity_audit_source) >= 1000:
            coverage_prompt = f"""任务：独立审计当前集人物身份覆盖，只找第一遍遗漏或错误降级的实体。

当前人物谱已有角色：
{known}

当前集原文：
{current_identity_audit_source}

前两遍候选：
{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}

后续姓名证据：
{future_context}

规则：
1. 逐段核对每个独立行动、开口或具有可区分外观的实体；集合称谓不能替代其中的独立人物。
2. 只输出遗漏实体，或已有候选中能由后续证据唯一升级为 named identity 的实体。
3. source_label 必须逐字来自当前集原文。canonical_name 必须有当前原文、人物谱或后续窗口证据。
4. 不得按职业、年龄、服饰、称号词表判断人物是否重要；无法唯一确认就不输出。

只输出 JSON：
{{"characters": [{{"source_label": "当前原文逐字称谓", "canonical_name": "稳定真名或专属称号", "identity_kind": "named|functional", "identity_group": "若与已有候选同一实体则精确复用其 identity_group，否则空串", "functional_identity_key": "同一实体分组或空串", "kind": "onscreen|mentioned", "evidence": "当前依据", "future_evidence": "后续同一性依据"}}]}}"""
            coverage_raw = await model_gateway.chat(
                [{"role": "user", "content": coverage_prompt}],
                temperature=0.05,
                max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
                call_meta={
                    "stage": "discover_character_candidates",
                    "episode_no": episode_no,
                    "discovery_phase": "coverage_audit",
                    "reuse_successful_operation": True,
                },
            )
            collect(
                coverage_raw,
                identity_haystack=f"{current_haystack}\n{future_context}",
                group_scope="coverage",
            )

    # 同一称谓在不同后文批次中可能先被保守判为 functional，后被真名证据命中。
    # 具名证据唯一时优先；出现两个不同真名时不猜，降级为一次性角色。
    def strongest_occurrence(options: list[dict]) -> dict:
        """Preserve an onscreen owned receipt independent of batch order."""
        signatures = {
            _current_identity_durable_signature(item) for item in options
        }
        if len(signatures) == 1 and not any(
            item.get("source_label_provenance")
            == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            for item in options
        ) and all(
            isinstance(item.get("source_evidence_receipt"), dict)
            and isinstance(item.get("source_evidence_receipts"), list)
            and bool(item.get("source_evidence_receipts"))
            for item in options
        ):
            return _merge_current_identity_occurrences(options)
        return next(
            (item for item in options if item.get("kind") == "onscreen"),
            options[0],
        )

    resolved: list[dict] = []
    # 第31轮 ERR-20260824-614276 RCA 追加发现：这一步是跨 collect() 批次
    # （current/future/coverage）的最终折叠，一直只按裸 source_label 分组
    # ——round-20/round-31 在 _project_current_identity_response /
    # _current_identity_projection_errors 两处都已经升级成 (source_label,
    # scope_qualifier) 复合键，唯独这里从未跟进：两个通过了前两道复合键
    # 校验的不同人（比如本轮"老者"F3/F4 补足后各自拿到的 甲/乙 限定语），
    # 走到这里又会被裸 source_label 重新拍扁成一条（strongest_occurrence
    # 内部按 durable signature 判等，签名不等就直接"挑一个 onscreen 的"
    # 静默丢弃另一个）——同一个漏洞类型的第三处变体，一并按同一把复合键
    # 尺子修正。
    for group_key in dict.fromkeys(
        (item["source_label"], str(item.get("scope_qualifier") or "").strip())
        for item in candidates
    ):
        source_label, qualifier = group_key
        options = [
            item for item in candidates
            if item["source_label"] == source_label
            and str(item.get("scope_qualifier") or "").strip() == qualifier
        ]
        named_options_by_name: dict[str, list[dict]] = {}
        for item in options:
            if item["identity_kind"] == "named":
                named_options_by_name.setdefault(item["name"], []).append(item)
        named_by_name = {
            name: strongest_occurrence(named_options)
            for name, named_options in named_options_by_name.items()
        }
        if len(named_by_name) == 1:
            resolved.append(next(iter(named_by_name.values())))
        elif len(named_by_name) > 1:
            functional_options = [
                item for item in options
                if item["identity_kind"] == "functional"
            ]
            functional = (
                strongest_occurrence(functional_options)
                if functional_options else None
            )
            resolved.append(functional or {
                "name": source_label,
                "source_label": source_label,
                "identity_kind": "functional",
                "identity_group": f"conflict:{source_label}",
                "existing_route_name": "",
                "kind": "onscreen",
                "evidence": "多批次身份线索冲突，不猜真名",
                "future_evidence": "",
            })
        else:
            resolved.append(strongest_occurrence(options))

    named_by_group: dict[str, set[str]] = {}
    named_evidence: dict[tuple[str, str], dict] = {}
    for item in resolved:
        if item.get("identity_kind") != "named":
            continue
        group = str(item.get("identity_group") or "").strip()
        name = str(item.get("name") or "").strip()
        if group and name:
            named_by_group.setdefault(group, set()).add(name)
            named_evidence[(group, name)] = item
    upgraded: list[dict] = []
    for item in resolved:
        group = str(item.get("identity_group") or "").strip()
        names = named_by_group.get(group, set())
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and len(names) == 1
        ):
            canonical_name = next(iter(names))
            evidence = named_evidence[(group, canonical_name)]
            upgraded.append({
                **item,
                "name": canonical_name,
                "identity_kind": "named",
                "future_evidence": str(
                    evidence.get("future_evidence") or ""
                ),
            })
        else:
            upgraded.append(item)
    resolved = upgraded

    # 按本集第一次出现排序，保证后续“路人甲/乙/丙/丁”分配不受模型输出顺序影响。
    return sorted(
        resolved,
        key=lambda item: current_haystack.find(item["source_label"]),
    )


class CurrentKnownIdentityDecision(BaseModel):
    """Select one backend-owned registered authority for one evidence ref."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=96)
    kind: Literal["onscreen", "mentioned"]
    # 同批折叠通道（RCA ERR-20260824-bc3d14，见
    # docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §2.7/§4.2 "同批折叠通道"）：
    # 模型借此声明本 K 决议吸收了哪些仍处于 functional 状态的称谓组——本批
    # f 项自己的 functional_identity_key、前批 P token（prior functional
    # 分组 decision_id）或本集已有功能身份决议的 canonical_name。不做 enum
    # 约束：批内 F1/F2 这类 token 由模型在同一响应里现造，构建 schema 时
    # 还不存在，无法预先枚举（跟 functional_identity_key 本身不能 enum
    # 约束是同一个理由，见 CurrentFunctionalIdentityDecision.functional_
    # identity_key）。代码侧只做集合成员关系核验（不做文本语义判断，见
    # _project_current_identity_response），伪造或越界的 token 会被拒绝，
    # 不静默接受——安全默认。
    absorbed_functional_keys: list[str] = Field(
        default_factory=list, max_length=16
    )

    @field_validator("absorbed_functional_keys")
    @classmethod
    def _absorbed_functional_keys_defensive_shape(
        cls, value: list[str]
    ) -> list[str]:
        cleaned = [str(item or "").strip() for item in value]
        if any(not item or len(item) > 96 for item in cleaned):
            raise ValueError(
                "absorbed_functional_keys 含空值或超长 token"
            )
        return cleaned


class CurrentNewNamedIdentityDecision(BaseModel):
    """Declare one literal current-source name without a free canonical field.

    ``name_kind`` is the identity-form rank (真名 > 尊称 > 代称).  Only a
    personal name may become a new authority; the backend deterministically
    demotes the other two forms to a functional identity.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    identity_label: str = Field(min_length=1, max_length=16)
    name_kind: Literal["personal_name", "honorific", "referential"]
    kind: Literal["onscreen", "mentioned"]


class CurrentFunctionalIdentityDecision(BaseModel):
    """Declare one unresolved current-source identity within owned evidence."""

    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    source_label: str = Field(
        min_length=1, max_length=IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH
    )
    functional_identity_key: str = Field(min_length=1, max_length=64)
    kind: Literal["onscreen", "mentioned"]
    # 真实第18轮 EP10 回归 ERR-20260824-b16bb4：结构性方案 a（唯一性判定键
    # 改为 (source_label, scope_qualifier) 复合键，见 prompt 规则8与
    # _project_current_identity_response 的 by_label 分组注释）。默认空串，
    # 不影响任何不需要区分的既有场景——模型只在自己判断同一 source_label
    # 这次可能指向跟之前不同的人时才需要填写。max_length=64 是纯防御性
    # 上限，拦的是整段抄录级失控值，不是语义约束——真实限定语（"县城木匠
    # 铺王伯，王有材的父亲"）可以带逗号顿号，见 field_validator 的说明。
    scope_qualifier: str = Field(default="", max_length=64)

    @field_validator("source_label")
    @classmethod
    def _source_label_forbids_identity_list_separators(cls, value: str) -> str:
        # 约束维度是分隔符标点，不是长度：max_length 只是防御性上限（见常量旁
        # 说明）。生产事故 EP7 的 source_label 是因为混入全角逗号才必须被拒，
        # 一个更短但带逗号的标签同样危险，一个更长但不带分隔符的标签反而无害。
        if _identity_source_label_has_list_separator(value):
            raise ValueError(
                "source_label 不得包含身份列表分隔符或空白"
                "（、，,／/；;｜|＆&＋+ 及空白）：下游会按这些字符切分身份列表"
            )
        return value

    @field_validator("scope_qualifier")
    @classmethod
    def _scope_qualifier_strip_only(cls, value: str) -> str:
        # 真实第19轮 EP1 回归：分隔符禁令是从 source_label 的校验直接抄过来
        # 的，但两者的下游数据流不一样——source_label 会被写进"身份列表"
        # 拼接字符串（台词发言人、场次角色表等），下游按分隔符切分，所以那
        # 条字段必须禁分隔符；scope_qualifier 只作为
        # _project_current_identity_response 的 by_label 分组键的第二个元素
        # （Python 元组 (source_label, scope_qualifier)，从未做过字符串拼接
        # 或按分隔符切分——见该函数的 by_label 构造），禁令在这里没有对应的
        # 下游风险，纯属误套（跟当年 source_label max_length=16 误伤自然语言
        # 值是同一类错误：约束跟着字段名走，没跟着字段的实际数据流走）。这里
        # 只做去首尾空白；长度上限（max_length=64，见字段定义）是唯一保留的
        # 防御性约束，拦的是"整段抄录级"失控值（模型把大段原文当限定语粘贴
        # 进来），不是语义约束，不限制标点或分隔符——"县城木匠铺王伯，王有材
        # 的父亲"这类带逗号的自然限定语必须放行。
        return str(value or "").strip()


class CurrentIdentityCandidateResponse(BaseModel):
    """Global closed RF11 K/N/F wire for current-source discovery."""

    model_config = ConfigDict(extra="forbid")

    k: list[CurrentKnownIdentityDecision]
    n: list[CurrentNewNamedIdentityDecision]
    f: list[CurrentFunctionalIdentityDecision]


class FutureIdentityCandidateResponse(BaseModel):
    """Exact group-keyed wire for bounded future identity resolution.

    The three maps are dynamically closed over backend-owned group keys.  A
    decision token either names one catalog entry, or selects the NEW sentinel;
    the sidecars are empty for every non-NEW decision.  This keeps the provider
    schema inside the proven strict subset without relying on anyOf/oneOf.
    """

    model_config = ConfigDict(extra="forbid")

    decisions: dict[str, str]
    revealed_names: dict[str, str]
    reveal_evidence_ids: dict[str, str]
    revealed_name_kinds: dict[str, str]


class StructuralIdentityCoverageResponse(BaseModel):
    """Exact keyed wire for the post-Blueprint identity coverage audit.

    Every value is an opaque backend-owned decision token.  Labels, groups,
    authorities and evidence never travel as independently mixable fields.
    """

    model_config = ConfigDict(extra="forbid")

    decisions: dict[str, str]


_IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS = frozenset({
    "$defs",
    "$ref",
    "additionalProperties",
    "enum",
    "items",
    "properties",
    "required",
    "type",
})


def _identity_source_label_schema(
    model_type: type[BaseModel],
    source_labels: list[str],
    *,
    candidate_defs: tuple[str, ...],
    branches: tuple[str, ...] = ("named", "functional"),
) -> dict:
    """Bind both branches of a split identity wire to one allowed label set."""
    known_labels = list(dict.fromkeys(
        str(value or "").strip() for value in source_labels
        if str(value or "").strip()
    ))
    if not known_labels:
        raise ValueError(
            "identity schema requires source labels"
        )
    schema = model_type.model_json_schema()
    for definition_name in candidate_defs:
        candidate_schema = schema["$defs"][definition_name]
        candidate_schema["properties"]["source_label"]["enum"] = known_labels
    for branch in branches:
        schema["properties"][branch]["maxItems"] = len(known_labels)
    return schema


def _current_identity_schema(
    evidence_refs: list[str],
    *,
    known_decision_ids: list[str],
) -> dict:
    """Build the global closed RF11 K/N/F schema.

    RF10 required one K/N/F object for every evidence span.  That shape made a
    model classify occurrences instead of identities and structurally invited
    the same person dozens of times.  RF11 emits only selected identities in
    three global arrays.  K remains an opaque evidence-bound backend token;
    N/F carry one explicit request-local evidence ref and are revalidated
    locally.  The shape stays inside the provider-proven strict subset.
    """
    refs = list(dict.fromkeys(
        str(value or "").strip()
        for value in evidence_refs
        if str(value or "").strip()
    ))
    if not refs:
        raise ValueError("current identity schema requires evidence refs")
    decision_ids = list(dict.fromkeys(
        str(value or "").strip()
        for value in known_decision_ids
        if str(value or "").strip()
    )) or ["K:NONE"]

    known_item = CurrentKnownIdentityDecision.model_json_schema()
    known_item["properties"]["decision_id"]["enum"] = decision_ids
    new_item = CurrentNewNamedIdentityDecision.model_json_schema()
    new_item["properties"]["evidence_ref"]["enum"] = refs
    functional_item = CurrentFunctionalIdentityDecision.model_json_schema()
    functional_item["properties"]["evidence_ref"]["enum"] = refs
    definitions = {
        "CurrentKnownIdentityDecision": known_item,
        "CurrentNewNamedIdentityDecision": new_item,
        "CurrentFunctionalIdentityDecision": functional_item,
    }
    # maxItems 会在 _identity_strict_provider_schema 投影到 provider 时被剥离
    # （不在 _IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS 白名单内，见
    # test_current_identity_rf11_schema_stays_under_strict_property_limit 同
    # 目录下对 provider_keywords 的 disjoint 断言），从不真正约束 provider 输出，
    # 真正生效的计数帽在 _project_current_identity_response 里、用同一公式
    # （_current_identity_decision_cap）计算。这里保留只为本地 schema 的自述
    # 信息不撒谎——两处用同一个函数，不会再次跟实际生效的帽子脱节。
    decision_cap = _current_identity_decision_cap(len(refs))
    return {
        "$defs": definitions,
        "type": "object",
        "properties": {
            "k": {
                "type": "array",
                "items": {"$ref": "#/$defs/CurrentKnownIdentityDecision"},
                "maxItems": decision_cap,
            },
            "n": {
                "type": "array",
                "items": {"$ref": "#/$defs/CurrentNewNamedIdentityDecision"},
                "maxItems": decision_cap,
            },
            "f": {
                "type": "array",
                "items": {"$ref": "#/$defs/CurrentFunctionalIdentityDecision"},
                "maxItems": decision_cap,
            },
        },
        "required": ["k", "n", "f"],
        "additionalProperties": False,
    }


def _future_identity_schema(
    group_keys: list[str],
    *,
    decision_ids_by_group: dict[str, list[str]],
    evidence_ids_by_group: dict[str, list[str]],
) -> dict:
    """Build three exact maps using only the provider-proven schema subset."""
    keys = list(dict.fromkeys(
        str(value or "").strip() for value in group_keys
        if str(value or "").strip()
    ))
    if not keys:
        raise ValueError("future identity schema requires group keys")

    def exact_map(properties: dict[str, dict]) -> dict:
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {
            "decisions": exact_map({
                key: {
                    "type": "string",
                    "enum": list(decision_ids_by_group[key]),
                }
                for key in keys
            }),
            "revealed_names": exact_map({
                # maxLength 是防御性上限，不是业务约束（业务约束是禁止分隔符
                # 标点，见 validate_response 里对 canonical_name 的检查）；provider
                # strict schema 会剥离 maxLength，这里保留只为向模型展示信息。
                key: {
                    "type": "string",
                    "maxLength": IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH,
                }
                for key in keys
            }),
            "reveal_evidence_ids": exact_map({
                key: {
                    "type": "string",
                    "enum": ["", *evidence_ids_by_group.get(key, [])],
                }
                for key in keys
            }),
            "revealed_name_kinds": exact_map({
                key: {
                    "type": "string",
                    "enum": ["", *IDENTITY_NAME_FORMS],
                }
                for key in keys
            }),
        },
        "required": [
            "decisions",
            "revealed_names",
            "reveal_evidence_ids",
            "revealed_name_kinds",
        ],
        "additionalProperties": False,
    }


def _structural_identity_coverage_schema(
    group_keys: list[str],
    *,
    decision_ids_by_group: dict[str, list[str]],
) -> dict:
    """Bind each coverage leader to its own opaque decision-token enum."""
    keys = list(dict.fromkeys(
        str(value or "").strip() for value in group_keys
        if str(value or "").strip()
    ))
    if not keys:
        raise ValueError("structural identity coverage requires group keys")
    if any(not decision_ids_by_group.get(key) for key in keys):
        raise ValueError("structural identity coverage requires decisions")
    decisions = {
        "type": "object",
        "properties": {
            key: {
                "type": "string",
                "enum": list(decision_ids_by_group[key]),
            }
            for key in keys
        },
        "required": keys,
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"decisions": decisions},
        "required": ["decisions"],
        "additionalProperties": False,
    }


def _identity_strict_provider_schema(
    local_schema: dict,
) -> dict:
    """Project the local identity contract to the provider-safe subset."""

    def sanitize(schema_node: dict) -> dict:
        sanitized: dict = {}
        for keyword, value in schema_node.items():
            if keyword == "const":
                sanitized["enum"] = [value]
                continue
            if keyword not in (
                _IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS
            ):
                continue
            if keyword in {"$defs", "properties"}:
                if not isinstance(value, dict):
                    raise ValueError(
                        f"identity strict schema {keyword} must be an object"
                    )
                sanitized[keyword] = {
                    name: sanitize(child_schema)
                    for name, child_schema in value.items()
                }
            elif keyword == "items":
                if not isinstance(value, dict):
                    raise ValueError(
                        "identity strict schema items must be an object"
                    )
                sanitized[keyword] = sanitize(value)
            else:
                sanitized[keyword] = value
        properties = sanitized.get("properties")
        if isinstance(properties, dict):
            if sanitized.get("additionalProperties") is not False:
                raise ValueError(
                    "identity strict object schemas must forbid extra fields"
                )
            sanitized["required"] = list(properties)
        return sanitized

    return sanitize(local_schema)


# Kept as a source-compatible alias for callers/tests which inspect the
# sanitizer directly; it now serves every strict identity-discovery substage.
_identity_coverage_strict_provider_schema = _identity_strict_provider_schema


def _identity_strict_response_format(
    local_schema: dict,
    *,
    name: str,
) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": _identity_strict_provider_schema(local_schema),
        },
    }


def _structural_identity_coverage_response_format(
    local_schema: dict,
) -> dict:
    return _identity_strict_response_format(
        local_schema,
        name="screenplay_structural_identity_coverage_v6",
    )


def _validate_current_identity_receipt_bundle(
    candidate: dict,
    *,
    source_text: str | None,
    draft_text: str | None = "",
) -> tuple[dict, list[dict], list[str]] | None:
    """Validate the complete RF11 receipt bundle, never only its primary."""
    current_receipt = candidate.get("source_evidence_receipt")
    current_receipts = candidate.get("source_evidence_receipts")
    if current_receipt is None and current_receipts is None:
        return None

    def invalid(reason: str) -> NoReturn:
        raise ContentGenerationError(
            f"current identity evidence receipt v2 无效：{reason}"
        )

    if (
        not isinstance(current_receipt, dict)
        or not isinstance(current_receipts, list)
        or not current_receipts
        or any(not isinstance(value, dict) for value in current_receipts)
    ):
        invalid("缺少完整 receipt list")
    receipts = [dict(value) for value in current_receipts]

    def seal_is_valid(value: dict) -> bool:
        if source_text is not None:
            return _current_identity_evidence_receipt_is_valid(
                value,
                source_text=source_text,
                draft_text=str(draft_text or ""),
            )
        if (
            value.get("receipt_version")
            != CURRENT_IDENTITY_EVIDENCE_RECEIPT_VERSION
            or value.get("contract_version")
            != CURRENT_IDENTITY_DECISION_VERSION
        ):
            return False
        try:
            payload = _current_identity_evidence_payload(value)
        except (TypeError, ValueError):
            return False
        return bool(
            payload["origin"] in {
                "current_source", "draft_identity_projection",
            }
            and payload["source_hash"]
            and payload["source_segment_id"]
            and payload["text"].strip()
            and payload["end_offset"] > payload["start_offset"]
            and str(value.get("evidence_id") or "")
            == "CE:" + evidence_repository.content_hash(payload)[:24]
        )

    if any(not seal_is_valid(value) for value in receipts):
        invalid("seal 或 owned source epoch 不匹配")
    evidence_ids = [
        str(value.get("evidence_id") or "").strip() for value in receipts
    ]
    if not all(evidence_ids) or len(evidence_ids) != len(set(evidence_ids)):
        invalid("evidence_id 空值或重复")
    canonical_receipts = sorted(receipts, key=_current_identity_receipt_sort_key)
    if receipts != canonical_receipts:
        invalid("receipt list 顺序不是 canonical")

    label = str(candidate.get("source_label") or "").strip()
    provenance = str(candidate.get("source_label_provenance") or "").strip()
    if provenance == CURRENT_IDENTITY_LITERAL_PROVENANCE:
        if not label or any(
            label not in str(value.get("text") or "") for value in receipts
        ):
            invalid("逐字 source_label 与 receipt 不匹配")
    elif provenance == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE:
        if len(receipts) != 1 or (
            label and label in str(receipts[0].get("text") or "")
        ):
            invalid("synthetic receipt 语义不闭合")
    else:
        invalid("source_label provenance 不允许持有 v2 receipt")

    if current_receipt != receipts[0]:
        invalid("singular primary 不是 canonical 首项")
    expected_source_ids = list(dict.fromkeys(
        str(value.get("source_segment_id") or "").strip()
        for value in receipts
        if str(value.get("source_segment_id") or "").strip()
    ))
    raw_source_ids = candidate.get("source_segment_ids")
    if (
        not isinstance(raw_source_ids, list)
        or not raw_source_ids
        or any(
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            for value in raw_source_ids
        )
        or len(raw_source_ids) != len(set(raw_source_ids))
    ):
        invalid("source_segment_ids 必须为 exact nonempty unique string list")
    actual_source_ids = list(raw_source_ids)
    if actual_source_ids != expected_source_ids:
        invalid("source_segment_ids 投影不一致")
    primary_source_id = str(current_receipt.get("source_segment_id") or "")
    if str(candidate.get("source_segment_id") or "") != primary_source_id:
        invalid("singular source_segment_id 不一致")
    return dict(current_receipt), receipts, expected_source_ids


def _attach_candidate_source_evidence(
    candidates: list[dict],
    source_text: str,
    *,
    draft_text: str = "",
) -> list[dict]:
    """Bind candidate labels to one owned SRC without guessing from vocabulary."""
    segments = index_source_segments(source_text)
    by_id = {segment.segment_id: segment for segment in segments}
    for candidate in candidates:
        typed_owned = bool(candidate.pop("_typed_source_evidence_owned", False))
        candidate.pop("_current_materialization_compatible", None)
        candidate.pop("_current_response_group_key", None)
        current_receipt = candidate.get("source_evidence_receipt")
        current_receipts = candidate.get("source_evidence_receipts")
        label = str(candidate.get("source_label") or "").strip()
        cited_id = str(candidate.get("source_segment_id") or "").strip()
        cited = by_id.get(cited_id)
        if current_receipt is not None or current_receipts is not None:
            try:
                bundle = _validate_current_identity_receipt_bundle(
                    candidate,
                    source_text=source_text,
                    draft_text=draft_text,
                )
            except ContentGenerationError:
                candidate["source_evidence_receipt"] = None
                candidate["source_evidence_receipts"] = []
                candidate["source_segment_id"] = ""
                candidate["source_segment_ids"] = []
                candidate["source_quote"] = ""
                raise
            assert bundle is not None
            current_receipt, receipts, expected_source_ids = bundle
            primary_source_id = str(current_receipt.get("source_segment_id") or "")
            candidate["source_evidence_receipt"] = dict(current_receipt)
            candidate["source_evidence_receipts"] = receipts
            candidate["source_segment_id"] = primary_source_id
            candidate["source_segment_ids"] = expected_source_ids
            candidate["source_quote"] = str(current_receipt.get("text") or "")
            continue
        if typed_owned and cited is not None:
            candidate["source_segment_id"] = cited.segment_id
            candidate["source_quote"] = str(
                candidate.get("source_quote") or cited.text
            )
            continue
        owned = (
            [cited]
            if cited is not None and label and label in cited.text
            else [segment for segment in segments if label and label in segment.text]
        )
        # A short label is accepted only when the cited source span has one
        # occurrence.  Ambiguous spans remain unresolved for structural audit.
        if len(owned) == 1 and (
            len(textmatch.condense(label)) > 3
            or owned[0].text.count(label) == 1
        ):
            candidate["source_segment_id"] = owned[0].segment_id
            model_quote = str(candidate.get("source_quote") or "").strip()
            candidate["source_quote"] = (
                model_quote
                if model_quote and model_quote in owned[0].text and label in model_quote
                else owned[0].text
            )
        else:
            candidate["source_segment_id"] = ""
            candidate["source_quote"] = ""
    return candidates


async def extract_current_identity_candidates(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    existing_resolutions: list[dict] | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Extract current-episode identities without future or coverage prompts."""
    candidates = await _discover_character_candidates_legacy(
        source_text,
        bible,
        episode_no,
        draft_text=draft_text,
        future_text="",
        existing_resolutions=existing_resolutions,
        project_id=project_id,
    )
    return _attach_candidate_source_evidence(
        candidates,
        source_text,
        draft_text=draft_text,
    )


async def resolve_future_identity_candidates(
    candidates: list[dict],
    *,
    source_text: str,
    future_text: str,
    bible: Bible,
    episode_no: int,
    future_label: str = "",
) -> list[dict]:
    """Resolve current unresolved identity groups from bounded future evidence.

    The provider never copies a label, authority, group or evidence quote on
    this wire.  It selects one backend-owned decision token per exact group
    key.  Only a genuinely new name remains open text, and that name must be
    anchored verbatim in one backend-owned raw-future evidence span.
    """
    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if item.get("identity_kind") == "functional"
        and item.get("source_label_provenance")
        != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        and item.get("kind") == "onscreen"
        and str(item.get("identity_group") or "").strip()
    }
    unresolved = [
        dict(item) for item in candidates
        if item.get("identity_kind") == "functional"
        and item.get("source_label_provenance")
        != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        and (
            item.get("kind") == "onscreen"
            or str(item.get("identity_group") or "").strip()
            in unresolved_onscreen_groups
            or str(item.get("source_label") or "").strip() in future_text
        )
    ]
    if not unresolved or not str(future_text or "").strip():
        return candidates
    known_names = [
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    ]
    authority_by_id: dict[str, dict] = {}
    for name in known_names:
        authority_by_id[f"bible:{name}"] = {
            "authority_id": f"bible:{name}",
            "canonical_name": name,
            "identity_group": "",
            "aliases": [],
            "materialization_compatible": True,
        }
    for candidate in candidates:
        if str(candidate.get("identity_kind") or "") != "named":
            continue
        canonical_name = str(candidate.get("name") or "").strip()
        if not canonical_name:
            continue
        identity_group = str(candidate.get("identity_group") or "").strip()
        # Every named candidate which can authorize a future alias must converge
        # on the same final card authority.  The resolution is persisted only
        # after ``ensure_character_card`` succeeds, so this does not claim a
        # durable Bible identity before materialization.
        authority_id = str(candidate.get("authority_id") or "").strip()
        if not authority_id:
            authority_id = _canonical_named_authority_id(canonical_name)
        candidate_materialization_compatible = bool(
            authority_id == _canonical_named_authority_id(canonical_name)
            and identity_group in {"", authority_id}
            and candidate.get("materialization_compatible", True)
        )
        authority = authority_by_id.setdefault(authority_id, {
            "authority_id": authority_id,
            "canonical_name": canonical_name,
            "identity_group": identity_group,
            "aliases": [],
            "materialization_compatible": candidate_materialization_compatible,
        })
        if authority["canonical_name"] != canonical_name:
            raise ContentGenerationError(
                f"identity authority={authority_id} 对应多个真名"
            )
        source_label = str(candidate.get("source_label") or "").strip()
        if source_label and source_label not in authority["aliases"]:
            authority["aliases"].append(source_label)
        # An authority assembled from several backend routes is safe to
        # materialize only when every origin converges on the final Bible
        # authority/group.  A Bible entry must not mask a durable alias whose
        # origin group is incompatible with that card authority.
        authority["materialization_compatible"] = bool(
            authority.get("materialization_compatible", True)
            and candidate_materialization_compatible
        )
    authority_projection = list(authority_by_id.values())

    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：这里原本按裸
    # source_label 键控（label_to_group: dict[str, str]），跟 _project_
    # current_identity_response 单批内的 (source_label, scope_qualifier)
    # 复合键判定不一致——上游合法放行的"两个外宗弟子"（不同 scope_
    # qualifier、不同 identity_group）流到这里，因为只看裸 label 又被判成
    # "同一称谓对应多个身份组"重新拦下。键升级为复合键；raw_group 的兜底
    # 生成也带上 qualifier，避免两个原本不同的人在都没有 identity_group 时
    # 被兜底成同一个 group（"label:外宗弟子"），把两个人的 candidates 揉
    # 到一起。
    raw_groups: dict[str, dict] = {}
    label_to_group: dict[tuple[str, str], str] = {}
    for candidate in unresolved:
        source_label = str(candidate.get("source_label") or "").strip()
        if not source_label:
            raise ContentGenerationError(
                "future identity candidate 缺少 source_label"
            )
        scope_qualifier = str(candidate.get("scope_qualifier") or "").strip()
        raw_group = str(candidate.get("identity_group") or "").strip()
        if not raw_group:
            raw_group = (
                f"label:{source_label}:{scope_qualifier}"
                if scope_qualifier else f"label:{source_label}"
            )
        previous_group = label_to_group.setdefault(
            (source_label, scope_qualifier), raw_group,
        )
        if previous_group != raw_group:
            raise ContentGenerationError(
                "future identity 同一称谓对应多个身份组："
                f"{source_label}"
            )
        group = raw_groups.setdefault(raw_group, {
            "identity_group": raw_group,
            "labels": [],
            "candidates": [],
        })
        if source_label not in group["labels"]:
            group["labels"].append(source_label)
        group["candidates"].append(candidate)

    group_specs: list[dict] = []
    for index, group in enumerate(raw_groups.values(), start=1):
        group_specs.append({
            **group,
            "group_key": f"G{index:03d}",
        })
    group_keys = [str(group["group_key"]) for group in group_specs]

    # Evidence IDs always resolve to an exact raw-future span.  Current-tail
    # context is shown separately for semantic handoff, but can never be cited
    # as the owned evidence which authorizes a decision.
    # Use one overlap policy across the complete raw future source.  Applying
    # overlap only inside a long balanced quotation leaves ordinary 120-char
    # segment boundaries able to split a <=16-char name.  A 32-char overlap
    # guarantees every allowed label/name is complete in at least one window.
    future_segments = [
        SourceSegment(
            segment_id=f"FUTURE:E{index + 1}",
            text=future_text[offset:offset + 120],
            start_offset=offset,
            end_offset=min(len(future_text), offset + 120),
        )
        for index, offset in enumerate(range(0, len(future_text), 88))
        if future_text[offset:offset + 120]
    ]
    evidence_by_id: dict[str, dict] = {}
    evidence_ids_by_group: dict[str, list[str]] = {}
    # 事故 RCA（EP2「绿袍男子」误并入「李富贵」）：当某个标签在整段未来文本
    # 里从未逐字出现，下面的 else 分支盲抓未来文本开头约 900 字符作为该组
    # 的证据窗口，内容与该标签毫无关系——纯属兜底，只是为了让 N: 分支（发现
    # 新真名）仍有文本可看。这样取得的窗口绝不能被当成"这就是该标签的身份
    # 证据"去背书任何 K: 决议：窗口里偶然出现的任何已登记角色别名/真名都只
    # 是巧合共现，不是该标签与那个角色同一人的证据。用这个集合记录哪些组是
    # 纯兜底取得证据，供下面铸造决议时拒绝为它们产出 K: 选项。
    fallback_evidence_group_keys: set[str] = set()
    per_group_budget = min(
        1800,
        max(
            120,
            CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET // max(1, len(group_specs)),
        ),
    )
    for group in group_specs:
        group_key = str(group["group_key"])
        group_labels = [str(value) for value in group["labels"]]
        label_source_indexes = {
            index for index, segment in enumerate(future_segments)
            if any(label in segment.text for label in group_labels)
        }
        if label_source_indexes:
            context_source_indexes = {
                neighbor
                for index in label_source_indexes
                for neighbor in (index - 1, index, index + 1)
                if 0 <= neighbor < len(future_segments)
            }
            context_source_indexes.add(len(future_segments) - 1)
            context_source_indexes.update(
                index for index, segment in enumerate(future_segments)
                if any(name in segment.text for name in known_names)
            )
            matching = [
                segment for index, segment in enumerate(future_segments)
                if index in context_source_indexes
            ]
        else:
            fallback_evidence_group_keys.add(group_key)
            matching = [
                segment for segment in future_segments
                if segment.start_offset < 900
            ]
        label_window_indexes = {
            index for index, segment in enumerate(matching)
            if any(label in segment.text for label in group_labels)
        }
        adjacent_label_window_indexes = {
            neighbor
            for index in label_window_indexes
            for neighbor in (index - 1, index + 1)
            if 0 <= neighbor < len(matching)
        }
        ranked = sorted(
            enumerate(matching),
            key=lambda item: (
                0 if item[0] in label_window_indexes else (
                    1 if item[0] in adjacent_label_window_indexes else 2
                ),
                -sum(name in item[1].text for name in known_names),
                0 if item[0] == 0 else 1,
                0 if item[0] == len(matching) - 1 else 1,
                item[1].start_offset,
            ),
        )
        selected: list = []
        used = 0
        max_windows = max(1, min(6, per_group_budget // 120))
        for _rank, segment in ranked:
            if used >= per_group_budget or len(selected) >= max_windows:
                break
            if segment.text in {item.text for item in selected}:
                continue
            if selected and used + len(segment.text) > per_group_budget:
                continue
            selected.append(segment)
            used += len(segment.text)
        selected.sort(key=lambda item: item.start_offset)
        group_evidence_ids: list[str] = []
        for segment in selected:
            evidence_id = "E:" + evidence_repository.content_hash({
                "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                "origin": "future",
                "source_hash": evidence_repository.content_hash(future_text),
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "text": segment.text,
            })[:20]
            evidence_by_id.setdefault(evidence_id, {
                "evidence_id": evidence_id,
                "origin": "future",
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "text": segment.text,
            })
            group_evidence_ids.append(evidence_id)
        evidence_ids_by_group[group_key] = list(dict.fromkeys(
            group_evidence_ids
        ))

    named_authorities_by_identity_group: dict[str, set[str]] = {}
    for candidate in candidates:
        if str(candidate.get("identity_kind") or "") != "named":
            continue
        # Same-group K is a recovery authority, not a way for two values from
        # the same provider response to certify each other.  A free functional
        # key may collide with a named group string; only an explicitly durable
        # backend decision may authorize this shortcut.
        if str(candidate.get("decision_provenance") or "").strip() not in (
            DURABLE_IDENTITY_DECISION_PROVENANCE
        ):
            continue
        identity_group = str(candidate.get("identity_group") or "").strip()
        canonical_name = str(candidate.get("name") or "").strip()
        if not identity_group or not canonical_name:
            continue
        named_authorities_by_identity_group.setdefault(
            identity_group, set()
        ).add(_canonical_named_authority_id(canonical_name))

    # An authority which already stands on this episode's stage under its own
    # name cannot be revealed by a later window that merely mentions that name:
    # that is co-occurrence ("A talked about B"), and the unresolved label is
    # then someone else.  An authority with no independent named presence here
    # has no such alternative reading, and a future window naming it is the
    # only way this episode can learn who the label is.
    episode_named_authorities = {
        str(candidate.get("authority_id") or "").strip()
        or _canonical_named_authority_id(str(candidate.get("name") or ""))
        for candidate in candidates
        if str(candidate.get("identity_kind") or "") == "named"
        and str(candidate.get("name") or "").strip()
    }

    decision_by_id: dict[str, dict] = {}
    decision_ids_by_group: dict[str, list[str]] = {}
    for group in group_specs:
        group_key = str(group["group_key"])
        functional_id = f"F:{group_key}"
        decision_by_id[functional_id] = {
            "decision_id": functional_id,
            "group_key": group_key,
            "resolution_kind": "functional",
        }
        decision_ids = [functional_id]
        # 兜底证据不得为 K 决议背书（见上面 fallback_evidence_group_keys 的
        # 注释）：这个组的证据窗口和它的标签毫无逐字关联，窗口里出现的任何
        # 已登记权威的别名/真名都只是巧合共现，不是"这个组就是那个人"的
        # 证据。可选项在这里被硬性收窄到只剩 F:（证据不足）与下面的 N:
        # （若窗口内确实首次揭示了新真名）——不做成"允许但弱置信度标注"，
        # 因为一旦选项出现在 schema 枚举里，模型就可能选中它，且后续任何
        # 环节都无法再用"这是不是兜底窗口"这条信息去否决一个已经铸造出的
        # decision_id。
        if group_key not in fallback_evidence_group_keys:
            for authority_id, authority in authority_by_id.items():
                canonical_name = str(
                    authority.get("canonical_name") or ""
                ).strip()
                # K decisions need an anchor the backend can bind to this
                # group's own evidence: a registered non-canonical alias, an
                # authority already bound to this exact current identity
                # group, or -- for an authority not otherwise present in this
                # episode -- its canonical name.  Excluding the canonical name
                # outright was the production defect: every Bible-seeded
                # authority starts with an empty alias list, so no K decision
                # was ever minted, "this group is an already-registered
                # person" became unrepresentable, and the run died on rule 5
                # instead.
                registered_aliases = [
                    str(value).strip()
                    for value in authority.get("aliases") or []
                    if str(value).strip()
                    and str(value).strip() != canonical_name
                ]
                same_group_authority = authority_id in (
                    named_authorities_by_identity_group.get(
                        str(group.get("identity_group") or ""), set()
                    )
                )
                if same_group_authority:
                    proof_anchors = [
                        str(value) for value in group.get("labels") or []
                    ]
                    proof_kind = "same_group_authority"
                else:
                    canonical_anchor = (
                        [canonical_name]
                        if canonical_name
                        and authority_id not in episode_named_authorities
                        else []
                    )
                    proof_anchors = list(dict.fromkeys([
                        *registered_aliases,
                        *canonical_anchor,
                    ]))
                    proof_kind = (
                        "registered_alias" if registered_aliases
                        else "canonical_name"
                    )
                if not proof_anchors:
                    continue
                anchored_evidence_ids = [
                    evidence_id
                    for evidence_id in evidence_ids_by_group[group_key]
                    if any(
                        anchor
                        and anchor in str(
                            evidence_by_id.get(evidence_id, {}).get("text")
                            or ""
                        )
                        for anchor in proof_anchors
                    )
                ]
                if not anchored_evidence_ids:
                    continue
                known_hash = evidence_repository.content_hash({
                    "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
                    "group_key": group_key,
                    "authority_id": authority_id,
                    "evidence_ids": anchored_evidence_ids,
                })[:12]
                known_id = f"K:{group_key}:{authority_id}:{known_hash}"
                decision_by_id[known_id] = {
                    "decision_id": known_id,
                    "group_key": group_key,
                    "resolution_kind": "known_named",
                    "authority_id": authority_id,
                    "canonical_name": canonical_name,
                    "evidence_ids": anchored_evidence_ids,
                    "materialization_compatible": bool(
                        authority.get("materialization_compatible")
                    ),
                    "proof_kind": proof_kind,
                    "proof_anchors": proof_anchors,
                }
                decision_ids.append(known_id)
        if evidence_ids_by_group[group_key]:
            new_id = f"N:{group_key}"
            decision_by_id[new_id] = {
                "decision_id": new_id,
                "group_key": group_key,
                "resolution_kind": "new_named",
            }
            decision_ids.append(new_id)
        decision_ids_by_group[group_key] = decision_ids

    identity_schema = _future_identity_schema(
        group_keys,
        decision_ids_by_group=decision_ids_by_group,
        evidence_ids_by_group=evidence_ids_by_group,
    )
    identity_response_format = _identity_strict_response_format(
        identity_schema,
        name="screenplay_future_identity_resolution_v10",
    )
    group_projection = [
        {
            "group_key": group["group_key"],
            "identity_group": group["identity_group"],
            "source_labels": group["labels"],
        }
        for group in group_specs
    ]
    decision_projection = [
        decision for decision in decision_by_id.values()
    ]
    evidence_projection = [
        evidence for evidence in evidence_by_id.values()
    ]
    current_boundary = str(source_text or "").strip()[-700:]
    prompt = f"""任务：只为当前集尚未确认的身份组做后续姓名消歧。
未决身份组（group_key 是唯一可输出的键）：
{json.dumps(group_projection, ensure_ascii=False, separators=(',', ':'))}
已有人物权威目录（只读）：
{json.dumps(authority_projection, ensure_ascii=False, separators=(',', ':'))}
后续证据目录（{future_label or '后续章节'}；evidence_id 对应未改写的原文连续片段）：
{json.dumps(evidence_projection, ensure_ascii=False, separators=(',', ':'))}
可选决议目录（已将 group/authority/evidence 组合绑定为不透明 decision_id）：
{json.dumps(decision_projection, ensure_ascii=False, separators=(',', ':'))}
当前章末交接上下文（仅供推理，绝不是可选 evidence_id）：
{current_boundary or '（无）'}
规则：
1. decisions/revealed_names/reveal_evidence_ids/revealed_name_kinds 四个对象都必须精确输出全部
   group_key，不得增删键。
2. 证据不足时选 F: 决议，这是合法终态；此时两个侧载字段都必须是空字符串。
3. 证据显示该组就是「已有人物权威目录」中的人时，只能选对应那名 authority 的 K: 决议；
   该 token 已绑定 authority 与原文证据，两个侧载字段都必须为空；
   若目录里没有为该组与该 authority 列出 K: 决议，则只能选 F: 决议。
4. 只有证据目录首次逐字揭示了不在已有权威目录中的稳定真名，才能选 N: 决议；
   revealed_names 写真名，reveal_evidence_ids 选包含该真名的 evidence_id，
   revealed_name_kinds 写 personal_name。
   {IDENTITY_NAME_FORM_RULE}
   「某师姐」「某爷」「某掌柜」这类姓氏或关系加称呼是 honorific，不是真名：
   这种情况选 F: 决议，四个对象里除 decisions 外都写空字符串。
   非 N: 决议的组，revealed_names/reveal_evidence_ids/revealed_name_kinds 三项都必须是空字符串。
5. 不得回抄或改写证据文本，不得为已有权威重新签发新名，不得输出只在后续出场的人。
只输出符合下列 Schema 的 JSON：
{json.dumps(identity_schema, ensure_ascii=False, separators=(',', ':'))}"""

    def normalize_identity_payload(payload: dict) -> dict:
        """Route a NEW answer to the decision its own evidence supports.

        Two deterministic rewrites, both before validation:

        * a NEW whose declared form is not a personal name is demoted to this
          group's functional decision -- 真名 > 尊称 > 代称, and only a real
          name may mint a new authority; and

        * a NEW that actually names an already-registered person is rewritten
          onto that person's own backend decision.

        The provider sometimes expresses "this group is that already-registered
        person" with the N token plus that person's existing canonical name.
        When the backend has itself already minted a K decision for the exact
        same (group, authority, evidence) tuple, the answer carries every fact
        the K decision requires and differs only in which token was written --
        so it is canonicalised onto the backend's own token instead of failing
        the episode.  Without a matching backend decision nothing is rewritten
        and the NEW rule stays fail-closed: this can never bind an authority
        the backend has not already anchored in this group's own evidence.
        """
        if not isinstance(payload, dict):
            return payload
        decisions = payload.get("decisions")
        revealed_names = payload.get("revealed_names")
        reveal_evidence_ids = payload.get("reveal_evidence_ids")
        if not (
            isinstance(decisions, dict)
            and isinstance(revealed_names, dict)
            and isinstance(reveal_evidence_ids, dict)
        ):
            return payload
        name_kinds = payload.get("revealed_name_kinds")
        if not isinstance(name_kinds, dict):
            name_kinds = {}
        rewritten: dict[str, str] = {}
        for group_key in group_keys:
            selected = decision_by_id.get(str(decisions.get(group_key) or ""))
            if (
                selected is None
                or str(selected.get("resolution_kind") or "") != "new_named"
            ):
                continue
            if str(
                name_kinds.get(group_key) or ""
            ) != IDENTITY_NAME_FORM_PERSONAL:
                # 真名 > 尊称 > 代称：只有真名可以签发新的人物权威。尊称与代称
                # （以及没有明确声明形态的情况）确定性降级为功能身份，本组仍然是
                # 一个独立身份，等真名出现在证据里再由 K 决议认领同一个人。
                functional_id = f"F:{group_key}"
                if functional_id in decision_by_id:
                    rewritten[group_key] = functional_id
                continue
            canonical_name = str(
                revealed_names.get(group_key) or ""
            ).strip()
            evidence_id = str(
                reveal_evidence_ids.get(group_key) or ""
            ).strip()
            if not canonical_name or not evidence_id:
                continue
            # An ambiguous name matches no single authority: fail closed.
            authority_ids = [
                authority_id
                for authority_id, authority in authority_by_id.items()
                if str(
                    authority.get("canonical_name") or ""
                ).strip() == canonical_name
            ]
            if len(authority_ids) != 1:
                continue
            known = next(
                (
                    decision for decision in decision_by_id.values()
                    if str(decision.get("group_key") or "") == group_key
                    and str(
                        decision.get("resolution_kind") or ""
                    ) == "known_named"
                    and str(
                        decision.get("authority_id") or ""
                    ) == authority_ids[0]
                    and evidence_id in (decision.get("evidence_ids") or [])
                ),
                None,
            )
            if known is not None:
                rewritten[group_key] = str(known["decision_id"])
                continue
            # 归一规则（真实第26轮 EP5 回归 ERR-20260824-88ece5）：门禁立意
            # （防重复铸造身份）本身没错，错的是对"多报"的响应形态。
            # authority_ids 唯一命中只证明"这个真名字符串对应项目里唯一
            # 一个已有身份"，还不足以证明"这个 group 真的是那个人"——两个
            # 回归夹具（"三哥"被声称是"陈三"、"小胖子"被声称是"李富贵"，
            # 但各自的 future_text 里那个真名压根不存在，是纯粹的臆断/
            # 嫁接昵称）证明 authority_ids 唯一命中单独作为归一条件太松，
            # 必须补一条真正的"确定性一致性比对"：真名整体是否至少在这个
            # 组能看到的完整 future_text 里逐字出现过——不要求命中后端
            # 预先按 proof_anchors 筛出的那个更窄的 evidence 子集（那正是
            # 要豁免的负担：已知身份的锚点在它初次签发时已经验过，不需要
            # 精确复现是哪一条 evidence_id 命中的），只要求这个名字本身
            # 真的出现在模型能看到的原文里，不是凭一个昵称/称谓单方面嫁接。
            # 按门禁不对称教义（缺失致命、冗余归一、矛盾致命）：
            #   - authority_ids 唯一命中 + 真名整体逐字出现在 future_text
            #     = 冗余，归一为对既有身份的引用（见 REISSUE_KNOWN_
            #     RESOLUTION_KIND 分支在 validate_response/response_
            #     decisions 里的处理）；
            #   - authority_ids 命中多个（len!=1，上面已经 continue 掉）
            #     = 矛盾，同一个真名字符串被项目内不同的 authority_id
            #     分别持有，无法确定性判断该并入哪一个；
            #   - authority_ids 唯一命中但真名整体不在 future_text 里
            #     = 同样按矛盾/不相容处理，维持原始 NEW 校验路径不动
            #     （不重写，交给下面 validate_response 的既有 NEW 规则
            #     去拦——它本就会因为"不得重新签发已有 authority"报错）。
            if canonical_name not in future_text:
                continue
            reissue_id = f"{REISSUE_KNOWN_RESOLUTION_KIND}:{group_key}:{authority_ids[0]}"
            if reissue_id not in decision_by_id:
                reissue_authority = authority_by_id.get(authority_ids[0], {})
                decision_by_id[reissue_id] = {
                    "decision_id": reissue_id,
                    "group_key": group_key,
                    "resolution_kind": REISSUE_KNOWN_RESOLUTION_KIND,
                    "authority_id": authority_ids[0],
                    "canonical_name": canonical_name,
                    "materialization_compatible": bool(
                        reissue_authority.get("materialization_compatible")
                    ),
                }
            rewritten[group_key] = reissue_id
        if not rewritten:
            return payload
        return {
            **payload,
            "decisions": {**decisions, **rewritten},
            "revealed_names": {
                **revealed_names,
                **{group_key: "" for group_key in rewritten},
            },
            "reveal_evidence_ids": {
                **reveal_evidence_ids,
                **{group_key: "" for group_key in rewritten},
            },
            "revealed_name_kinds": {
                **name_kinds,
                **{group_key: "" for group_key in rewritten},
            },
        }

    def response_decisions(
        value: FutureIdentityCandidateResponse,
    ) -> tuple[list[dict], dict[str, dict]]:
        # resolved_by_group（真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性
        # 排查命中）：identity_group 是这条流水线里唯一真正可靠的按人区分的
        # 键——一个 group 可能因为模型判定"这些称谓是同一个人"而含多个不同
        # 的 source_label（见下面 group["labels"]），也可能两个不同的人
        # 恰好共享同一个裸 source_label 字符串（"外宗弟子"甲/乙，各自不同
        # identity_group）。旧代码只留了 decisions（按 source_label 展开的
        # 扁平列表），下游再用 dict 推导式按裸 source_label 二次索引
        # （resolved_by_label/candidate_by_label）——两个人共享同一个裸标签
        # 时，字典推导式静默用后者覆盖前者，两个人的解析结果被合并成一个。
        # 直接在这里按 identity_group（拼接的真正唯一键）建一份可靠映射，
        # 调用方不再需要经过裸标签这一跳。
        decisions: list[dict] = []
        resolved_by_group: dict[str, dict] = {}
        for group in group_specs:
            group_key = str(group["group_key"])
            selected_id = str(value.decisions.get(group_key) or "")
            selected = decision_by_id.get(selected_id, {})
            resolution_kind = str(selected.get("resolution_kind") or "")
            if resolution_kind == "known_named":
                anchors = [
                    str(value)
                    for value in selected.get("proof_anchors") or []
                    if str(value)
                ]
                evidence_options = [
                    evidence_by_id.get(str(evidence_id), {})
                    for evidence_id in selected.get("evidence_ids") or []
                ]
                evidence = next(
                    (
                        item for item in evidence_options
                        if any(
                            anchor and anchor in str(item.get("text") or "")
                            for anchor in anchors
                        )
                    ),
                    {},
                )
                bounded_evidence = _bounded_owned_identity_evidence(
                    str(evidence.get("text") or ""),
                    anchors=anchors,
                )
                common = {
                    "resolution_kind": resolution_kind,
                    "identity_kind": "named",
                    "canonical_name": str(
                        selected.get("canonical_name") or ""
                    ),
                    "authority_id": str(
                        selected.get("authority_id") or ""
                    ),
                    "materialization_compatible": bool(
                        selected.get("materialization_compatible")
                    ),
                    "future_evidence": bounded_evidence,
                }
            elif resolution_kind == REISSUE_KNOWN_RESOLUTION_KIND:
                # 归一分支（第26轮，见 normalize_identity_payload 上方完整
                # 说明）：这个 group 被确定性判定为对一个已有 authority 的
                # 冗余重复声明，不是新身份——future_evidence 留空，不假装
                # 有一条这次才核验出的逐字锚点（已知身份的锚点在它初次
                # 签发时已经验过，这里不重新造一份）。
                common = {
                    "resolution_kind": resolution_kind,
                    "identity_kind": "named",
                    "canonical_name": str(
                        selected.get("canonical_name") or ""
                    ),
                    "authority_id": str(
                        selected.get("authority_id") or ""
                    ),
                    "materialization_compatible": bool(
                        selected.get("materialization_compatible")
                    ),
                    "future_evidence": "",
                }
            elif resolution_kind == "new_named":
                canonical_name = str(
                    value.revealed_names.get(group_key) or ""
                )
                evidence = evidence_by_id.get(
                    str(value.reveal_evidence_ids.get(group_key) or ""),
                    {},
                )
                bounded_evidence = _bounded_owned_identity_evidence(
                    str(evidence.get("text") or ""),
                    anchors=[canonical_name],
                )
                common = {
                    "resolution_kind": resolution_kind,
                    "identity_kind": "named",
                    "canonical_name": canonical_name,
                    "authority_id": (
                        _canonical_named_authority_id(canonical_name)
                        if canonical_name.strip() else ""
                    ),
                    "materialization_compatible": True,
                    "future_evidence": bounded_evidence,
                }
            else:
                common = {
                    "resolution_kind": "functional",
                    "identity_kind": "functional",
                    "canonical_name": "",
                    "authority_id": "",
                    "materialization_compatible": False,
                    "future_evidence": "",
                }
            resolved_by_group[str(group["identity_group"])] = common
            for source_label in group["labels"]:
                decisions.append({
                    **common,
                    "source_label": str(source_label),
                })
        return decisions, resolved_by_group

    def validate_response(
        value: FutureIdentityCandidateResponse,
    ) -> list[str]:
        errors: list[str] = []
        expected_keys = set(group_keys)
        maps = {
            "decisions": value.decisions,
            "revealed_names": value.revealed_names,
            "reveal_evidence_ids": value.reveal_evidence_ids,
            "revealed_name_kinds": value.revealed_name_kinds,
        }
        for field_name, values in maps.items():
            actual_keys = set(values)
            if actual_keys != expected_keys:
                errors.append(
                    f"future identity {field_name} keys 不闭合"
                )
        existing_identity_names = {
            name
            for authority in authority_by_id.values()
            for name in (
                str(authority.get("canonical_name") or ""),
                *[
                    str(value)
                    for value in authority.get("aliases") or []
                ],
            )
            if name
        }
        for group_key in group_keys:
            selected_id = str(value.decisions.get(group_key) or "")
            selected = decision_by_id.get(selected_id)
            if (
                selected is None
                or str(selected.get("group_key") or "") != group_key
            ):
                errors.append(
                    f"future identity decision_id 越界：{group_key}"
                )
                continue
            canonical_name = str(
                value.revealed_names.get(group_key) or ""
            )
            evidence_id = str(
                value.reveal_evidence_ids.get(group_key) or ""
            )
            resolution_kind = str(selected.get("resolution_kind") or "")
            declared_form = str(
                value.revealed_name_kinds.get(group_key) or ""
            )
            if resolution_kind != "new_named":
                if canonical_name or evidence_id or declared_form:
                    errors.append(
                        "future identity 非 NEW 决议侧载必须为空："
                        f"{group_key}"
                    )
                if resolution_kind == "known_named":
                    authority = authority_by_id.get(
                        str(selected.get("authority_id") or "")
                    )
                    selected_evidence_ids = [
                        str(value)
                        for value in selected.get("evidence_ids") or []
                    ]
                    proof_kind = str(selected.get("proof_kind") or "")
                    proof_anchors = [
                        str(value)
                        for value in selected.get("proof_anchors") or []
                        if str(value)
                    ]
                    same_group_authority = str(
                        selected.get("authority_id") or ""
                    ) in named_authorities_by_identity_group.get(
                        str(
                            next(
                                (
                                    group.get("identity_group")
                                    for group in group_specs
                                    if group.get("group_key") == group_key
                                ),
                                "",
                            )
                        ),
                        set(),
                    )
                    if (
                        authority is None
                        or proof_kind not in {
                            "registered_alias",
                            "canonical_name",
                            "same_group_authority",
                        }
                        or (
                            proof_kind == "same_group_authority"
                            and not same_group_authority
                        )
                        or not proof_anchors
                        or not selected_evidence_ids
                        or any(
                            value not in evidence_ids_by_group.get(
                                group_key, []
                            )
                            for value in selected_evidence_ids
                        )
                        or not any(
                            anchor
                            and anchor in str(
                                evidence_by_id.get(value, {}).get("text")
                                or ""
                            )
                            for value in selected_evidence_ids
                            for anchor in proof_anchors
                        )
                    ):
                        errors.append(
                            "future identity known 缺少后端登记的权威锚点："
                            f"{group_key}"
                        )
                elif resolution_kind == REISSUE_KNOWN_RESOLUTION_KIND:
                    # 归一分支（第26轮 ERR-20260824-88ece5，见
                    # normalize_identity_payload 上方完整说明）：已知身份
                    # 不需要重新锚定真名——锚点在它初次签发时已经验过。
                    # 只做最基本的健全性检查：authority_id 必须真的存在于
                    # 权威目录（防止归一逻辑自身出 bug 生造一个不存在的
                    # authority_id），不重新要求 proof_anchors/evidence_id
                    # 命中——那正是这条归一规则要豁免的负担。
                    if str(selected.get("authority_id") or "") not in authority_by_id:
                        errors.append(
                            "future identity reissue 指向不存在的 authority："
                            f"{group_key}"
                        )
                continue
            if canonical_name != canonical_name.strip() or not canonical_name:
                errors.append(
                    f"future identity NEW 真名无效：{group_key}"
                )
            if len(canonical_name) > IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH:
                errors.append(
                    f"future identity NEW 真名过长：{group_key}"
                )
            if _identity_source_label_has_list_separator(canonical_name):
                errors.append(
                    f"future identity NEW 真名不得包含身份列表分隔符：{group_key}"
                )
            if declared_form != IDENTITY_NAME_FORM_PERSONAL:
                errors.append(
                    "future identity NEW 只能签发真名（真名 > 尊称 > 代称）："
                    f"{group_key}"
                )
            if canonical_name in existing_identity_names:
                errors.append(
                    "future identity NEW 不得重新签发已有 authority："
                    f"{group_key}"
                )
            if evidence_id not in evidence_ids_by_group.get(group_key, []):
                errors.append(
                    f"future identity NEW evidence_id 越界：{group_key}"
                )
                continue
            evidence = evidence_by_id.get(evidence_id, {})
            evidence_text = str(evidence.get("text") or "")
            if (
                evidence.get("origin") != "future"
                or evidence_text not in future_text
                or canonical_name not in evidence_text
            ):
                errors.append(
                    f"future identity NEW 缺少逐字真名锚点：{group_key}"
                )
        return errors
    identity_provider, identity_model, identity_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    identity_semantic_settings = hiagent.text_request_semantic_settings(
        identity_provider
    )
    operation_id = (
        "screenplay.identity.future.v11:"
        + evidence_repository.content_hash({
            "episode_no": episode_no,
            "provider": identity_provider,
            "model": identity_model,
            "requested_max_tokens": 4096,
            "effective_max_tokens": identity_effective_max,
            "temperature": 0.1,
            "provider_semantic_settings": identity_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
            "messages": [{"role": "user", "content": prompt}],
            "output_schema": identity_schema,
            "response_format": identity_response_format,
            "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
        })
    )

    response = await _identity_structured_with_resample(
        [{"role": "user", "content": prompt}],
        model_type=FutureIdentityCandidateResponse,
        validate=validate_response,
        operation_id_for_attempt=lambda resample_attempt: (
            operation_id
            if not resample_attempt
            else f"{operation_id}:resample:{resample_attempt}"
        ),
        max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        temperature=0.1,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta={
            "contract_version": FUTURE_IDENTITY_DECISION_VERSION,
            "provider": identity_provider,
            "model": identity_model,
            "effective_max_tokens": identity_effective_max,
            "provider_semantic_settings": identity_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
            "stage": "discover_character_candidates",
            "stage_key": "screenplay_character_discovery",
            "substage": "future_identity",
            "discovery_phase": "future_identity",
            "episode_no": episode_no,
            "reuse_successful_operation": False,
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
            "disable_reasoning_fallback": True,
            "schema_hash": evidence_repository.content_hash(identity_schema),
            "decision_catalog_hash": evidence_repository.content_hash(
                decision_projection
            ),
            "evidence_catalog_hash": evidence_repository.content_hash(
                evidence_projection
            ),
        },
        repair_context=json.dumps(
            {
                "groups": group_projection,
                "decisions": decision_projection,
                "evidence": evidence_projection,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        output_schema=identity_schema,
        response_format=identity_response_format,
        require_response_format=True,
        normalize_payload=normalize_identity_payload,
    )

    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：resolved_by_group
    # 直接按 identity_group（真正唯一键，见 response_decisions 上方注释）
    # 取解析结果，不再经过裸 source_label 的两跳字典推导式（原设计里两个
    # 不同的人共享同一个裸标签时，Python 字典推导式会静默用后者覆盖前者，
    # 两个人的解析结果被悄悄合并成一个——"外宗弟子"甲乙正是这个形状）。
    _decisions, resolved_by_group = response_decisions(response)
    merged: list[dict] = []
    for item in candidates:
        resolution = resolved_by_group.get(str(item.get("identity_group") or "").strip())
        if not resolution or resolution.get("identity_kind") != "named":
            merged.append(item)
            continue
        canonical_name = str(resolution.get("canonical_name") or "").strip()
        resolved_authority_id = (
            str(resolution.get("authority_id") or "")
            or _canonical_named_authority_id(canonical_name)
        )
        merged.append({
            **item,
            "name": canonical_name,
            "identity_kind": "named",
            "authority_id": resolved_authority_id,
            # The selected backend decision owns this verdict.  Never inherit
            # a previous functional candidate's optimistic flag, and never
            # recompute from the final authority ID alone: its origin group may
            # still be a durable incompatible manual/reference group.
            "materialization_compatible": bool(
                resolution.get("materialization_compatible")
            ),
            "future_evidence": str(
                resolution.get("future_evidence") or ""
            ),
            "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
            # 归一观测（第26轮 ERR-20260824-88ece5）：resolution_kind 是
            # 后端自己判定这个 group 具体走的是哪条决议（known_named/
            # new_named/reissue_known），此前从未向调用方暴露——加上后
            # 调用方可以直接 `sum(1 for c in result if c.get("resolution_
            # kind") == REISSUE_KNOWN_RESOLUTION_KIND)` 得到
            # normalized_new_reissues 计数，不需要另开一条平行的计数
            # 通路。纯附加字段，不影响任何既有消费者。
            "resolution_kind": str(resolution.get("resolution_kind") or ""),
        })
    return merged


async def audit_identity_coverage_from_structural_evidence(
    candidates: list[dict],
    *,
    structural_evidence: list[dict] | None,
    source_text: str,
    bible: Bible,
    episode_no: int,
    existing_resolutions: list[dict] | None = None,
    catalog_receipt: dict[str, object] | None = None,
) -> list[dict]:
    """Audit only typed Blueprint/IR references that lack identity ownership."""
    evidence = [item for item in (structural_evidence or []) if isinstance(item, dict)]
    if not evidence:
        return candidates
    source_by_id = {
        segment.segment_id: segment.text
        for segment in index_source_segments(source_text)
    }
    source_order = {
        source_id: index for index, source_id in enumerate(source_by_id)
    }
    minimal = []
    for item in evidence:
        source_ids = [
            str(value) for value in item.get("source_segment_ids") or []
            if str(value) in source_by_id
        ]
        minimal.append({
            **item,
            "source_segment_ids": source_ids,
            "source_segments": {
                source_id: source_by_id[source_id] for source_id in source_ids
            },
        })
    allowed_source_labels = list(dict.fromkeys(
        str(item.get("identity_key") or "").strip()
        for item in minimal
        if str(item.get("identity_key") or "").strip()
    ))
    authority_by_id: dict[str, dict] = {}
    for character in bible.characters:
        canonical_name = str(character.name or "").strip()
        if canonical_name:
            authority_by_id[f"bible:{canonical_name}"] = {
                "authority_id": f"bible:{canonical_name}",
                "canonical_name": canonical_name,
                "identity_group": "",
                "aliases": [],
                "materialization_compatible": True,
            }
    groups_by_ref: dict[str, dict] = {}
    # Current RF9 may preserve a non-literal provider label as an explicitly
    # synthetic observation.  It is useful as a low-confidence audit input,
    # but it is not an alias or identity authority and must never suppress the
    # Blueprint-owned coverage gate.
    catalog_candidates = [
        candidate
        for candidate in candidates
        if identity_resolution_is_authoritative(candidate)
    ]
    for resolution in existing_resolutions or []:
        if not screenplay_identity_resolution_is_current_for_source(
            resolution,
            episode_no=episode_no,
            source_text=source_text,
        ) or not identity_resolution_is_authoritative(resolution):
            continue
        canonical_name = str(
            resolution.get("canonical_name") or ""
        ).strip()
        catalog_candidates.append({
            "source_label": str(
                resolution.get("source_label") or ""
            ).strip(),
            "name": canonical_name,
            "identity_kind": (
                "functional"
                if resolution_declares_functional_identity(resolution)
                else "named"
            ),
            "identity_group": str(
                resolution.get("identity_group") or ""
            ).strip(),
            "authority_id": str(
                resolution.get("authority_id") or ""
            ).strip(),
        })
    for candidate in catalog_candidates:
        source_label = str(candidate.get("source_label") or "").strip()
        canonical_name = str(candidate.get("name") or "").strip()
        identity_group = str(candidate.get("identity_group") or "").strip()
        identity_kind = str(candidate.get("identity_kind") or "").strip()
        if identity_group:
            group = groups_by_ref.setdefault(identity_group, {
                "identity_group_ref": identity_group,
                "source_labels": [],
                "authority_ids": [],
                "source_segment_ids": [],
            })
            if source_label and source_label not in group["source_labels"]:
                group["source_labels"].append(source_label)
            candidate_source_ids = [
                str(value).strip()
                for value in (
                    candidate.get("source_segment_ids")
                    or [candidate.get("source_segment_id")]
                )
                if str(value or "").strip() in source_by_id
            ]
            for source_id in candidate_source_ids:
                if source_id not in group["source_segment_ids"]:
                    group["source_segment_ids"].append(source_id)
        if identity_kind == "named" and canonical_name:
            authority_id = str(candidate.get("authority_id") or "").strip()
            if not authority_id:
                authority_id = _canonical_named_authority_id(canonical_name)
            authority = authority_by_id.setdefault(authority_id, {
                "authority_id": authority_id,
                "canonical_name": canonical_name,
                "identity_group": identity_group,
                "aliases": [],
                "materialization_compatible": (
                    authority_id == _canonical_named_authority_id(canonical_name)
                    and identity_group in {"", authority_id}
                ),
            })
            if authority["canonical_name"] != canonical_name:
                raise ContentGenerationError(
                    f"identity authority={authority_id} 对应多个真名"
                )
            if source_label and source_label not in authority["aliases"]:
                authority["aliases"].append(source_label)
            if identity_group:
                group = groups_by_ref[identity_group]
                if authority_id not in group["authority_ids"]:
                    group["authority_ids"].append(authority_id)
    seed_group_by_label: dict[str, str] = {}
    for label in allowed_source_labels:
        seed_ref = "new:" + evidence_repository.content_hash({
            "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
            "source_label": label,
            "structural_evidence": sorted(
                [
                    item for item in minimal
                    if str(item.get("identity_key") or "").strip() == label
                ],
                key=evidence_repository.content_hash,
            ),
        })[:24]
        seed_group_by_label[label] = seed_ref
        groups_by_ref.setdefault(seed_ref, {
            "identity_group_ref": seed_ref,
            "source_labels": [label],
            "authority_ids": [],
            "source_segment_ids": [],
        })
    conflicting_groups = {
        group_ref: sorted(set(group.get("authority_ids") or []))
        for group_ref, group in groups_by_ref.items()
        if len(set(group.get("authority_ids") or [])) > 1
    }
    if conflicting_groups:
        raise ContentGenerationError(
            "structural identity group 缺少唯一权威："
            + json.dumps(
                conflicting_groups,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    structural_by_key: dict[str, list[dict]] = {}
    for item in minimal:
        label = str(item.get("identity_key") or "").strip()
        if label:
            structural_by_key.setdefault(label, []).append(item)
    owned_source_by_key = {
        label: "\n".join(
            str(text)
            for item in items
            for text in (item.get("source_segments") or {}).values()
            if str(text)
        )
        for label, items in structural_by_key.items()
    }
    missing_owned_source = [
        label for label in allowed_source_labels
        if not owned_source_by_key.get(label, "").strip()
    ]
    if missing_owned_source:
        raise ContentGenerationError(
            "structural identity coverage 缺少 owned SRC："
            + ",".join(sorted(missing_owned_source))
        )
    for label, typed_items in structural_by_key.items():
        if {
            str(item.get("usage") or "").strip() for item in typed_items
        } == {"mentioned"}:
            continue
        matching_authorities = [
            authority
            for authority in authority_by_id.values()
            if label in {
                str(authority.get("canonical_name") or "").strip(),
                *(
                    str(alias or "").strip()
                    for alias in authority.get("aliases") or []
                ),
            }
        ]
        if (
            matching_authorities
            and not any(
                authority.get("materialization_compatible")
                for authority in matching_authorities
            )
        ):
            raise ContentGenerationError(
                "structural coverage 可见人物只有不可物化的引用身份："
                f"{label}"
            )

    coverage_groups = [
        {
            "group_key": f"I{index:03d}",
            "source_label": label,
            "source_segment_ids": sorted({
                str(source_id)
                for item in structural_by_key[label]
                for source_id in item.get("source_segment_ids") or []
                if str(source_id) in source_order
            }, key=lambda source_id: source_order[source_id]),
            "seed_group_ref": seed_group_by_label[label],
        }
        for index, label in enumerate(allowed_source_labels, start=1)
    ]
    coverage_group_by_key = {
        str(group["group_key"]): group for group in coverage_groups
    }
    evidence_by_id: dict[str, dict] = {}
    evidence_ids_by_group: dict[str, list[str]] = {}
    for group in coverage_groups:
        group_key = str(group["group_key"])
        evidence_ids: list[str] = []
        for source_id in group["source_segment_ids"]:
            text = str(source_by_id[source_id])
            evidence_id = "E:" + evidence_repository.content_hash({
                "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "group_key": group_key,
                "source_segment_id": source_id,
                "text": text,
            })[:20]
            evidence_by_id[evidence_id] = {
                "evidence_id": evidence_id,
                "group_key": group_key,
                "source_segment_id": source_id,
                "text": text,
            }
            evidence_ids.append(evidence_id)
        evidence_ids_by_group[group_key] = evidence_ids

    def matching_group_evidence_ids(
        group_key: str,
        identity_group_ref: str,
    ) -> list[str]:
        """Return owned spans for an exact backend-registered label binding.

        Mere SRC overlap or co-occurrence is not identity evidence: one source
        sentence routinely contains several people.  An existing group is
        eligible only when this exact synthetic identity key is already one of
        its registered labels and appears verbatim in the owned span.
        """
        group = groups_by_ref.get(identity_group_ref, {})
        registered_labels = {
            str(value).strip()
            for value in group.get("source_labels") or []
            if str(value).strip()
        }
        source_label = str(
            coverage_group_by_key[group_key]["source_label"]
        )
        if source_label not in registered_labels:
            return []
        return [
            evidence_id
            for evidence_id in evidence_ids_by_group.get(group_key, [])
            if source_label in str(evidence_by_id[evidence_id]["text"])
        ]

    decision_by_id: dict[str, dict] = {}
    decision_ids_by_group: dict[str, list[str]] = {}

    def register_decision(group_key: str, payload: dict) -> str:
        decision_kind = (
            "K" if payload.get("identity_kind") == "named" else "F"
        )
        decision_id = (
            f"{decision_kind}:{group_key}:"
            + evidence_repository.content_hash({
                "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                **payload,
            })[:16]
        )
        decision_by_id[decision_id] = {
            "decision_id": decision_id,
            "group_key": group_key,
            **payload,
        }
        decision_ids_by_group.setdefault(group_key, []).append(decision_id)
        return decision_id

    def coverage_group_kind(group_key: str) -> str:
        label = str(coverage_group_by_key[group_key]["source_label"])
        usages = {
            str(item.get("usage") or "").strip()
            for item in structural_by_key.get(label, [])
        }
        return "mentioned" if usages == {"mentioned"} else "onscreen"

    for group in coverage_groups:
        group_key = str(group["group_key"])
        label = str(group["source_label"])
        evidence_ids = evidence_ids_by_group[group_key]
        primary_evidence_id = evidence_ids[0]
        source_ids = list(group["source_segment_ids"])
        seed_group_ref = str(group["seed_group_ref"])
        register_decision(group_key, {
            "source_label": label,
            "identity_kind": "functional",
            "identity_group_ref": seed_group_ref,
            "authority_id": "",
            "canonical_name": "",
            "evidence_id": primary_evidence_id,
            "owned_source_segment_ids": source_ids,
            "proof_kind": "owned_functional_new",
        })
        for identity_group_ref, catalog_group in groups_by_ref.items():
            if identity_group_ref == seed_group_ref:
                continue
            matching_ids = matching_group_evidence_ids(
                group_key, identity_group_ref
            )
            if not matching_ids:
                continue
            group_authorities = sorted(set(
                str(value)
                for value in catalog_group.get("authority_ids") or []
                if str(value)
            ))
            if not group_authorities:
                register_decision(group_key, {
                    "source_label": label,
                    "identity_kind": "functional",
                    "identity_group_ref": identity_group_ref,
                    "authority_id": "",
                    "canonical_name": "",
                    "evidence_id": matching_ids[0],
                    "owned_source_segment_ids": source_ids,
                    "proof_kind": "owned_functional_existing_group",
                })

        for authority_id, authority in authority_by_id.items():
            canonical_name = str(
                authority.get("canonical_name") or ""
            ).strip()
            materialization_compatible = bool(
                authority.get("materialization_compatible")
            )
            if (
                coverage_group_kind(group_key) == "onscreen"
                and not materialization_compatible
            ):
                # A non-Bible/manual authority may be cited while mentioned,
                # but cannot be upgraded through coverage into a card-backed
                # onscreen identity without atomically migrating its authority.
                continue
            authority_anchors = list(dict.fromkeys(
                value
                for value in [
                    canonical_name,
                    *[
                        str(alias).strip()
                        for alias in authority.get("aliases") or []
                    ],
                ]
                if value
            ))
            identity_label_anchor_ids = [
                evidence_id
                for evidence_id in evidence_ids
                if label in authority_anchors
                and label in str(evidence_by_id[evidence_id]["text"])
            ]
            if identity_label_anchor_ids:
                register_decision(group_key, {
                    "source_label": label,
                    "identity_kind": "named",
                    "identity_group_ref": seed_group_ref,
                    "authority_id": authority_id,
                    "canonical_name": canonical_name,
                    "evidence_id": identity_label_anchor_ids[0],
                    "owned_source_segment_ids": source_ids,
                    "proof_kind": "identity_key_registered_authority",
                    "proof_anchors": [label],
                    "materialization_compatible": materialization_compatible,
                })
            for identity_group_ref, catalog_group in groups_by_ref.items():
                if set(catalog_group.get("authority_ids") or []) != {
                    authority_id
                }:
                    continue
                matching_ids = matching_group_evidence_ids(
                    group_key, identity_group_ref
                )
                if not matching_ids:
                    continue
                register_decision(group_key, {
                    "source_label": label,
                    "identity_kind": "named",
                    "identity_group_ref": identity_group_ref,
                    "authority_id": authority_id,
                    "canonical_name": canonical_name,
                    "evidence_id": matching_ids[0],
                    "owned_source_segment_ids": source_ids,
                    "proof_kind": "existing_bound_group",
                    "proof_anchors": [],
                    "materialization_compatible": materialization_compatible,
                })

    coverage_group_keys = [
        str(group["group_key"]) for group in coverage_groups
    ]
    coverage_schema = _structural_identity_coverage_schema(
        coverage_group_keys,
        decision_ids_by_group=decision_ids_by_group,
    )
    coverage_response_format = _structural_identity_coverage_response_format(
        coverage_schema
    )
    coverage_group_projection = [
        {
            "group_key": group["group_key"],
            "source_label": group["source_label"],
            "owned_source_segment_ids": group["source_segment_ids"],
        }
        for group in coverage_groups
    ]
    coverage_evidence_projection = list(evidence_by_id.values())
    coverage_decision_projection = list(decision_by_id.values())
    receipt_hashes = {
        "authority_catalog_hash": evidence_repository.content_hash(
            sorted(
                authority_by_id.values(),
                key=lambda item: str(item.get("authority_id") or ""),
            )
        ),
        "group_catalog_hash": evidence_repository.content_hash(
            sorted(
                groups_by_ref.values(),
                key=lambda item: str(
                    item.get("identity_group_ref") or ""
                ),
            )
        ),
        "decision_catalog_hash": evidence_repository.content_hash(
            coverage_decision_projection
        ),
        "evidence_catalog_hash": evidence_repository.content_hash(
            coverage_evidence_projection
        ),
    }
    if catalog_receipt is not None:
        catalog_receipt.clear()
        catalog_receipt.update({
            "version": _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION,
            **receipt_hashes,
            "hash": evidence_repository.content_hash(receipt_hashes),
        })
    prompt = f"""任务：审计结构化蓝图/IR 中未绑定的人物引用。
未决引用目录（group_key 是唯一可输出的键；同键所有 owned SRC 行共用一个决议）：
{json.dumps(coverage_group_projection, ensure_ascii=False, separators=(',', ':'))}
owned SRC 证据目录（后端逐字锁定，不得回抄或改写）：
{json.dumps(coverage_evidence_projection, ensure_ascii=False, separators=(',', ':'))}
可选决议目录（每个不透明 decision_id 已绑定 label/kind/group/authority/evidence/source_ids）：
{json.dumps(coverage_decision_projection, ensure_ascii=False, separators=(',', ':'))}
规则：
1. decisions 必须精确输出全部 group_key，不得增删键。
2. 证据不足时选 F 决议，这是合法终态；不得猜测人物权威。
3. 只有目录已提供 K 决议时才能绑定已有人物；不得自行组合姓名、组或证据。
4. 只输出符合下列 Schema 的 JSON：
{json.dumps(coverage_schema, ensure_ascii=False, separators=(',', ':'))}"""
    coverage_provider, coverage_model, coverage_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    coverage_semantic_settings = hiagent.text_request_semantic_settings(
        coverage_provider
    )

    def validate_response(
        value: StructuralIdentityCoverageResponse,
    ) -> list[str]:
        errors: list[str] = []
        expected_keys = set(coverage_group_keys)
        if set(value.decisions) != expected_keys:
            errors.append("structural coverage decisions keys 不闭合")
        named_authorities_by_group: dict[str, set[str]] = {}
        named_groups: set[str] = set()
        functional_groups: set[str] = set()
        for group_key in coverage_group_keys:
            selected_id = str(value.decisions.get(group_key) or "")
            item = decision_by_id.get(selected_id)
            if item is None or item.get("group_key") != group_key:
                errors.append(f"structural coverage decision_id 越界：{group_key}")
                continue
            source_label = str(item.get("source_label") or "")
            expected_label = str(
                coverage_group_by_key[group_key]["source_label"]
            )
            if source_label != expected_label:
                errors.append(f"structural coverage label 不匹配：{group_key}")
            identity_group = str(item.get("identity_group_ref") or "")
            if identity_group not in groups_by_ref:
                errors.append(f"identity_group_ref 越界：{group_key}")
            expected_source_ids = list(
                coverage_group_by_key[group_key]["source_segment_ids"]
            )
            if list(item.get("owned_source_segment_ids") or []) != (
                expected_source_ids
            ):
                errors.append(f"owned source ids 不闭合：{group_key}")
            evidence_id = str(item.get("evidence_id") or "")
            evidence = evidence_by_id.get(evidence_id)
            if (
                evidence_id not in evidence_ids_by_group.get(group_key, [])
                or evidence is None
                or str(evidence.get("source_segment_id") or "")
                not in expected_source_ids
                or str(evidence.get("text") or "")
                != source_by_id.get(
                    str(evidence.get("source_segment_id") or ""), ""
                )
            ):
                errors.append(f"owned evidence receipt 无效：{group_key}")
                continue
            if item.get("identity_kind") == "named":
                authority_id = str(item.get("authority_id") or "")
                authority = authority_by_id.get(authority_id)
                if authority is None:
                    errors.append(f"authority_id 越界：{group_key}")
                else:
                    existing_group_authorities = set(
                        groups_by_ref.get(identity_group, {}).get(
                            "authority_ids", []
                        )
                    )
                    if (
                        existing_group_authorities
                        and authority_id not in existing_group_authorities
                    ):
                        errors.append(
                            "named authority 与已有 group 权威冲突："
                            f"{group_key}"
                        )
                    authority_anchors = set(
                        str(value).strip()
                        for value in item.get("proof_anchors") or []
                        if str(value).strip()
                    )
                    proof_kind = str(item.get("proof_kind") or "")
                    bound_group_proof = bool(
                        proof_kind == "existing_bound_group"
                        and existing_group_authorities == {authority_id}
                        and evidence_id in matching_group_evidence_ids(
                            group_key, identity_group
                        )
                    )
                    label_authority_proof = bool(
                        proof_kind == "identity_key_registered_authority"
                        and source_label in authority_anchors
                        and source_label
                        in str(evidence.get("text") or "")
                        and source_label
                        in {
                            str(authority.get("canonical_name") or "").strip(),
                            *(
                                str(alias or "").strip()
                                for alias in authority.get("aliases") or []
                            ),
                        }
                    )
                    if not (bound_group_proof or label_authority_proof):
                        errors.append(
                            "named group 缺少 owned authority 锚点："
                            f"{group_key}"
                        )
                    if (
                        coverage_group_kind(group_key) == "onscreen"
                        and not item.get("materialization_compatible")
                    ):
                        errors.append(
                            "structural coverage K authority 不可直接物化人物卡："
                            f"{group_key}"
                        )
                named_groups.add(identity_group)
                named_authorities_by_group.setdefault(
                    identity_group, set()
                ).add(authority_id)
            else:
                if item.get("authority_id") or item.get("canonical_name"):
                    errors.append(f"functional 携带权威：{group_key}")
                functional_groups.add(identity_group)
        for identity_group, authority_ids in named_authorities_by_group.items():
            if len(authority_ids) > 1:
                errors.append(
                    "identity_group 对应多个 named authority："
                    f"{identity_group}"
                )
        for identity_group in named_groups & functional_groups:
            errors.append(
                "functional 不得引用本响应已升级 group："
                f"{identity_group}"
            )
        for identity_group in functional_groups:
            if groups_by_ref.get(identity_group, {}).get("authority_ids"):
                errors.append(
                    "functional 不得引用已命名 group："
                    f"{identity_group}"
                )
        return errors

    response = await _identity_structured_with_resample(
        [{"role": "user", "content": prompt}],
        model_type=StructuralIdentityCoverageResponse,
        validate=validate_response,
        operation_id_for_attempt=lambda resample_attempt: (
            f"screenplay.identity.coverage.v6:{episode_no}:"
            + evidence_repository.content_hash({
                "resample_attempt": resample_attempt,
                "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "provider": coverage_provider,
                "model": coverage_model,
                "requested_max_tokens": 4096,
                "effective_max_tokens": coverage_effective_max,
            "temperature": 0.05,
            "provider_semantic_settings": coverage_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
                "prompt": prompt,
                "schema": coverage_schema,
                "response_format": coverage_response_format,
            })
        ),
        max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        temperature=0.05,
        format_retry_limit=0,
        semantic_retry_limit=0,
        call_meta={
            "stage": "discover_character_candidates",
            "stage_key": "screenplay_character_discovery",
            "substage": "structural_coverage",
            "discovery_phase": "coverage",
            "episode_no": episode_no,
            "contract_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
            "schema_hash": evidence_repository.content_hash(
                coverage_schema
            ),
            "decision_catalog_hash": evidence_repository.content_hash(
                coverage_decision_projection
            ),
            "evidence_catalog_hash": evidence_repository.content_hash(
                coverage_evidence_projection
            ),
            "disable_provider_retries": True,
            "disable_provider_candidate_fallback": True,
            "disable_reasoning_fallback": True,
            "reuse_successful_operation": False,
            "provider": coverage_provider,
            "model": coverage_model,
            "effective_max_tokens": coverage_effective_max,
            "provider_semantic_settings": coverage_semantic_settings,
            "retry_epoch": _identity_operation_retry_epoch(),
        },
        output_schema=coverage_schema,
        response_format=coverage_response_format,
        require_response_format=True,
    )
    selected_decisions = [
        decision_by_id[str(response.decisions[group_key])]
        for group_key in coverage_group_keys
    ]
    existing = {
        (str(item.get("source_label") or ""), str(item.get("identity_group") or ""))
        for item in candidates
    }
    additions: list[dict] = []
    new_group_members: dict[str, set[str]] = {}
    for decision in selected_decisions:
        raw_group = str(decision.get("identity_group_ref") or "").strip()
        label = str(decision.get("source_label") or "").strip()
        if raw_group.startswith("new:") and label:
            new_group_members.setdefault(raw_group, set()).add(label)
    normalized_new_groups = {
        raw_group: (
            "structural:"
            + evidence_repository.content_hash({
                "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "source_labels": sorted(labels),
                "source_segment_ids": sorted(
                    {
                        str(source_id)
                        for label in labels
                        for typed_item in structural_by_key.get(label, [])
                        for source_id in typed_item.get("source_segment_ids") or []
                        if str(source_id) in source_order
                    },
                    key=lambda source_id: source_order[source_id],
                ),
            })[:24]
        )
        for raw_group, labels in new_group_members.items()
    }
    for raw in selected_decisions:
        label = str(raw.get("source_label") or "").strip()
        typed_evidence = structural_by_key.get(label) or []
        if not label or not typed_evidence:
            raise ContentGenerationError(
                f"结构人物 coverage 缺少 owned evidence：{label}"
            )
        identity_kind = str(raw.get("identity_kind") or "functional")
        authority_id = str(raw.get("authority_id") or "").strip()
        canonical_name = str(
            authority_by_id.get(authority_id, {}).get("canonical_name") or ""
        )
        raw_group = str(raw.get("identity_group_ref") or "").strip()
        group = normalized_new_groups.get(raw_group, raw_group)
        if (label, group) in existing:
            continue
        usages = {
            str(value.get("usage") or "").strip()
            for value in typed_evidence
        }
        projected_kind = "mentioned" if usages == {"mentioned"} else "onscreen"
        if (
            identity_kind == "named"
            and projected_kind == "onscreen"
            and not raw.get("materialization_compatible")
        ):
            raise ContentGenerationError(
                "structural coverage K authority 不可直接物化人物卡："
                f"{label}"
            )
        source_ids = sorted({
            str(source_id)
            for value in typed_evidence
            for source_id in value.get("source_segment_ids") or []
            if str(source_id) in source_by_id
        }, key=lambda source_id: source_order[source_id])
        evidence_record = evidence_by_id.get(
            str(raw.get("evidence_id") or ""), {}
        )
        source_segment_id = str(
            evidence_record.get("source_segment_id") or ""
        )
        if source_segment_id not in source_ids:
            raise ContentGenerationError(
                f"结构人物 coverage evidence receipt 越界：{label}"
            )
        evidence_text = str(evidence_record.get("text") or "")
        proof_anchors = [
            str(value)
            for value in raw.get("proof_anchors") or []
            if str(value)
        ]
        bounded_evidence = (
            _bounded_owned_identity_evidence(
                evidence_text,
                anchors=proof_anchors,
                max_chars=80,
            )
            if identity_kind == "named" and proof_anchors
            else evidence_text.strip()[:80]
        )
        additions.append({
            "name": canonical_name or label,
            "source_label": label,
            "identity_kind": identity_kind,
            "identity_group": group,
            "authority_id": authority_id if identity_kind == "named" else "",
            "kind": projected_kind,
            "evidence": bounded_evidence,
            "future_evidence": "",
            "source_segment_ids": source_ids,
            "source_segment_id": source_segment_id,
            "source_quote": source_by_id.get(source_segment_id, ""),
            "_typed_source_evidence_owned": bool(source_segment_id),
            "materialization_compatible": bool(
                raw.get("materialization_compatible")
            ),
        })
    return _attach_candidate_source_evidence([*candidates, *additions], source_text)


async def discover_character_candidates(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    future_text: str = "",
    future_label: str = "",
    existing_resolutions: list[dict] | None = None,
    structural_evidence: list[dict] | None = None,
    scope_id: str | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Targeted identity pipeline: current, unresolved future, typed audit."""
    artifact_scope_id = str(scope_id or f"episode-{episode_no}")
    targeted = str(
        get_setting("screenplay_targeted_identity_enabled") or "true"
    ).strip().lower() not in {"0", "false", "off", "no"}
    structural_coverage_applied = bool(
        targeted and structural_evidence
    )
    discovery_input = {
        "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": (
            _current_identity_evidence_catalog_hash(
                source_text,
                draft_text=draft_text,
            )
        ),
        "mode": "targeted" if targeted else "legacy",
        "episode_no": episode_no,
        "source_text": source_text,
        "draft_text": draft_text,
        "future_text": future_text,
        "future_label": future_label,
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": existing_resolutions or [],
        "structural_evidence": structural_evidence or [],
    }
    if structural_coverage_applied:
        discovery_input.update({
            "structural_coverage_policy_version": (
                STRUCTURAL_IDENTITY_COVERAGE_VERSION
            ),
            "structural_coverage_applied": True,
        })
    input_hash = evidence_repository.content_hash(discovery_input)
    evidence_conn = get_conn()
    artifacts_available = bool(
        scope_id
        and evidence_conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
        ).fetchone()
    )
    artifact_seals_available = bool(
        artifacts_available
        and _has_column(evidence_conn, "artifacts", "content_hash")
    )
    cached_rows = (
        evidence_conn.execute(
            """SELECT content_json,content_hash FROM artifacts
                 WHERE scope_type='episode' AND scope_id=?
                   AND type='screenplay_identity_discovery' AND status='validated'
                 ORDER BY created_at DESC LIMIT 20""",
            (artifact_scope_id,),
        ).fetchall()
        if artifact_seals_available else []
    )
    for row in cached_rows:
        try:
            cached = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            not isinstance(cached, dict)
            or not str(row["content_hash"] or "").strip()
            or str(row["content_hash"] or "").strip()
            != evidence_repository.content_hash(cached)
        ):
            # `validated` is not an integrity seal by itself.  Never reuse a
            # payload whose bytes no longer match the repository-owned hash.
            continue
        if (
            cached.get("contract_version") == IDENTITY_DISCOVERY_CONTRACT_VERSION
            and cached.get("current_identity_version")
            == CURRENT_IDENTITY_DECISION_VERSION
            and cached.get("current_evidence_catalog_hash")
            == discovery_input["current_evidence_catalog_hash"]
            and (
                not structural_coverage_applied
                or (
                    cached.get("structural_coverage_policy_version")
                    == STRUCTURAL_IDENTITY_COVERAGE_VERSION
                    and cached.get("structural_coverage_applied") is True
                )
            )
            and cached.get("input_hash") == input_hash
            and isinstance(cached.get("candidates"), list)
        ):
            if any(not isinstance(item, dict) for item in cached["candidates"]):
                continue
            cached_candidates = [dict(item) for item in cached["candidates"]]
            typed_current_candidates = [
                item for item in cached_candidates
                if str(item.get("source_label_provenance") or "").strip()
                in {
                    CURRENT_IDENTITY_LITERAL_PROVENANCE,
                    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
                }
            ]
            if (
                targeted
                and not structural_coverage_applied
                and len(typed_current_candidates) != len(cached_candidates)
            ):
                continue
            if any(
                item.get("source_evidence_receipt") is None
                or item.get("source_evidence_receipts") is None
                for item in typed_current_candidates
            ):
                continue
            try:
                _attach_candidate_source_evidence(
                    typed_current_candidates,
                    source_text,
                    draft_text=draft_text,
                )
                return cached_candidates
            except ContentGenerationError:
                # A validated marker cannot override a broken RF11 receipt.
                # Ignore the cache and rerun the strict discovery gate.
                continue

    if targeted:
        current = await extract_current_identity_candidates(
            source_text,
            bible,
            episode_no,
            draft_text=draft_text,
            existing_resolutions=existing_resolutions,
            project_id=project_id,
        )
        resolved = await resolve_future_identity_candidates(
            current,
            source_text=source_text,
            future_text=future_text,
            bible=bible,
            episode_no=episode_no,
            future_label=future_label,
        )
        audited = await audit_identity_coverage_from_structural_evidence(
            resolved,
            structural_evidence=structural_evidence,
            source_text=source_text,
            bible=bible,
            episode_no=episode_no,
            existing_resolutions=existing_resolutions,
        )
    else:
        audited = _attach_candidate_source_evidence(
            await _discover_character_candidates_legacy(
                source_text,
                bible,
                episode_no,
                draft_text=draft_text,
                future_text=future_text,
                future_label=future_label,
                existing_resolutions=existing_resolutions,
                project_id=project_id,
            ),
            source_text,
        )
    trace = None
    try:
        from app.observability.tracing import current_trace
        trace = current_trace()
    except Exception:  # noqa: BLE001 - evidence is optional outside workflows
        pass
    if not artifacts_available:
        return audited
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery_raw",
            scope_type="episode",
            scope_id=artifact_scope_id,
            status="candidate",
            trust_level="T0",
            content={
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                "current_evidence_catalog_hash": discovery_input[
                    "current_evidence_catalog_hash"
                ],
                "structural_coverage_policy_version": (
                    STRUCTURAL_IDENTITY_COVERAGE_VERSION
                ),
                "structural_coverage_applied": structural_coverage_applied,
                "input_hash": input_hash,
                "mode": "targeted" if targeted else "legacy",
                "model_candidates": audited,
            },
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery",
            scope_type="episode",
            scope_id=artifact_scope_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                "current_evidence_catalog_hash": discovery_input[
                    "current_evidence_catalog_hash"
                ],
                "structural_coverage_policy_version": (
                    STRUCTURAL_IDENTITY_COVERAGE_VERSION
                ),
                "structural_coverage_applied": structural_coverage_applied,
                "episode_no": episode_no,
                "candidates": audited,
                "source_hash": evidence_repository.content_hash(source_text),
                "input_hash": input_hash,
                "mode": "targeted" if targeted else "legacy",
            },
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    return audited


def _identity_resolution(
    item: dict,
    canonical_name: str,
    resolution: str,
    *,
    reason: str = "",
) -> dict:
    receipt_bundle = _validate_current_identity_receipt_bundle(
        item,
        source_text=None,
    )
    primary_receipt = receipt_bundle[0] if receipt_bundle is not None else None
    receipt_list = receipt_bundle[1] if receipt_bundle is not None else []
    receipt_source_ids = (
        receipt_bundle[2]
        if receipt_bundle is not None
        else list(dict.fromkeys(
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ))
    )
    payload = {
        "source_label": str(item.get("source_label") or item.get("name") or "").strip(),
        "canonical_name": canonical_name,
        "resolution": resolution,
        "reason": reason,
        "evidence": str(item.get("evidence") or "").strip()[:80],
        "future_evidence": str(item.get("future_evidence") or "").strip()[:120],
        "identity_group": str(item.get("identity_group") or "").strip()[:96],
        "identity_scope_fingerprint": str(
            item.get("identity_scope_fingerprint") or ""
        ).strip(),
        "decision_provenance": str(
            item.get("decision_provenance")
            or AUTOMATIC_IDENTITY_DECISION_PROVENANCE
        ).strip(),
        "decision_contract_version": FUTURE_IDENTITY_DECISION_VERSION,
        "structural_identity_policy_version": (
            STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
        "authority_id": str(item.get("authority_id") or "").strip(),
        "source_label_provenance": str(
            item.get("source_label_provenance") or ""
        ).strip(),
        "source_segment_ids": receipt_source_ids,
    }
    if primary_receipt is not None:
        payload.update({
            "source_evidence_receipt": dict(primary_receipt),
            "source_evidence_receipts": receipt_list,
            "source_segment_id": str(
                primary_receipt.get("source_segment_id") or ""
            ),
            "source_quote": str(primary_receipt.get("text") or ""),
        })
    return normalize_character_resolution(payload)


def structural_identity_resolution_is_current(value: dict) -> bool:
    """Whether a durable resolution may suppress the current coverage gate."""
    provenance = str(value.get("decision_provenance") or "").strip()
    return bool(
        provenance in DURABLE_IDENTITY_DECISION_PROVENANCE
        or str(
            value.get("structural_identity_policy_version") or ""
        ).strip() == STRUCTURAL_IDENTITY_COVERAGE_VERSION
    )


def screenplay_identity_resolution_is_current_for_source(
    value: dict,
    *,
    episode_no: int,
    source_text: str,
) -> bool:
    """Fence automatic identity authority by wire versions and source epoch."""
    current = screenplay_identity_resolution_is_current_for_scope(
        value,
        identity_scope_fingerprint=screenplay_identity_scope_fingerprint(
            episode_no, source_text
        ),
    )
    provenance = str(value.get("decision_provenance") or "").strip()
    label_provenance = str(
        value.get("source_label_provenance") or ""
    ).strip()
    if (
        current
        and provenance not in DURABLE_IDENTITY_DECISION_PROVENANCE
        and label_provenance in {
            CURRENT_IDENTITY_LITERAL_PROVENANCE,
            CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
        }
    ):
        try:
            return _validate_current_identity_receipt_bundle(
                value,
                source_text=source_text,
            ) is not None
        except ContentGenerationError:
            return False
    if (
        current
        and provenance not in DURABLE_IDENTITY_DECISION_PROVENANCE
        and label_provenance == IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
    ):
        return _identity_adjudication_receipt_is_valid(
            value,
            source_text=source_text,
        )
    return current


def screenplay_identity_resolution_is_current_for_scope(
    value: dict,
    *,
    identity_scope_fingerprint: str,
) -> bool:
    """Fence automatic authority by wire versions and an owned-source epoch."""
    provenance = str(value.get("decision_provenance") or "").strip()
    if provenance in DURABLE_IDENTITY_DECISION_PROVENANCE:
        return True
    current = bool(
        str(value.get("decision_contract_version") or "").strip()
        == FUTURE_IDENTITY_DECISION_VERSION
        and structural_identity_resolution_is_current(value)
        and str(
            value.get("identity_scope_fingerprint") or ""
        ).strip() == str(identity_scope_fingerprint or "").strip()
    )
    label_provenance = str(
        value.get("source_label_provenance") or ""
    ).strip()
    if current and label_provenance in {
        CURRENT_IDENTITY_LITERAL_PROVENANCE,
        CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    }:
        try:
            return _validate_current_identity_receipt_bundle(
                value,
                source_text=None,
            ) is not None
        except ContentGenerationError:
            return False
    if (
        current
        and label_provenance == IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
    ):
        return _identity_adjudication_receipt_is_valid(
            value,
            source_text=None,
        )
    return current


def _identity_adjudication_receipt_is_valid(
    value: dict,
    *,
    source_text: str | None,
) -> bool:
    receipt = value.get("identity_adjudication_receipt")
    if not isinstance(receipt, dict):
        return False

    def exact_source_ids(raw: object) -> list[str] | None:
        if (
            not isinstance(raw, list)
            or not raw
            or any(
                not isinstance(item, str)
                or not item.strip()
                or item != item.strip()
                for item in raw
            )
        ):
            return None
        normalized = list(raw)
        return normalized if len(normalized) == len(set(normalized)) else None

    source_ids = exact_source_ids(receipt.get("source_segment_ids"))
    item_source_ids = exact_source_ids(value.get("source_segment_ids"))
    evidence_source_ids = exact_source_ids(value.get("evidence_source_ids"))
    if (
        receipt.get("version") != "screenplay-ir-identity-adjudicator.v2"
        or source_ids is None
        or item_source_ids is None
        or evidence_source_ids is None
    ):
        return False
    payload = {
        "version": receipt["version"],
        "source_hash": str(receipt.get("source_hash") or "").strip(),
        "source_segment_ids": source_ids,
    }
    if (
        not payload["source_hash"]
        or str(receipt.get("hash") or "").strip()
        != evidence_repository.content_hash(payload)
        or source_ids != item_source_ids
        or source_ids != evidence_source_ids
    ):
        return False
    if source_text is None:
        # Persistence compares validity classes without necessarily owning the
        # episode source.  The source-aware read fence below performs the
        # stronger membership/order proof whenever the source is available.
        return True
    if payload["source_hash"] != evidence_repository.content_hash(source_text):
        return False
    indexed_source_ids = [
        segment.segment_id for segment in index_source_segments(source_text)
    ]
    selected_source_ids = set(source_ids)
    return bool(
        selected_source_ids.issubset(indexed_source_ids)
        and source_ids
        == [
            source_id
            for source_id in indexed_source_ids
            if source_id in selected_source_ids
        ]
    )


_STRUCTURAL_IDENTITY_RECEIPT_VERSION = (
    "screenplay-identity-structural-resolution-receipt.v3"
)
_STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION = (
    "screenplay-identity-structural-catalog-receipt.v1"
)


def _structural_identity_candidate_semantic_rows(
    candidates: list[dict] | None,
) -> list[dict]:
    """Canonical semantic projection bound into a validated coverage Artifact."""
    fields = (
        "source_label",
        "name",
        "identity_kind",
        "kind",
        "identity_group",
        "authority_id",
        "source_segment_id",
        "source_quote",
        "source_label_provenance",
    )
    rows: list[dict] = []
    for item in (candidates or []):
        if not isinstance(item, dict):
            raise ContentGenerationError(
                "结构人物 candidate semantic receipt 含非对象项"
            )
        if not str(item.get("source_label") or "").strip() or not str(
            item.get("identity_group") or ""
        ).strip():
            raise ContentGenerationError(
                "结构人物 candidate semantic receipt 缺少身份键"
            )
        bundle = _validate_current_identity_receipt_bundle(
            item,
            source_text=None,
        )
        primary_receipt = bundle[0] if bundle is not None else None
        receipts = bundle[1] if bundle is not None else []
        receipt_source_ids = bundle[2] if bundle is not None else None
        source_segment_ids = (
            receipt_source_ids
            if receipt_source_ids is not None
            else [
                str(value).strip()
                for value in item.get("source_segment_ids") or []
                if str(value).strip()
            ]
        )
        rows.append({
            **{
                field: str(item.get(field) or "").strip()
                for field in fields
            },
            "source_segment_ids": source_segment_ids,
            "source_evidence_receipt_hash": (
                evidence_repository.content_hash(primary_receipt)
                if primary_receipt is not None
                else ""
            ),
            "source_evidence_receipts_hash": (
                evidence_repository.content_hash(receipts)
                if bundle is not None else ""
            ),
        })
    return sorted(
        rows,
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )


def _structural_identity_candidate_semantic_hash(
    candidates: list[dict] | None,
) -> str:
    return evidence_repository.content_hash(
        _structural_identity_candidate_semantic_rows(candidates)
    )


def _structural_identity_catalog_input_hash(
    *,
    bible: Bible,
    base_candidates: list[dict] | None,
    structural_evidence_hash: str,
    existing_resolutions: list[dict] | None,
    output_candidates: list[dict] | None,
) -> str:
    """Fingerprint every backend-owned input that can change coverage options.

    Automatic rows materialized by ``output_candidates`` are excluded so the
    pre-audit fingerprint remains stable after a successful coverage result is
    persisted.  Their complete semantics are already bound separately by the
    materialization receipt.  Durable/manual rows remain inputs even when they
    share a key with an output candidate.
    """
    output_keys = {
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        for item in (output_candidates or [])
        if isinstance(item, dict)
        and str(item.get("source_label") or "").strip()
        and str(item.get("identity_group") or "").strip()
    }
    resolution_fields = (
        "source_label",
        "canonical_name",
        "authority_id",
        "resolution",
        "identity_group",
        "identity_scope_fingerprint",
        "decision_provenance",
        "decision_contract_version",
        "structural_identity_policy_version",
    )
    resolution_rows = []
    for item in normalize_character_resolutions(existing_resolutions):
        key = (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        provenance = str(item.get("decision_provenance") or "").strip()
        if (
            provenance not in DURABLE_IDENTITY_DECISION_PROVENANCE
            and key in output_keys
        ):
            continue
        resolution_rows.append({
            field: str(item.get(field) or "").strip()
            for field in resolution_fields
        })
    resolution_rows.sort(
        key=lambda item: tuple(item[field] for field in resolution_fields)
    )
    output_named_authorities = {
        str(item.get("name") or "").strip()
        for item in (output_candidates or [])
        if isinstance(item, dict)
        and str(item.get("identity_kind") or "").strip() == "named"
        and str(item.get("name") or "").strip()
    }
    bible_authorities = sorted({
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
        and str(character.name or "").strip()
        not in output_named_authorities
    })
    return evidence_repository.content_hash({
        "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        "structural_evidence_hash": structural_evidence_hash,
        "bible_authorities": bible_authorities,
        "base_candidate_semantics": (
            _structural_identity_candidate_semantic_rows(base_candidates)
        ),
        "external_resolution_semantics": resolution_rows,
    })


def _structural_identity_catalog_receipt_is_valid(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    fields = (
        "authority_catalog_hash",
        "group_catalog_hash",
        "decision_catalog_hash",
        "evidence_catalog_hash",
    )
    payload = {field: str(value.get(field) or "") for field in fields}
    return bool(
        value.get("version")
        == _STRUCTURAL_IDENTITY_CATALOG_RECEIPT_VERSION
        and all(payload.values())
        and str(value.get("hash") or "")
        == evidence_repository.content_hash(payload)
    )


def _structural_identity_required_bible_names(
    candidates: list[dict] | None,
) -> list[str]:
    """Named visible identities require a committed card before cache success."""
    return sorted({
        str(item.get("name") or "").strip()
        for item in (candidates or [])
        if isinstance(item, dict)
        and str(item.get("identity_kind") or "").strip() == "named"
        and str(item.get("kind") or "onscreen").strip() != "mentioned"
        and str(item.get("name") or "").strip()
    })


def _project_bible_character_names(
    conn,
    project_id: str,
    fallback_bible: Bible,
) -> set[str]:
    """Read the post-materialization Bible, with isolated-test compatibility."""
    if _has_column(conn, "projects", "bible_json"):
        row = conn.execute(
            "SELECT bible_json FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if row and row["bible_json"]:
            try:
                current_bible = Bible.model_validate(
                    json.loads(row["bible_json"])
                )
            except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
                return set()
            return {
                str(character.name or "").strip()
                for character in current_bible.characters
                if str(character.name or "").strip()
            }
    return {
        str(character.name or "").strip()
        for character in fallback_bible.characters
        if str(character.name or "").strip()
    }


def _structural_identity_resolution_receipt(
    resolutions: list[dict] | None,
    *,
    candidates: list[dict] | None,
    identity_scope_fingerprint: str,
) -> dict:
    """Bind a coverage Artifact to the exact durable rows it materialized.

    Candidate keys select only rows owned by this coverage result; every
    authority-bearing field is retained so a same-label/group row with a
    different canonical identity can never satisfy replay recovery.
    """
    candidate_keys = {
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        for item in (candidates or [])
        if isinstance(item, dict)
        and str(item.get("source_label") or "").strip()
        and str(item.get("identity_group") or "").strip()
    }
    fields = (
        "source_label",
        "canonical_name",
        "authority_id",
        "authority_version",
        "resolution",
        "identity_group",
        "identity_scope_fingerprint",
        "source_instance_key",
        "decision_provenance",
        "decision_contract_version",
        "structural_identity_policy_version",
    )
    rows: list[dict] = []
    for item in normalize_character_resolutions(resolutions):
        key = (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        if key not in candidate_keys or not (
            screenplay_identity_resolution_is_current_for_scope(
            item,
            identity_scope_fingerprint=identity_scope_fingerprint,
            )
        ):
            continue
        bundle = _validate_current_identity_receipt_bundle(
            item,
            source_text=None,
        )
        primary_receipt = bundle[0] if bundle is not None else None
        receipt_list = bundle[1] if bundle is not None else []
        receipt_source_ids = bundle[2] if bundle is not None else [
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ]
        rows.append({
            **{
                field: str(item.get(field) or "").strip()
                for field in fields
            },
            "source_segment_ids": receipt_source_ids,
            "source_evidence_receipt_hash": (
                evidence_repository.content_hash(primary_receipt)
                if primary_receipt is not None else ""
            ),
            "source_evidence_receipts_hash": (
                evidence_repository.content_hash(receipt_list)
                if bundle is not None else ""
            ),
        })
    rows.sort(key=lambda item: json.dumps(
        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ))
    return {
        "version": _STRUCTURAL_IDENTITY_RECEIPT_VERSION,
        "rows": rows,
        "hash": evidence_repository.content_hash(rows),
    }


def _structural_identity_resolution_receipt_is_valid(value: object) -> bool:
    return bool(
        isinstance(value, dict)
        and value.get("version") == _STRUCTURAL_IDENTITY_RECEIPT_VERSION
        and isinstance(value.get("rows"), list)
        and str(value.get("hash") or "")
        == evidence_repository.content_hash(value["rows"])
    )


def _replace_resolved_label(text: str, source_label: str, canonical_name: str) -> str:
    if not text or source_label == canonical_name:
        return text
    # Identity normalization can run at several durable pipeline boundaries
    # (candidate, normalized working copy, approved publication).  Preserve an
    # already-canonical occurrence before matching its source alias so mappings
    # such as ``美 -> 卢美`` cannot grow another ``卢`` on every pass.
    prefix, separator, suffix = canonical_name.partition(source_label)
    if separator:
        if prefix and suffix:
            repeated = (
                rf"(?:{re.escape(prefix)}){{2,}}"
                rf"{re.escape(source_label)}"
                rf"(?:{re.escape(suffix)}){{2,}}"
            )
            text = re.sub(repeated, canonical_name, text)
        elif prefix:
            text = re.sub(
                rf"(?:{re.escape(prefix)}){{2,}}{re.escape(source_label)}",
                canonical_name,
                text,
            )
        elif suffix:
            text = re.sub(
                rf"{re.escape(source_label)}(?:{re.escape(suffix)}){{2,}}",
                canonical_name,
                text,
            )
    pattern = re.compile(
        rf"{re.escape(canonical_name)}|{re.escape(source_label)}"
    )
    return pattern.sub(
        lambda match: (
            canonical_name
            if match.group(0) == source_label
            else match.group(0)
        ),
        text,
    )


_IDENTITY_LIST_SEPARATOR_PATTERN = re.compile(
    r"([、，,／/；;｜|＆&＋+\s]+)"
)


def _identity_source_label_has_list_separator(value: str) -> bool:
    """True if ``value`` contains a char the identity-list grammar splits on.

    这是 source_label / 未来揭示真名的真正业务约束（见
    ``IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH`` 旁的说明）：长度只是不精确
    的代理，混入 ``_IDENTITY_LIST_SEPARATOR_PATTERN`` 命中的分隔符或空白才会
    让下游身份列表被错误切分。
    """
    return _IDENTITY_LIST_SEPARATOR_PATTERN.search(str(value or "")) is not None


def _project_identity_token(
    token: str,
    source_label: str,
    canonical_name: str,
) -> str:
    """Project one complete identity token through durable authority.

    ``plot_spine.who`` is a structured identity carrier, not prose.  Alias
    decisions therefore apply only to a complete token.  The expansion branch
    is a compatibility migration for artifacts produced by the former
    substring replacement; its shape is derived from this exact authority
    mapping rather than from any vocabulary list.
    """
    value = str(token or "").strip()
    if not value or source_label == canonical_name:
        return value
    if value == source_label or value == canonical_name:
        return canonical_name

    prefix, separator, suffix = canonical_name.partition(source_label)
    if not separator:
        return value
    if prefix and suffix:
        repeated = re.fullmatch(
            rf"(?:{re.escape(prefix)}){{2,}}"
            rf"{re.escape(source_label)}"
            rf"(?:{re.escape(suffix)}){{2,}}",
            value,
        )
    elif prefix:
        repeated = re.fullmatch(
            rf"(?:{re.escape(prefix)}){{2,}}{re.escape(source_label)}",
            value,
        )
    elif suffix:
        repeated = re.fullmatch(
            rf"{re.escape(source_label)}(?:{re.escape(suffix)}){{2,}}",
            value,
        )
    else:
        repeated = None
    return canonical_name if repeated is not None else value


def _identity_list_tokens(value: str) -> list[str]:
    """Return complete identities from the structured ``who`` grammar."""
    return [
        part.strip()
        for part in _IDENTITY_LIST_SEPARATOR_PATTERN.split(str(value or ""))
        if part.strip()
        and _IDENTITY_LIST_SEPARATOR_PATTERN.fullmatch(part) is None
    ]


def _replace_identity_list_label(
    value: str,
    source_label: str,
    canonical_name: str,
) -> str:
    """Apply one authority decision to exact ``who`` identity tokens."""
    parts = _IDENTITY_LIST_SEPARATOR_PATTERN.split(str(value or ""))
    return "".join(
        part
        if _IDENTITY_LIST_SEPARATOR_PATTERN.fullmatch(part or "") is not None
        else _project_identity_token(part, source_label, canonical_name)
        for part in parts
    )


def _replace_screenplay_body_label(
    text: str,
    source_label: str,
    canonical_name: str,
    *,
    replace_prose: bool = True,
    replace_speaker: bool = True,
) -> str:
    """改剧本正文中的角色身份，不改其他角色说出的台词内容。"""
    lines: list[str] = []
    speaker_pattern = re.compile(
        rf"^(?P<indent>\s*){re.escape(source_label)}(?P<emotion>[\(（][^\)）]{{0,16}}[\)）])?(?P<colon>[:：])"
    )
    any_dialogue_pattern = re.compile(
        r"^\s*[\u3400-\u9fffA-Za-z0-9_·•・·-]{1,16}(?:[\(（][^\)）]{0,16}[\)）])?[:：]"
    )
    for line in (text or "").splitlines(keepends=True):
        if replace_speaker and speaker_pattern.match(line):
            line = speaker_pattern.sub(
                lambda match: (
                    f"{match.group('indent')}{canonical_name}"
                    f"{match.group('emotion') or ''}{match.group('colon')}"
                ),
                line,
                count=1,
            )
        elif replace_prose and not any_dialogue_pattern.match(line):
            line = _replace_resolved_label(line, source_label, canonical_name)
        lines.append(line)
    return "".join(lines)


def _restore_non_dialogue_prefix(
    text: str,
    source_label: str,
    canonical_name: str,
    *,
    authoritative_lines: set[str],
) -> str:
    """Restore a structural prefix previously mistaken for a speaker."""
    prefix = re.compile(
        rf"^(?P<indent>\s*){re.escape(canonical_name)}(?P<colon>[:：])"
        r"(?P<line>.*)$"
    )
    lines: list[str] = []
    for raw_line in (text or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        ending = raw_line[len(line):]
        match = prefix.match(line)
        if (
            match is not None
            and match.group("line").strip() not in authoritative_lines
        ):
            line = (
                f"{match.group('indent')}{source_label}"
                f"{match.group('colon')}{match.group('line')}"
            )
        lines.append(line + ending)
    return "".join(lines)


def _replace_identity_value(value, source_label: str, canonical_name: str):
    """Replace exact identity values recursively without touching source spans."""
    if isinstance(value, str):
        return canonical_name if value == source_label else value
    if isinstance(value, list):
        return [
            _replace_identity_value(item, source_label, canonical_name)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _replace_identity_value(item, source_label, canonical_name)
            for item in value
        )
    if isinstance(value, dict):
        return {
            (
                canonical_name if str(key) == source_label else key
            ): _replace_identity_value(item, source_label, canonical_name)
            for key, item in value.items()
        }
    return value


def _identity_value_contains(value, identity: str) -> bool:
    if isinstance(value, str):
        return value == identity
    if isinstance(value, (list, tuple)):
        return any(_identity_value_contains(item, identity) for item in value)
    if isinstance(value, dict):
        return any(
            str(key) == identity or _identity_value_contains(item, identity)
            for key, item in value.items()
        )
    return False


def _replace_narrative_plan_identity(
    plan,
    source_label: str,
    canonical_name: str,
    *,
    replace_display_text: bool = True,
) -> bool:
    """Atomically update every authoritative entity reference in one plan.

    SourceEvidence and direct source excerpts remain immutable.  The mapping is
    AI/project supplied; this routine validates no role vocabulary and merely
    applies one resolved identity consistently across the relation graph.
    """
    if plan is None:
        return False
    before = plan.model_dump(mode="json")

    for contract in plan.identity_contracts:
        if replace_display_text and contract.display_name == source_label:
            contract.display_name = canonical_name
        contract.voice_ids = list(dict.fromkeys(
            canonical_name if voice_id == source_label else voice_id
            for voice_id in contract.voice_ids
        ))
        if replace_display_text:
            contract.evidence.rationale = _replace_resolved_label(
                contract.evidence.rationale, source_label, canonical_name,
            )
    for proposition in plan.propositions:
        proposition.entity_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in proposition.entity_ids
        ))
        if replace_display_text:
            proposition.canonical_statement = _replace_resolved_label(
                proposition.canonical_statement, source_label, canonical_name,
            )
    for fact in plan.state_facts:
        if fact.subject_id == source_label:
            fact.subject_id = canonical_name
        fact.value.data = _replace_identity_value(
            fact.value.data, source_label, canonical_name,
        )
    for evidence in plan.evidence:
        evidence.perceivable_by = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in evidence.perceivable_by
        ))
        if replace_display_text:
            evidence.observable_claim = _replace_resolved_label(
                evidence.observable_claim, source_label, canonical_name,
            )
        evidence.competing_attention_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in evidence.competing_attention_ids
        ))
    for question in plan.dramatic_questions:
        if replace_display_text:
            question.question_text = _replace_resolved_label(
                question.question_text, source_label, canonical_name,
            )
    for action in plan.atomic_actions:
        action.actor_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in action.actor_ids
        ))
        action.target_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in action.target_ids
        ))
        if replace_display_text:
            for field in ("semantic_intent", "completion_condition", "decision_not_applicable_reason"):
                value = getattr(action, field, None)
                if isinstance(value, str):
                    setattr(action, field, _replace_resolved_label(value, source_label, canonical_name))
            for phase in action.temporal_phases:
                phase.start_condition = _replace_resolved_label(
                    phase.start_condition, source_label, canonical_name,
                )
                phase.end_condition = _replace_resolved_label(
                    phase.end_condition, source_label, canonical_name,
                )
    for event in plan.events:
        event.character_goal_effects = _replace_identity_value(
            event.character_goal_effects, source_label, canonical_name,
        )
    for state in plan.character_states:
        if state.character_id == source_label:
            state.character_id = canonical_name
        state.relationship_state = _replace_identity_value(
            state.relationship_state, source_label, canonical_name,
        )
        state.emotion = _replace_identity_value(
            state.emotion, source_label, canonical_name,
        )
        if replace_display_text:
            state.tactic = _replace_resolved_label(
                state.tactic, source_label, canonical_name,
            )
    for belief in plan.character_beliefs:
        if belief.character_id == source_label:
            belief.character_id = canonical_name
    for prior in plan.audience_priors:
        if replace_display_text:
            prior.audience_description = _replace_resolved_label(
                prior.audience_description, source_label, canonical_name,
            )
        prior.familiarity_assumptions = _replace_identity_value(
            prior.familiarity_assumptions, source_label, canonical_name,
        )
    for state in plan.audience_states:
        for field in (
            "causal_hypotheses",
            "character_goal_hypotheses",
            "spatial_model",
            "temporal_model",
            "working_memory",
            "affective_state",
        ):
            setattr(
                state,
                field,
                _replace_identity_value(
                    getattr(state, field), source_label, canonical_name,
                ),
            )
        state.attention_residue_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in state.attention_residue_ids
        ))
    for intent in plan.experience_intents:
        intent.attention_target_ids = list(dict.fromkeys(
            canonical_name if entity_id == source_label else entity_id
            for entity_id in intent.attention_target_ids
        ))
        if replace_display_text:
            intent.director_objective = _replace_resolved_label(
                intent.director_objective, source_label, canonical_name,
            )
            intent.forbidden_misconceptions = [
                _replace_resolved_label(value, source_label, canonical_name)
                for value in intent.forbidden_misconceptions
            ]
    for scene in plan.scene_contracts:
        if scene.point_of_view_character_id == source_label:
            scene.point_of_view_character_id = canonical_name
        scene.relationship_deltas = _replace_identity_value(
            scene.relationship_deltas, source_label, canonical_name,
        )
        if replace_display_text:
            for field in (
                "not_applicable_reason",
                "alternative_dramatic_function",
                "value_polarity_in",
                "value_polarity_out",
                "scene_button",
            ):
                value = getattr(scene, field, None)
                if isinstance(value, str):
                    setattr(scene, field, _replace_resolved_label(value, source_label, canonical_name))
    for arc in plan.arc_contracts:
        if replace_display_text:
            for field in ("not_applicable_reason", "alternative_dramatic_function"):
                value = getattr(arc, field, None)
                if isinstance(value, str):
                    setattr(arc, field, _replace_resolved_label(value, source_label, canonical_name))
        arc.pressure_curve = _replace_identity_value(
            arc.pressure_curve, source_label, canonical_name,
        )
        arc.information_density_curve = _replace_identity_value(
            arc.information_density_curve, source_label, canonical_name,
        )
        arc.processing_beats = _replace_identity_value(
            arc.processing_beats, source_label, canonical_name,
        )
    return plan.model_dump(mode="json") != before


def _merge_duplicate_narrative_identity_contracts(plan) -> list[dict]:
    """Merge aliases that resolve to one canonical display identity."""
    if plan is None:
        return []
    data = plan.model_dump(mode="json")
    contracts = list(data.get("identity_contracts") or [])
    groups: dict[str, list[tuple[int, dict]]] = {}
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            continue
        display_name = str(contract.get("display_name") or "").strip()
        if display_name:
            groups.setdefault(display_name, []).append((index, contract))

    replacements: dict[str, str] = {}
    merged_by_display: dict[str, dict] = {}
    changes: list[dict] = []
    for display_name, members in groups.items():
        if len(members) < 2:
            continue
        _canonical_index, canonical = max(
            members,
            key=lambda item: (
                int(str(item[1].get("identity_id") or "") == display_name),
                int(str(item[1].get("visual_policy") or "") == "canonical"),
                int(str(item[1].get("asset_requirement") or "") == "required"),
                -item[0],
            ),
        )
        canonical_id = str(canonical.get("identity_id") or "").strip()
        if not canonical_id:
            continue
        merged = dict(canonical)
        merged_evidence = dict(merged.get("evidence") or {})
        merged_voice_ids = list(merged.get("voice_ids") or [])
        rationales = [str(merged_evidence.get("rationale") or "").strip()]
        merged_ids: list[str] = []
        for _index, contract in members:
            identity_id = str(contract.get("identity_id") or "").strip()
            if identity_id and identity_id != canonical_id:
                replacements[identity_id] = canonical_id
                merged_ids.append(identity_id)
            merged_voice_ids.extend(contract.get("voice_ids") or [])
            evidence = contract.get("evidence") or {}
            for field in (
                "source_evidence_ids",
                "proposition_ids",
                "adaptation_decision_ids",
            ):
                merged_evidence[field] = list(dict.fromkeys([
                    *(merged_evidence.get(field) or []),
                    *(evidence.get(field) or []),
                ]))
            rationale = str(evidence.get("rationale") or "").strip()
            if rationale:
                rationales.append(rationale)
        merged["voice_ids"] = list(dict.fromkeys(merged_voice_ids))
        merged_evidence["rationale"] = "；".join(dict.fromkeys(filter(
            None,
            rationales,
        )))
        merged["evidence"] = merged_evidence
        merged_by_display[display_name] = merged
        changes.append({
            "kind": "identity_contract_merge",
            "display_name": display_name,
            "canonical_identity_id": canonical_id,
            "merged_identity_ids": merged_ids,
        })

    if not replacements:
        return []

    def replace_merged_ids(value):
        if isinstance(value, str):
            return replacements.get(value, value)
        if isinstance(value, list):
            replaced = [replace_merged_ids(item) for item in value]
            if not any(
                isinstance(item, str) and item in replacements
                for item in value
            ):
                return replaced
            deduplicated: list = []
            seen_strings: set[str] = set()
            for item in replaced:
                if isinstance(item, str):
                    if item in seen_strings:
                        continue
                    seen_strings.add(item)
                deduplicated.append(item)
            return deduplicated
        if isinstance(value, tuple):
            return tuple(replace_merged_ids(item) for item in value)
        if isinstance(value, dict):
            return {
                replacements.get(str(key), key): replace_merged_ids(item)
                for key, item in value.items()
            }
        return value

    data = replace_merged_ids(data)

    retained_contracts: list[dict] = []
    emitted_displays: set[str] = set()
    for contract in contracts:
        display_name = str(contract.get("display_name") or "").strip()
        merged = merged_by_display.get(display_name)
        if merged is not None:
            if display_name in emitted_displays:
                continue
            normalized = replace_merged_ids(merged)
            retained_contracts.append(normalized)
            emitted_displays.add(display_name)
            continue
        normalized = replace_merged_ids(contract)
        retained_contracts.append(normalized)
    data["identity_contracts"] = retained_contracts

    rebuilt = type(plan).model_validate(data)
    for field in type(plan).model_fields:
        setattr(plan, field, getattr(rebuilt, field))
    return changes


def apply_screenplay_character_resolutions(screenplay, resolutions: list[dict] | None) -> list[dict]:
    """在剧本进入 QA/发布之前原子性落实人物身份映射。

    原文证据字段（source_text/source_basis/source_fact/source_span）保持不变，
    避免破坏逐字证据；所有会被下游当成角色身份的字段统一改名。
    """
    changes: list[dict] = []
    authoritative_speakers = {
        str(turn.speaker or "").strip()
        for chain in getattr(screenplay, "dialogue_chains", None) or []
        for turn in chain.turns or []
        if str(turn.speaker or "").strip()
    }
    authoritative_lines_by_speaker: dict[str, set[str]] = {}
    for chain in getattr(screenplay, "dialogue_chains", None) or []:
        for turn in chain.turns or []:
            speaker = str(turn.speaker or "").strip()
            line = str(turn.line or "").strip()
            if speaker and line:
                authoritative_lines_by_speaker.setdefault(
                    speaker,
                    set(),
                ).add(line)
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        # Occurrence-scoped identity decisions can legitimately share one
        # source label (for example two people both called “绿袍修士”).  Their
        # authority_id is already bound inside the IR, so a global text
        # replacement here would arbitrarily assign every occurrence to the
        # first entity and corrupt the compiled identity graph.
        if str(item.get("source_instance_key") or "").strip():
            continue
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not source_label or not canonical_name or source_label == canonical_name:
            continue
        replace_display_text = item.get("resolution") != "future_identity"

        changed = False
        for scene in getattr(screenplay, "scene_outline", None) or []:
            before = list(scene.characters or [])
            scene.characters = list(dict.fromkeys(
                canonical_name if name == source_label else name
                for name in before
            ))
            changed = changed or scene.characters != before
            if replace_display_text:
                for field in ("story_function", "summary", "conflict", "turn"):
                    value = getattr(scene, field, "") or ""
                    replaced = _replace_resolved_label(value, source_label, canonical_name)
                    if replaced != value:
                        setattr(scene, field, replaced)
                        changed = True

        body = getattr(screenplay, "full_script_text", "") or ""
        replaced_body = _replace_screenplay_body_label(
            body,
            source_label,
            canonical_name,
            replace_prose=replace_display_text,
            replace_speaker=source_label in authoritative_speakers,
        )
        if source_label not in authoritative_speakers:
            replaced_body = _restore_non_dialogue_prefix(
                replaced_body,
                source_label,
                canonical_name,
                authoritative_lines=authoritative_lines_by_speaker.get(
                    canonical_name,
                    set(),
                ),
            )
        if replaced_body != body:
            screenplay.full_script_text = replaced_body
            changed = True

        spine = getattr(screenplay, "plot_spine", None)
        if spine is not None:
            for beat in spine.spine_beats or []:
                for field in (
                    ("who", "does", "turn")
                    if replace_display_text
                    else ("who",)
                ):
                    value = getattr(beat, field, "") or ""
                    replaced = (
                        _replace_identity_list_label(
                            value,
                            source_label,
                            canonical_name,
                        )
                        if field == "who"
                        else _replace_resolved_label(
                            value,
                            source_label,
                            canonical_name,
                        )
                    )
                    if replaced != value:
                        setattr(beat, field, replaced)
                        changed = True

        for chain in getattr(screenplay, "dialogue_chains", None) or []:
            for turn in chain.turns or []:
                if (turn.speaker or "").strip() == source_label:
                    turn.speaker = canonical_name
                    changed = True

        for event in getattr(screenplay, "events", None) or []:
            if replace_display_text:
                for field in ("state_in", "trigger", "visible_change", "state_out", "adaptation_reason"):
                    value = getattr(event, field, "") or ""
                    replaced = _replace_resolved_label(value, source_label, canonical_name)
                    if replaced != value:
                        setattr(event, field, replaced)
                        changed = True

        for info in getattr(screenplay, "information_ledger", None) or []:
            if (info.speaker_id or "").strip() == source_label:
                info.speaker_id = canonical_name
                changed = True
            if replace_display_text:
                content = info.content or ""
                replaced = _replace_resolved_label(content, source_label, canonical_name)
                if replaced != content:
                    info.content = replaced
                    changed = True

        for voice in getattr(screenplay, "voice_bible", None) or []:
            if (voice.speaker_id or "").strip() == source_label:
                voice.speaker_id = canonical_name
                if getattr(screenplay, "narrative_plan", None) is not None:
                    if (
                        resolution_declares_functional_identity(item)
                        and str(voice.role_type or "").strip() != "narrator"
                    ):
                        voice.role_type = "functional_character"
                elif resolution_declares_functional_identity(item):
                    voice.role_type = "functional_character"
                changed = True

        changed = _replace_narrative_plan_identity(
            getattr(screenplay, "narrative_plan", None),
            source_label,
            canonical_name,
            replace_display_text=replace_display_text,
        ) or changed

        if replace_display_text:
            for field in (
                "logline", "dramatic_question", "protagonist_goal", "obstacle", "stakes",
                "emotional_curve", "ending_hook", "adaptation_direction", "opening", "development",
                "conflict", "climax", "episode_premise",
            ):
                value = getattr(screenplay, field, "") or ""
                replaced = _replace_resolved_label(value, source_label, canonical_name)
                if replaced != value:
                    setattr(screenplay, field, replaced)
                    changed = True
            for field in (
                "key_lines", "key_plot_points", "character_state_changes",
                "approved_adaptations", "forbidden_additions",
            ):
                values = list(getattr(screenplay, field, None) or [])
                replaced_values = [
                    _replace_resolved_label(value, source_label, canonical_name)
                    for value in values
                ]
                if replaced_values != values:
                    setattr(screenplay, field, replaced_values)
                    changed = True

        if changed:
            changes.append({
                "source_label": source_label,
                "canonical_name": canonical_name,
                "resolution": item.get("resolution") or "unknown",
            })
    changes.extend(_merge_duplicate_narrative_identity_contracts(
        getattr(screenplay, "narrative_plan", None),
    ))
    return changes


def normalize_screenplay_identity_annotations(screenplay, bible: Bible) -> list[dict]:
    """Strip carrier annotations only when the base is already authoritative.

    Identity fields may contain presentation notes such as ``角色（画外）``.
    This normalization never interprets the note or classifies role names. It
    only projects an exact Bible/contract/voice token back to its canonical
    display name; ambiguous or unknown bases remain unresolved for model audit.
    """
    visual_targets: dict[str, set[str]] = {}
    voice_targets: dict[str, set[str]] = {}

    def register(targets: dict[str, set[str]], token: object, canonical: str) -> None:
        value = str(token or "").strip()
        if value and canonical:
            targets.setdefault(value, set()).add(canonical)

    for character in bible.characters:
        name = str(character.name or "").strip()
        register(visual_targets, name, name)
        register(voice_targets, name, name)

    plan = getattr(screenplay, "narrative_plan", None)
    for contract in (getattr(plan, "identity_contracts", None) or []):
        canonical = str(contract.display_name or "").strip()
        if str(contract.visual_policy or "").strip() != "offscreen_only":
            register(visual_targets, contract.identity_id, canonical)
            register(visual_targets, contract.display_name, canonical)
        for voice_id in contract.voice_ids or []:
            register(voice_targets, voice_id, canonical)

    for voice in getattr(screenplay, "voice_bible", None) or []:
        if str(voice.role_type or "").strip() == "narrator":
            speaker_id = str(voice.speaker_id or "").strip()
            register(voice_targets, speaker_id, speaker_id)

    usages: dict[str, set[str]] = {}

    def collect(raw: object, usage: str) -> None:
        value = str(raw or "").strip()
        if _identity_carrier_annotation_base(value):
            usages.setdefault(value, set()).add(usage)

    for scene in getattr(screenplay, "scene_outline", None) or []:
        for character in scene.characters or []:
            collect(character, "visual")
    for chain in getattr(screenplay, "dialogue_chains", None) or []:
        for turn in chain.turns or []:
            collect(turn.speaker, "voice")
    for item in getattr(screenplay, "information_ledger", None) or []:
        collect(item.speaker_id, "voice")
    for voice in getattr(screenplay, "voice_bible", None) or []:
        collect(voice.speaker_id, "voice")
    from app.validators import screenplay_speaker_names
    for speaker in screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or ""
    ):
        collect(speaker, "voice")

    resolutions: list[dict] = []
    target_maps = {"visual": visual_targets, "voice": voice_targets}
    for source_label, required_usages in usages.items():
        base = _identity_carrier_annotation_base(source_label)
        candidates: set[str] | None = None
        for usage in required_usages:
            current = target_maps[usage].get(base, set())
            candidates = set(current) if candidates is None else candidates & current
        if candidates and len(candidates) == 1:
            resolutions.append({
                "source_label": source_label,
                "canonical_name": next(iter(candidates)),
                "resolution": "authority_annotation",
            })
    if not resolutions:
        return []
    return apply_screenplay_character_resolutions(screenplay, resolutions)


def normalize_screenplay_offscreen_visual_identities(screenplay) -> list[dict]:
    """Remove typed offscreen-only identities from visual scene membership."""
    plan = getattr(screenplay, "narrative_plan", None)
    if plan is None:
        return []
    offscreen_tokens = {
        token
        for contract in plan.identity_contracts
        if str(contract.visual_policy or "").strip() == "offscreen_only"
        for token in {
            str(contract.identity_id or "").strip(),
            str(contract.display_name or "").strip(),
            *(
                str(voice_id or "").strip()
                for voice_id in (contract.voice_ids or [])
            ),
        }
        if token
    }
    if not offscreen_tokens:
        return []

    changes: list[dict] = []
    for scene in getattr(screenplay, "scene_outline", None) or []:
        before = list(scene.characters or [])
        scene.characters = [
            identity for identity in before
            if str(identity or "").strip() not in offscreen_tokens
        ]
        removed = [
            identity for identity in before
            if str(identity or "").strip() in offscreen_tokens
        ]
        if removed:
            changes.append({
                "source_label": ",".join(str(value) for value in removed),
                "canonical_name": "",
                "resolution": "offscreen_visual_membership_removed",
                "scene_no": scene.scene_no,
            })
    return changes


def normalize_screenplay_voice_ids(screenplay, bible: Bible) -> list[dict]:
    """Normalize voice aliases and remove unreferenced non-identity entries.

    New prompts require Bible character names as speaker IDs.  This migration
    path handles existing working artifacts without guessing from initials or
    role labels: the alias must own ledger text that names exactly one Bible
    character, and that character must actually speak in the screenplay.
    Ambiguous or referenced aliases remain untouched so the identity gate still
    fails closed. Unbound entries that no spoken field references are dead
    metadata, not identities, and are removed without inspecting their names or
    role labels.
    """
    changes = normalize_screenplay_identity_annotations(screenplay, bible)
    plan = getattr(screenplay, "narrative_plan", None)
    if plan is None:
        return changes
    bible_names = {
        str(character.name or "").strip()
        for character in bible.characters
        if str(character.name or "").strip()
    }
    for voice in getattr(screenplay, "voice_bible", None) or []:
        speaker_id = str(voice.speaker_id or "").strip()
        role_type = str(voice.role_type or "").strip()
        if not speaker_id:
            continue
        matching_contracts = [
            contract
            for contract in plan.identity_contracts
            if (
                speaker_id in {
                    str(contract.identity_id or "").strip(),
                    str(contract.display_name or "").strip(),
                }
                and (
                    role_type != "narrator"
                    or str(contract.visual_policy or "").strip()
                    == "offscreen_only"
                )
            )
        ]
        if len(matching_contracts) != 1:
            continue
        contract = matching_contracts[0]
        before = list(contract.voice_ids or [])
        if speaker_id not in before:
            contract.voice_ids = [*before, speaker_id]
            changes.append({
                "source_label": speaker_id,
                "canonical_name": speaker_id,
                "resolution": (
                    "narrator_voice_contract_bound"
                    if role_type == "narrator"
                    else "voice_contract_bound"
                ),
            })
    explicitly_bound = {
        str(voice_id or "").strip()
        for contract in plan.identity_contracts
        for voice_id in contract.voice_ids
        if str(voice_id or "").strip()
    }
    from app.validators import screenplay_speaker_names

    dialogue_speakers = {
        str(turn.speaker or "").strip()
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip()
    }
    dialogue_speakers.update(screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or "",
    ))
    dialogue_turns = [
        (
            str(turn.speaker or "").strip(),
            str(turn.line or "").strip(),
        )
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip() and str(turn.line or "").strip()
    ]

    def alias_candidate(ledger_items) -> str | None:
        ledger_text = "\n".join(
            f"{item.content or ''}\n{item.exact_text or ''}"
            for item in ledger_items
        )
        exact_texts = {
            str(item.exact_text or "").strip()
            for item in ledger_items
            if str(item.exact_text or "").strip()
        }
        exact_speakers = {
            speaker
            for speaker, line in dialogue_turns
            for exact_text in exact_texts
            if (
                speaker in bible_names
                and (exact_text == line or exact_text in line or line in exact_text)
            )
        }
        mentioned_candidates = {
            name
            for name in bible_names
            if name in dialogue_speakers and name in ledger_text
        }
        leading_candidates = {
            name
            for name in mentioned_candidates
            if any(
                str(item.content or "").strip().startswith(name)
                for item in ledger_items
            )
        }
        candidates = (
            exact_speakers
            if len(exact_speakers) == 1
            else mentioned_candidates
            if len(mentioned_candidates) == 1
            else leading_candidates
        )
        return next(iter(candidates)) if len(candidates) == 1 else None

    voice_delivery_owners = {"spoken_dialogue", "offscreen_voice", "narration"}
    non_voice_carriers: set[str] = set()
    for voice in getattr(screenplay, "voice_bible", None) or []:
        source_id = str(voice.speaker_id or "").strip()
        if (
            not source_id
            or str(voice.role_type or "").strip() == "narrator"
            or source_id in bible_names
            or source_id in explicitly_bound
        ):
            continue
        ledger_items = [
            item
            for item in (getattr(screenplay, "information_ledger", None) or [])
            if str(item.speaker_id or "").strip() == source_id
        ]
        if alias_candidate(ledger_items):
            continue
        if ledger_items and all(
            str(item.delivery_owner or "").strip() not in voice_delivery_owners
            for item in ledger_items
        ):
            non_voice_carriers.add(source_id)

    if non_voice_carriers:
        for item in getattr(screenplay, "information_ledger", None) or []:
            if str(item.speaker_id or "").strip() in non_voice_carriers:
                item.speaker_id = None
        for chain in getattr(screenplay, "dialogue_chains", None) or []:
            chain.turns = [
                turn for turn in (chain.turns or [])
                if str(turn.speaker or "").strip() not in non_voice_carriers
            ]
        screenplay.dialogue_chains = [
            chain for chain in (getattr(screenplay, "dialogue_chains", None) or [])
            if chain.turns
        ]
        retained_key_lines: list[str] = []
        for line in getattr(screenplay, "key_lines", None) or []:
            speaker, separator, _ = str(line or "").partition("：")
            if not separator:
                speaker, separator, _ = str(line or "").partition(":")
            if separator and speaker.strip() in non_voice_carriers:
                continue
            retained_key_lines.append(line)
        screenplay.key_lines = retained_key_lines
        body = getattr(screenplay, "full_script_text", "") or ""
        for source_id in sorted(non_voice_carriers):
            body = re.sub(
                rf"(?m)^(\s*){re.escape(source_id)}"
                r"(?:[\(（][^\)）]{0,16}[\)）])?\s*[:：]\s*(.*)$",
                lambda match: f"{match.group(1)}【{match.group(2).strip()}】",
                body,
            )
        screenplay.full_script_text = body
        screenplay.voice_bible = [
            voice
            for voice in (getattr(screenplay, "voice_bible", None) or [])
            if str(voice.speaker_id or "").strip() not in non_voice_carriers
        ]
        non_voice_changes = [{
            "source_label": source_id,
            "canonical_name": "",
            "resolution": "non_voice_carrier_removed",
        } for source_id in sorted(non_voice_carriers)]
    else:
        non_voice_changes = []

    dialogue_speakers = {
        str(turn.speaker or "").strip()
        for chain in (getattr(screenplay, "dialogue_chains", None) or [])
        for turn in (chain.turns or [])
        if str(turn.speaker or "").strip()
    }
    dialogue_speakers.update(screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or "",
    ))
    ledger_speakers = {
        str(item.speaker_id or "").strip()
        for item in (getattr(screenplay, "information_ledger", None) or [])
        if str(item.speaker_id or "").strip()
    }
    referenced_speakers = dialogue_speakers | ledger_speakers
    existing_voice_ids = {
        str(voice.speaker_id or "").strip()
        for voice in (getattr(screenplay, "voice_bible", None) or [])
        if str(voice.speaker_id or "").strip()
    }
    unreferenced_voice_ids: set[str] = set()

    for voice in getattr(screenplay, "voice_bible", None) or []:
        source_id = str(voice.speaker_id or "").strip()
        if (
            not source_id
            or str(voice.role_type or "").strip() == "narrator"
            or source_id in bible_names
            or source_id in explicitly_bound
        ):
            continue
        ledger_items = [
            item
            for item in (getattr(screenplay, "information_ledger", None) or [])
            if str(item.speaker_id or "").strip() == source_id
        ]
        if not ledger_items:
            if source_id not in referenced_speakers:
                unreferenced_voice_ids.add(source_id)
            continue
        canonical_name = alias_candidate(ledger_items)
        if not canonical_name:
            continue
        if canonical_name in existing_voice_ids:
            continue

        voice.speaker_id = canonical_name
        for item in ledger_items:
            item.speaker_id = canonical_name
        existing_voice_ids.discard(source_id)
        existing_voice_ids.add(canonical_name)
        changes.append({
            "source_label": source_id,
            "canonical_name": canonical_name,
            "resolution": "voice_alias_from_ledger",
        })

    changes.extend(non_voice_changes)
    if unreferenced_voice_ids:
        screenplay.voice_bible = [
            voice
            for voice in (getattr(screenplay, "voice_bible", None) or [])
            if str(voice.speaker_id or "").strip() not in unreferenced_voice_ids
        ]
        changes.extend({
            "source_label": source_id,
            "canonical_name": "",
            "resolution": "unreferenced_voice_removed",
        } for source_id in sorted(unreferenced_voice_ids))

    changes.extend(normalize_screenplay_offscreen_visual_identities(screenplay))
    return changes


def screenplay_character_resolution_errors(screenplay, resolutions: list[dict] | None) -> list[str]:
    """剧本发布前硬门禁：过渡称谓不得再占据任何角色身份位。"""
    errors: list[str] = []
    for item in resolutions or []:
        if not isinstance(item, dict):
            continue
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("canonical_name") or "").strip()
        if not source_label or not canonical_name or source_label == canonical_name:
            continue
        preserves_current_display = item.get("resolution") == "future_identity"
        residual_paths: list[str] = []
        for scene in getattr(screenplay, "scene_outline", None) or []:
            if source_label in (scene.characters or []):
                residual_paths.append(f"scene_outline[{scene.scene_no}].characters")
        spine = getattr(screenplay, "plot_spine", None)
        for beat_index, beat in enumerate(
            (spine.spine_beats if spine is not None else None) or []
        ):
            for token in _identity_list_tokens(beat.who):
                projected = _project_identity_token(
                    token,
                    source_label,
                    canonical_name,
                )
                if token == source_label or projected != token:
                    residual_paths.append(
                        f"plot_spine.spine_beats[{beat_index}].who[{token}]"
                    )
        for chain_index, chain in enumerate(getattr(screenplay, "dialogue_chains", None) or []):
            for turn_index, turn in enumerate(chain.turns or []):
                if (turn.speaker or "").strip() == source_label:
                    residual_paths.append(f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker")
        for index, info in enumerate(getattr(screenplay, "information_ledger", None) or []):
            if (info.speaker_id or "").strip() == source_label:
                residual_paths.append(f"information_ledger[{index}].speaker_id")
        for index, voice in enumerate(getattr(screenplay, "voice_bible", None) or []):
            if (voice.speaker_id or "").strip() == source_label:
                residual_paths.append(f"voice_bible[{index}].speaker_id")
        body = getattr(screenplay, "full_script_text", "") or ""
        speaker_pattern = re.compile(
            rf"(?m)^\s*{re.escape(source_label)}(?:[\(（][^\)）]{{0,16}}[\)）])?[:：]"
        )
        if not preserves_current_display and speaker_pattern.search(body):
            residual_paths.append("full_script_text.speaker")
        plan = getattr(screenplay, "narrative_plan", None)
        if plan is not None:
            for index, proposition in enumerate(plan.propositions):
                if source_label in proposition.entity_ids:
                    residual_paths.append(f"narrative_plan.propositions[{index}].entity_ids")
            for index, fact in enumerate(plan.state_facts):
                if fact.subject_id == source_label or _identity_value_contains(
                    fact.value.data, source_label,
                ):
                    residual_paths.append(f"narrative_plan.state_facts[{index}]")
            for index, evidence in enumerate(plan.evidence):
                if source_label in {
                    *evidence.perceivable_by,
                    *evidence.competing_attention_ids,
                }:
                    residual_paths.append(f"narrative_plan.evidence[{index}]")
            for index, action in enumerate(plan.atomic_actions):
                if source_label in {*action.actor_ids, *action.target_ids}:
                    residual_paths.append(f"narrative_plan.atomic_actions[{index}]")
            for index, state in enumerate(plan.character_states):
                if (
                    state.character_id == source_label
                    or _identity_value_contains(state.relationship_state, source_label)
                    or _identity_value_contains(state.emotion, source_label)
                ):
                    residual_paths.append(f"narrative_plan.character_states[{index}]")
            for index, belief in enumerate(plan.character_beliefs):
                if belief.character_id == source_label:
                    residual_paths.append(f"narrative_plan.character_beliefs[{index}]")
            for index, state in enumerate(plan.audience_states):
                if any(
                    _identity_value_contains(getattr(state, field), source_label)
                    for field in (
                        "causal_hypotheses",
                        "character_goal_hypotheses",
                        "spatial_model",
                        "temporal_model",
                        "working_memory",
                        "attention_residue_ids",
                        "affective_state",
                    )
                ):
                    residual_paths.append(f"narrative_plan.audience_states[{index}]")
            for index, intent in enumerate(plan.experience_intents):
                if source_label in intent.attention_target_ids:
                    residual_paths.append(f"narrative_plan.experience_intents[{index}]")
            for index, scene in enumerate(plan.scene_contracts):
                if (
                    scene.point_of_view_character_id == source_label
                    or _identity_value_contains(scene.relationship_deltas, source_label)
                ):
                    residual_paths.append(f"narrative_plan.scene_contracts[{index}]")
        if residual_paths:
            errors.append(
                f"角色身份预解析未落实：「{source_label}」必须在剧本阶段改为「{canonical_name}」；"
                f"残留位置：{', '.join(residual_paths[:8])}"
            )
    return errors


def screenplay_unknown_identity_errors(
    screenplay,
    bible: Bible,
    resolutions: list[dict] | None = None,
) -> list[str]:
    """确定性检查“模型判断是否已经落地”，不猜测称谓语义。"""
    bible_names = {character.name for character in bible.characters}
    narrative_plan = getattr(screenplay, "narrative_plan", None)
    narrative_authority = narrative_plan is not None
    if not bible_names and not narrative_authority:
        # 保留无真实人物谱项目的历史占位流程；有 Bible 时才启用身份硬门禁。
        return []
    resolver = None
    if narrative_authority:
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        try:
            resolver = narrative_identity_resolver(bible, screenplay)
        except IdentityContractError as exc:
            return [f"剧本身份合同无法解析：{exc}"]
    locations: dict[str, list[str]] = {}
    typed_functional_names = {
        str(item.get("canonical_name") or "").strip()
        for item in (resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }

    def collect(raw_name: str, path: str, *, usage: str) -> None:
        name = str(raw_name or "").strip()
        if not name:
            return
        if narrative_authority:
            try:
                resolver.resolve(name, usage=usage)
                return
            except IdentityContractError:
                pass
        elif name == "旁白" or name in bible_names:
            return
        elif name in typed_functional_names:
            return
        locations.setdefault(name, []).append(path)

    for scene_index, scene in enumerate(getattr(screenplay, "scene_outline", None) or []):
        for name in scene.characters or []:
            collect(name, f"scene_outline[{scene_index}].characters", usage="visual")
    # PlotSpineBeat.who is an event subject, not a visual-identity declaration.
    # It may carry a typed identity, prop, spatial boundary, or offscreen source.
    # Identity policy comes from the typed carriers above/below and the narrative
    # graph. Exact character resolutions still project into ``who`` and retain
    # their dedicated residual check in screenplay_character_resolution_errors.
    for chain_index, chain in enumerate(getattr(screenplay, "dialogue_chains", None) or []):
        for turn_index, turn in enumerate(chain.turns or []):
            collect(
                turn.speaker,
                f"dialogue_chains[{chain_index}].turns[{turn_index}].speaker",
                usage="voice",
            )
    for index, item in enumerate(getattr(screenplay, "information_ledger", None) or []):
        collect(item.speaker_id, f"information_ledger[{index}].speaker_id", usage="voice")
    # 与 validate_screenplay 共用同一台本解析器，避免把“地点：”“场景：”
    # 这类台本标签误当成人名。这里只检查模型决议是否落地，不猜称谓语义。
    from app.validators import screenplay_speaker_names
    for speaker in screenplay_speaker_names(
        getattr(screenplay, "full_script_text", "") or ""
    ):
        collect(speaker, "full_script_text.speaker", usage="voice")
    return [
        f"剧本人物身份未解决：「{name}」既不在人物谱，"
        + (
            "也未由本集 identity_contracts + voice_bible 定义可见/声音政策；"
            if narrative_authority
            else "也未被人物预检模型映射为一次性角色；"
        )
        + f"位置：{', '.join(paths[:8])}"
        for name, paths in locations.items()
    ]


def merge_screenplay_character_resolutions(
    existing: list[dict] | None,
    incoming: list[dict] | None,
) -> list[dict]:
    """合并模型决议：后续真名证据可升级早期路人降级，不反向覆盖。

    ``identity_group`` 是模型已经做出的同一实体决议。结构审计可能为该
    实体增加新的稳定句柄（例如“大青山被困少年1”），但这不能因为
    描述性 canonical_name 变化就签发第二个 authority。同组的功能身份
    因此稳定复用已有权威；只有更高优先级的真名证据可整组升级。
    同组出现两个不同真名时证据自相矛盾，必须失败，不做猜测归并。
    """
    priority = {
        "functional_extra": 0,
        "functional_identity": 1,
        "reference_identity": 2,
        "future_identity": 3,
    }
    normalized_existing = normalize_character_resolutions(existing)
    normalized_incoming = normalize_character_resolutions(incoming)

    # A group token is scoped to one discovery input.  A fresh owned-source
    # discovery retires functional rows carrying the same bare token from an
    # older or unscoped epoch instead of guessing that F1 still means the same
    # person after the source changed.
    incoming_scopes_by_group: dict[str, set[str]] = {}
    for item in normalized_incoming:
        group = str(item.get("identity_group") or "").strip()
        scope = str(item.get("identity_scope_fingerprint") or "").strip()
        if group and scope and str(item.get("resolution") or "") != "future_identity":
            incoming_scopes_by_group.setdefault(group, set()).add(scope)
    normalized_existing = [
        item
        for item in normalized_existing
        if not (
            str(item.get("resolution") or "") != "future_identity"
            and str(item.get("identity_group") or "").strip()
            in incoming_scopes_by_group
            and str(item.get("identity_scope_fingerprint") or "").strip()
            not in incoming_scopes_by_group[
                str(item.get("identity_group") or "").strip()
            ]
        )
    ]

    def group_key(item: dict) -> tuple[str, str] | None:
        group = str(item.get("identity_group") or "").strip()
        if not group:
            return None
        return (
            str(item.get("identity_scope_fingerprint") or "").strip(),
            group,
        )

    existing_by_group: dict[tuple[str, str], list[dict]] = {}
    incoming_by_group: dict[tuple[str, str], list[dict]] = {}
    for item in normalized_existing:
        if (key := group_key(item)) is not None:
            existing_by_group.setdefault(key, []).append(item)
    for item in normalized_incoming:
        if (key := group_key(item)) is not None:
            incoming_by_group.setdefault(key, []).append(item)

    def top_authorities(items: list[dict]) -> tuple[int, dict[tuple[str, str], dict]]:
        top_priority = max(
            (priority.get(str(item.get("resolution") or ""), 0) for item in items),
            default=-1,
        )
        choices = {
            (item["canonical_name"], item["authority_id"]): item
            for item in items
            if priority.get(str(item.get("resolution") or ""), 0) == top_priority
        }
        return top_priority, choices

    group_authorities: dict[tuple[str, str], dict] = {}
    for key in set(existing_by_group) | set(incoming_by_group):
        existing_priority, existing_choices = top_authorities(
            existing_by_group.get(key, [])
        )
        incoming_priority, incoming_choices = top_authorities(
            incoming_by_group.get(key, [])
        )
        authority = None
        if len(existing_choices) == 1:
            authority = next(iter(existing_choices.values()))
            if incoming_priority > existing_priority:
                authority = (
                    next(iter(incoming_choices.values()))
                    if len(incoming_choices) == 1
                    else None
                )
            elif (
                incoming_priority == existing_priority == priority["future_identity"]
                and incoming_choices
                and set(incoming_choices) != set(existing_choices)
            ):
                authority = None
        elif len(existing_choices) > 1:
            # Legacy divergent rows are repairable only when the current
            # owned-source pass supplies one unambiguous authority at equal or
            # higher strength.  Array order is never an authority signal.
            if incoming_priority >= existing_priority and len(incoming_choices) == 1:
                authority = next(iter(incoming_choices.values()))
        elif len(incoming_choices) == 1:
            authority = next(iter(incoming_choices.values()))
        if authority is None:
            scope, group = key
            names = sorted({
                item["canonical_name"]
                for item in [
                    *existing_by_group.get(key, []),
                    *incoming_by_group.get(key, []),
                ]
            })
            raise IdentityAuthorityConflictError([{
                "reason": "identity_group_authority_ambiguous",
                "identity_group": group,
                "identity_scope_fingerprint": scope,
                "canonical_names": names,
                "message": (
                    f"identity_group={group} 缺少唯一可验证权威：{names}"
                ),
            }])
        group_authorities[key] = authority

    def bind_to_group_authority(candidate: dict) -> dict:
        key = group_key(candidate)
        authority = group_authorities.get(key) if key is not None else None
        if authority is None:
            return candidate
        rebound = {
            **candidate,
            "canonical_name": authority["canonical_name"],
            "resolution": authority["resolution"],
            "authority_id": authority["authority_id"],
        }
        # source_instance_key is an occurrence scope, not an identity-group
        # alias.  Preserve it byte-for-byte and never synthesize one.
        if "source_instance_key" not in candidate:
            rebound.pop("source_instance_key", None)
        return normalize_character_resolution(rebound)

    merged: list[dict] = []
    for candidate in [*normalized_existing, *normalized_incoming]:
        candidate = bind_to_group_authority(candidate)
        source_label = str(candidate.get("source_label") or "").strip()
        source_instance_key = str(
            candidate.get("source_instance_key") or ""
        ).strip()
        current_index = next((
            index
            for index, current_item in enumerate(merged)
            if (
                str(current_item.get("source_label") or "").strip()
                == source_label
                and str(current_item.get("identity_group") or "").strip()
                == str(candidate.get("identity_group") or "").strip()
                and str(
                    current_item.get("identity_scope_fingerprint") or ""
                ).strip() == str(
                    candidate.get("identity_scope_fingerprint") or ""
                ).strip()
                and str(
                    current_item.get("source_instance_key") or ""
                ).strip() == source_instance_key
            )
        ), None)
        current = merged[current_index] if current_index is not None else None
        if current is None:
            merged.append(candidate)
            continue
        current_priority = priority.get(
            str(current.get("resolution") or ""), 0,
        )
        candidate_priority = priority.get(
            str(candidate.get("resolution") or ""), 0,
        )
        if candidate_priority > current_priority:
            merged[current_index] = candidate
        elif (
            candidate_priority == current_priority
            and current.get("canonical_name") == candidate.get("canonical_name")
        ):
            merged[current_index] = {**current, **candidate}
    return merged


def load_screenplay_character_resolutions(conn, episode_id: str) -> list[dict]:
    if not _has_column(conn, "episodes", "screenplay_character_resolutions"):
        return []
    row = conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if not row:
        return []
    try:
        payload = json.loads(row["screenplay_character_resolutions"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return (
        normalize_character_resolutions(payload)
        if isinstance(payload, list)
        else []
    )


def screenplay_character_resolutions_for_source(
    resolutions: list[dict] | None,
    *,
    episode_no: int,
    source_text: str,
) -> list[dict]:
    """Return the only resolution set downstream screenplay code may trust.

    Durable manual/Bible decisions remain portable.  Automatic decisions are
    admitted only when their aggregate/source epoch and, for RF11 current
    identities, complete owned-evidence receipt bundle are current.
    """
    return [
        item
        for item in normalize_character_resolutions(resolutions)
        if screenplay_identity_resolution_is_current_for_source(
            item,
            episode_no=episode_no,
            source_text=source_text,
        )
    ]


def load_screenplay_character_resolutions_for_source(
    conn,
    episode_id: str,
    *,
    episode_no: int,
    source_text: str,
) -> list[dict]:
    return screenplay_character_resolutions_for_source(
        load_screenplay_character_resolutions(conn, episode_id),
        episode_no=episode_no,
        source_text=source_text,
    )


def persist_screenplay_character_resolutions(
    conn,
    episode_id: str,
    resolutions: list[dict] | None,
    *,
    retire_legacy_future_identity: bool = False,
    expected_active_run_id: str | None = None,
    expected_revision_id: str | None = None,
    replace_identity_scope: str | None = None,
    retire_stale_structural_identity_policy: str | None = None,
    retire_stale_identity_scope_fingerprint: str | None = None,
    retire_automatic_identity_keys: set[tuple[str, str, str]] | None = None,
) -> list[dict]:
    columns = "screenplay_character_resolutions"
    if expected_active_run_id is not None:
        columns += ", active_screenplay_run_id"
    row = conn.execute(
        f"SELECT {columns} FROM episodes WHERE id=?",  # noqa: S608 - fixed columns
        (episode_id,),
    ).fetchone()
    if row is None:
        raise StateConflict("episode", episode_id, {episode_id}, "missing")
    old_json = str(row["screenplay_character_resolutions"] or "[]")
    try:
        old_payload = json.loads(old_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        old_payload = []
    current = (
        normalize_character_resolutions(old_payload)
        if isinstance(old_payload, list)
        else []
    )
    if replace_identity_scope is not None:
        # This call is the complete owned-source discovery replacement
        # boundary, not an incremental structural audit.  Retire every prior
        # automatic decision (including same-hash rows omitted by the fresh
        # result); only explicitly durable human/Bible provenance survives.
        current = [
            item
            for item in current
            if str(item.get("decision_provenance") or "").strip()
            in DURABLE_IDENTITY_DECISION_PROVENANCE
        ]
    if expected_active_run_id is not None:
        actual_owner = str(row["active_screenplay_run_id"] or "")
        if actual_owner != expected_active_run_id:
            raise StateConflict(
                "screenplay_resolution_owner",
                episode_id,
                {expected_active_run_id},
                actual_owner,
            )
    if expected_revision_id is not None:
        revision_row = conn.execute(
            "SELECT id FROM production_revisions "
            "WHERE episode_id=? AND kind='screenplay' AND status='active' "
            "ORDER BY updated_at DESC LIMIT 1",
            (episode_id,),
        ).fetchone()
        actual_revision = str(revision_row["id"] or "") if revision_row else ""
        if actual_revision != expected_revision_id:
            raise StateConflict(
                "screenplay_resolution_revision",
                episode_id,
                {expected_revision_id},
                actual_revision,
            )
    if retire_legacy_future_identity:
        current = [
            item for item in current
            if (
                str(item.get("resolution") or "") != "future_identity"
                or str(item.get("decision_contract_version") or "")
                == FUTURE_IDENTITY_DECISION_VERSION
            )
        ]
    if retire_stale_structural_identity_policy is not None:
        current = [
            item for item in current
            if (
                str(item.get("decision_provenance") or "").strip()
                in DURABLE_IDENTITY_DECISION_PROVENANCE
                or str(
                    item.get("structural_identity_policy_version") or ""
                ).strip() == retire_stale_structural_identity_policy
            )
        ]
    if retire_stale_identity_scope_fingerprint is not None:
        current = [
            item
            for item in current
            if screenplay_identity_resolution_is_current_for_scope(
                item,
                identity_scope_fingerprint=(
                    retire_stale_identity_scope_fingerprint
                ),
            )
        ]
    if retire_automatic_identity_keys:
        current = [
            item
            for item in current
            if (
                str(item.get("decision_provenance") or "").strip()
                in DURABLE_IDENTITY_DECISION_PROVENANCE
                or (
                    str(item.get("source_label") or "").strip(),
                    str(item.get("identity_group") or "").strip(),
                    str(
                        item.get("identity_scope_fingerprint") or ""
                    ).strip(),
                ) not in retire_automatic_identity_keys
            )
        ]
    merged = merge_screenplay_character_resolutions(current, resolutions)
    # Fingerprint stability guard. A fresh discovery pass that reproduces the
    # SAME semantic identity decisions (same authority_id / resolution /
    # identity group / provenance) must not rewrite the stored rows just because
    # the model re-authored volatile free-text (reason/evidence) or row order.
    # That churn changed screenplay_authority_fingerprint between a retry-grant
    # activation and its baseline task, superseding the revision the
    # user_retry_approval grant was bound to and deadlocking every retry
    # (BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED). Comparing against the ORIGINAL
    # stored payload (not the post-retire ``current``) keeps genuine retire /
    # scope-replacement writes intact while suppressing no-op semantic rewrites.
    stored_current = (
        normalize_character_resolutions(old_payload)
        if isinstance(old_payload, list)
        else []
    )

    def _receipt_semantic_key(item: dict) -> tuple[str, str]:
        if str(item.get("source_label_provenance") or "").strip() == (
            IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
        ):
            return (
                "adjudication_v2"
                if _identity_adjudication_receipt_is_valid(
                    item,
                    source_text=None,
                )
                else "invalid_adjudication",
                "",
            )
        try:
            bundle = _validate_current_identity_receipt_bundle(
                item,
                source_text=None,
            )
        except ContentGenerationError:
            return ("invalid", "")
        if bundle is not None:
            return ("current_v2", "")
        return ("typed_or_none", "")

    def _semantic_identity_key(items: list[dict]) -> list[tuple[str, ...]]:
        return sorted(
            (
                str(item.get("authority_id") or ""),
                str(item.get("source_label") or ""),
                str(item.get("canonical_name") or ""),
                str(item.get("resolution") or ""),
                str(item.get("identity_group") or ""),
                str(item.get("identity_scope_fingerprint") or ""),
                str(item.get("decision_provenance") or ""),
                str(item.get("decision_contract_version") or ""),
                str(
                    item.get("structural_identity_policy_version") or ""
                ),
                *_receipt_semantic_key(item),
            )
            for item in items
        )

    if _semantic_identity_key(merged) == _semantic_identity_key(stored_current):
        return stored_current
    if _has_column(conn, "episodes", "screenplay_character_resolutions"):
        clauses = ["id=?", "screenplay_character_resolutions=?"]
        params: list[object] = [
            json.dumps(merged, ensure_ascii=False),
            episode_id,
            old_json,
        ]
        if expected_active_run_id is not None:
            clauses.append("COALESCE(active_screenplay_run_id, '')=?")
            params.append(expected_active_run_id)
        if expected_revision_id is not None:
            clauses.append(
                "?=(SELECT id FROM production_revisions "
                "WHERE episode_id=episodes.id AND kind='screenplay' "
                "AND status='active' ORDER BY updated_at DESC LIMIT 1)"
            )
            params.append(expected_revision_id)
        cursor = conn.execute(
            "UPDATE episodes SET screenplay_character_resolutions=? WHERE "
            + " AND ".join(clauses),
            params,
        )
        if cursor.rowcount != 1:
            # This helper owns the persistence commit.  A failed optimistic
            # write must not leave the process-global SQLite connection inside
            # an open transaction or retain a write lock.
            conn.rollback()
            raise StateConflict(
                "screenplay_resolution_cas",
                episode_id,
                {expected_active_run_id or "unchanged-owner-and-value"},
                "stale-owner-revision-or-value",
            )
        conn.commit()
    return merged


async def ensure_cards_for_text(
    project_id: str,
    episode_no: int,
    source_text: str,
    bible: Bible,
    *,
    draft_text: str = "",
    generate_portraits: bool = True,
    _precomputed_candidates: list[dict] | None = None,
    write_guard: Callable[[], None] | None = None,
) -> dict:
    """发现并补人物卡；同时输出供剧本使用的姓名消歧表。"""
    conn = get_conn()
    episode_row = (
        conn.execute(
            "SELECT id FROM episodes WHERE project_id=? AND episode_no=?",
            (project_id, episode_no),
        ).fetchone()
        if _has_column(conn, "episodes", "id")
        else None
    )
    existing_resolutions = (
        load_screenplay_character_resolutions(conn, episode_row["id"])
        if episode_row
        else []
    )
    identity_scope_fingerprint = screenplay_identity_scope_fingerprint(
        episode_no, source_text
    )
    # Automatic decisions are inputs to the next discovery pass only when all
    # three authority fences match the current owned source.  Older coverage,
    # future-wire or source epochs must be re-adjudicated before influencing a
    # the current strict prompt; explicitly durable manual/Bible decisions survive.
    existing_resolutions = [
        item for item in existing_resolutions
        if screenplay_identity_resolution_is_current_for_source(
            item,
            episode_no=episode_no,
            source_text=source_text,
        )
    ]
    future_text, future_label = _future_chapter_context(
        conn, project_id, episode_no,
    )
    candidates = (
        [dict(item) for item in _precomputed_candidates]
        if _precomputed_candidates is not None
        else await discover_character_candidates(
            source_text, bible, episode_no, draft_text=draft_text,
            future_text=future_text, future_label=future_label,
            existing_resolutions=existing_resolutions,
            scope_id=str(episode_row["id"]) if episode_row else None,
            project_id=project_id,
        )
    )
    candidates = [
        {
            **item,
            "identity_scope_fingerprint": str(
                item.get("identity_scope_fingerprint")
                or identity_scope_fingerprint
            ),
        }
        for item in candidates
        if isinstance(item, dict)
    ]
    for item in candidates:
        provenance = str(
            item.get("source_label_provenance") or ""
        ).strip()
        has_bundle_fields = bool(
            item.get("source_evidence_receipt") is not None
            or item.get("source_evidence_receipts") is not None
        )
        if has_bundle_fields or provenance in {
            CURRENT_IDENTITY_LITERAL_PROVENANCE,
            CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
        }:
            bundle = _validate_current_identity_receipt_bundle(
                item,
                source_text=source_text,
                draft_text=draft_text,
            )
            if bundle is None:
                raise ContentGenerationError(
                    "current identity candidate 缺少 v2 evidence receipt bundle"
                )
    if write_guard:
        write_guard()
    known = {c.name for c in bible.characters}
    unknown_by_name: dict[str, list[dict]] = {}
    functional_candidates: list[dict] = []
    known_named_candidates: list[dict] = []
    mentioned_only_candidates: list[dict] = []
    errors: list[str] = []
    for item in candidates:
        if item.get("identity_kind") == "functional":
            functional_candidates.append(item)
            continue
        name = str(item.get("name") or "").strip()
        if not _named_candidate_materialization_compatible(item):
            if item.get("kind") == "mentioned" and name and name not in known:
                mentioned_only_candidates.append(item)
            else:
                errors.append(
                    "named authority 不可直接物化人物卡："
                    f"{str(item.get('source_label') or name).strip()}->{name}"
                )
            continue
        if name in known:
            known_named_candidates.append(item)
        elif _candidate_requires_identity_card(item, known):
            unknown_by_name.setdefault(name, []).append(item)
        elif name:
            mentioned_only_candidates.append(item)
    added: list[dict] = []
    provisional_characters: list[dict] = []
    skipped: list[dict] = [
        {
            "status": "mentioned_only",
            "name": str(item.get("name") or "").strip(),
            "reason": "本集仅提及且未出镜/开口，不创建人物卡",
        }
        for item in mentioned_only_candidates
    ]
    warnings: list[str] = []
    resolutions: list[dict] = []
    assigned_extra_names: dict[str, str] = {}
    assigned_identity_groups: dict[str, str] = {}
    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：identity_group
    # 已经是可靠的按人区分键，但当两个不同的 identity_group 都退回同一个
    # 裸 source_label 当 route_name（比如两个不同的"外宗弟子"）时，两者的
    # route_name 字符串会变成完全相同的值——route_name 是这个函数唯一往
    # 外传的东西，下游（app.production.prep_pack 的 functional_extras，按
    # 这个字符串当 key 聚合 event_ids）拿到手就已经分不清是谁了，会把两个
    # 人的出场事件悄悄合并进同一条群演记录。用确定性序号区分（"外宗弟子
    # （乙）"），不是"路人甲/乙/丙"式的泛化替换——原有的功能性描述原样
    # 保留，只在真的撞车时追加后缀（见函数上方"不得通过改成路人甲/乙/丙
    # 来...抹掉来源身份"的既有原则，这里遵循同一原则：只加后缀，不换描述）。
    _route_name_first_owner: dict[str, str] = {}
    _route_name_collisions: dict[str, int] = {}

    # A stable referenced identity still needs an authority even when it never
    # appears visually and therefore must not create a character card.
    for item in mentioned_only_candidates:
        source_label = str(
            item.get("source_label") or item.get("name") or ""
        ).strip()
        canonical_name = str(item.get("name") or source_label).strip()
        if source_label and canonical_name:
            resolutions.append(_identity_resolution(
                item,
                canonical_name,
                "reference_identity",
                reason=(
                    "来源或蓝图引用该稳定身份，但当前集不需要人物卡或视觉资产"
                ),
            ))

    for item in known_named_candidates:
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("name") or "").strip()
        if source_label and canonical_name and source_label != canonical_name:
            resolutions.append(_identity_resolution(
                item,
                canonical_name,
                "future_identity",
                reason="后续章节已确认该称谓属于人物谱已有角色",
            ))

    # 功能身份保留原文稳定称谓。是否需要人物卡与是否具备真名是两件事，
    # 不得通过改成“路人甲/乙/丙”来降低角色重要性或抹掉来源身份。
    for item in functional_candidates:
        source_label = str(item.get("source_label") or item.get("name") or "").strip()
        identity_group = str(
            item.get("identity_group") or f"source:{source_label}"
        ).strip()
        route_name = str(item.get("existing_route_name") or "").strip()
        if not route_name:
            route_name = assigned_identity_groups.get(identity_group, "")
        if not route_name:
            first_owner = _route_name_first_owner.setdefault(
                source_label, identity_group,
            )
            if first_owner == identity_group:
                route_name = source_label
            else:
                _route_name_collisions[source_label] = (
                    _route_name_collisions.get(source_label, 1) + 1
                )
                route_name = (
                    f"{source_label}"
                    f"（{_identity_disambiguating_suffix(_route_name_collisions[source_label])}）"
                )
        assigned_identity_groups[identity_group] = route_name
        assigned_extra_names[source_label] = route_name
        resolutions.append(_identity_resolution(
            item,
            route_name,
            "functional_identity",
            reason="模型依据当前来源确认该实体为本集功能身份",
        ))

    for name, items in unknown_by_name.items():
        ensure_kwargs = {
            "generate_portrait": generate_portraits,
            "require_identity_card": True,
        }
        if write_guard is not None:
            ensure_kwargs["write_guard"] = write_guard
        result = await ensure_character_card(
            project_id,
            name,
            episode_no,
            **ensure_kwargs,
        )
        if result.get("status") == "added":
            added.append(result)
            if not result.get("has_portrait"):
                warnings.append(
                    f"{name}：人物卡已添加，定妆资产将在独立资产环节补齐"
                    if result.get("portrait_deferred")
                    else f"{name}：人物卡已添加，定妆照生成失败，需稍后重试"
                )
        elif result.get("status") == "pending_review":
            # 兼容旧实现返回值；新流程不应再产生用户待审项。
            errors.append(f"{name}：自动建卡流程未完成")
        elif result.get("status") in {
            "skipped_minor", "exists", "skipped_not_person",
        }:
            skipped.append(result)
            if result.get("status") == "skipped_not_person":
                # 非人（宗门、器物）以及非故事角色（作者笔名出现在章末旁白）
                # 本来就不该进人物谱，这是正常终态而不是错误。但它们不能继续
                # 保持 named：结构人物 coverage 会要求每个具名身份都有已物化的
                # 人物卡，于是"正确地拒绝建卡"反而让整集硬失败（生产上 EP3 卡在
                # 「耳根」——作者笔名）。降级为功能身份，让两边重新一致。
                for item in items:
                    item["identity_kind"] = "functional"
                    item["authority_id"] = ""
                    item["materialization_compatible"] = False
            # 非人（宗门/器物/地点）本来就不该进人物谱，这是正常终态而不是错误。
            if result.get("status") == "skipped_minor":
                # identity_kind=named 已由身份模型给出可靠同一性证据。
                # 不能再用“戏份不足”把真名降回路人；卡片不完整就留在剧本闸门修复。
                errors.append(
                    f"{name}：真名已确认，但人物卡未完成："
                    f"{result.get('reason') or 'unknown reason'}"
                )
        else:
            errors.append(f"{name}：{result.get('reason') or result.get('status') or '补卡失败'}")

        if result.get("status") in {"added", "exists"}:
            for item in items:
                source_label = str(item.get("source_label") or name).strip()
                if source_label != name:
                    resolutions.append(_identity_resolution(
                        item,
                        name,
                        "future_identity",
                        reason="后续章节已确认该称谓的稳定真名",
                    ))
    return {
        "checked": len(unknown_by_name),
        "candidates": candidates,
        "added": added,
        "provisional_characters": provisional_characters,
        "skipped": skipped,
        "resolutions": resolutions,
        "future_context_label": future_label,
        "errors": errors,
        "warnings": warnings,
    }


async def ensure_structural_identity_coverage(
    project_id: str,
    episode_id: str,
    episode_no: int,
    source_text: str,
    bible: Bible,
    structural_evidence: list[dict],
    *,
    write_guard: Callable[[], None] | None = None,
    expected_active_run_id: str | None = None,
    expected_revision_id: str | None = None,
) -> dict:
    """Materialize only identity gaps evidenced by a validated Blueprint/IR.

    This is the replacement for the old unconditional third full-chapter scan:
    current/future candidates are reused from the normalized discovery Artifact,
    and the model sees only unresolved typed references plus their owned SRC.
    """
    conn = get_conn()
    source_hash = evidence_repository.content_hash(source_text)
    identity_scope_fingerprint = screenplay_identity_scope_fingerprint(
        episode_no, source_text
    )
    structural_hash = evidence_repository.content_hash({
        "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
        "source_hash": source_hash,
        "structural_evidence": structural_evidence,
    })
    rows = conn.execute(
        """SELECT id,content_json,content_hash FROM artifacts
             WHERE scope_type='episode' AND scope_id=?
               AND type='screenplay_identity_discovery' AND status='validated'
             ORDER BY created_at DESC LIMIT 20""",
        (episode_id,),
    ).fetchall() if _has_column(conn, "artifacts", "content_hash") else []
    parsed_rows: list[tuple[sqlite3.Row, dict]] = []
    for row in rows:
        try:
            payload = json.loads(row["content_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and str(row["content_hash"] or "").strip()
            and str(row["content_hash"] or "").strip()
            == evidence_repository.content_hash(payload)
        ):
            parsed_rows.append((row, payload))
    base_candidates: list[dict] = []
    parent_artifact_id = ""
    for row, payload in parsed_rows:
        if (
            payload.get("mode") != "structural_coverage"
            and payload.get("contract_version")
            == IDENTITY_DISCOVERY_CONTRACT_VERSION
            and payload.get("structural_coverage_policy_version")
            == STRUCTURAL_IDENTITY_COVERAGE_VERSION
            and payload.get("structural_coverage_applied") is False
            and payload.get("source_hash") == source_hash
            and isinstance(payload.get("candidates"), list)
        ):
            if any(not isinstance(item, dict) for item in payload["candidates"]):
                continue
            candidate_rows = [dict(item) for item in payload["candidates"]]
            typed_current_rows = [
                item for item in candidate_rows
                if str(item.get("source_label_provenance") or "").strip()
                in {
                    CURRENT_IDENTITY_LITERAL_PROVENANCE,
                    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
                }
            ]
            if (
                candidate_rows
                and len(typed_current_rows) != len(candidate_rows)
            ):
                continue
            try:
                for item in typed_current_rows:
                    if _validate_current_identity_receipt_bundle(
                        item,
                        source_text=source_text,
                    ) is None:
                        raise ContentGenerationError(
                            "structural base current candidate 缺少 v2 receipt"
                        )
            except ContentGenerationError:
                continue
            base_candidates = candidate_rows
            parent_artifact_id = str(row["id"])
            break
    invalid_cached_resolution_keys: set[tuple[str, str, str]] = set()
    matching_coverage_artifact_seen = False

    def verified_materialized_bible_names(candidates: list[dict]) -> list[str]:
        required = [
            name for name in _structural_identity_required_bible_names(
                candidates
            )
            # 卡层已判定"不是角色"（宗门、器物，或出现在章末旁白里的作者笔名）。
            # 那是正确的拒绝，不能反过来要求它必须有人物卡——生产上 EP3 就是被
            # 「耳根」这个作者笔名整集卡死的。
            if not str(
                get_setting(_non_character_skip_key(project_id, name)) or ""
            ).strip()
        ]
        available = _project_bible_character_names(conn, project_id, bible)
        missing = set(required) - available
        if missing:
            raise ContentGenerationError(
                "结构人物 coverage 的 named card 尚未物化："
                + ",".join(sorted(missing))
            )
        return required
    for _row, payload in parsed_rows:
        if (
            not matching_coverage_artifact_seen
            and payload.get("mode") == "structural_coverage"
            and payload.get("contract_version")
            == IDENTITY_DISCOVERY_CONTRACT_VERSION
            and payload.get("policy_version")
            == STRUCTURAL_IDENTITY_COVERAGE_VERSION
            and payload.get("source_hash") == source_hash
            and payload.get("structural_evidence_hash") == structural_hash
            and isinstance(payload.get("candidates"), list)
        ):
            matching_coverage_artifact_seen = True
            required_keys = {
                (
                    str(item.get("source_label") or "").strip(),
                    str(item.get("identity_group") or "").strip(),
                    identity_scope_fingerprint,
                )
                for item in payload["candidates"]
                if (
                    isinstance(item, dict)
                    and str(item.get("source_label") or "").strip()
                    and str(item.get("identity_group") or "").strip()
                )
            }
            cached_resolutions = load_screenplay_character_resolutions(
                conn, episode_id
            )
            current_cached_resolutions = (
                screenplay_character_resolutions_for_source(
                    cached_resolutions,
                    episode_no=episode_no,
                    source_text=source_text,
                )
            )
            expected_receipt = payload.get("materialized_resolution_receipt")
            try:
                expected_candidate_hash = str(
                    payload.get("candidate_semantic_hash") or ""
                )
                required_bible_names = (
                    _structural_identity_required_bible_names(
                        payload["candidates"]
                    )
                )
                current_bible_names = _project_bible_character_names(
                    conn, project_id, bible
                )
                actual_receipt = _structural_identity_resolution_receipt(
                    current_cached_resolutions,
                    candidates=payload["candidates"],
                    identity_scope_fingerprint=identity_scope_fingerprint,
                )
                actual_catalog_input_hash = (
                    _structural_identity_catalog_input_hash(
                        bible=bible,
                        base_candidates=base_candidates,
                        structural_evidence_hash=structural_hash,
                        existing_resolutions=current_cached_resolutions,
                        output_candidates=payload["candidates"],
                    )
                )
                cache_is_exact = bool(
                    expected_candidate_hash
                    and expected_candidate_hash
                    == _structural_identity_candidate_semantic_hash(
                        payload["candidates"]
                    )
                    and _structural_identity_resolution_receipt_is_valid(
                        expected_receipt
                    )
                    and expected_receipt == actual_receipt
                    and payload.get("materialized_bible_names")
                    == required_bible_names
                    and set(required_bible_names) <= current_bible_names
                    and str(payload.get("coverage_catalog_input_hash") or "")
                    == actual_catalog_input_hash
                    and _structural_identity_catalog_receipt_is_valid(
                        payload.get("coverage_catalog_receipt")
                    )
                )
            except (ContentGenerationError, TypeError, ValueError, KeyError):
                # A validated cache may be stale or corrupt.  Recovery must
                # re-audit once instead of making that bad marker permanently
                # sticky across retries.
                cache_is_exact = False
            if cache_is_exact:
                # A validated receipt can coexist with unrelated legacy rows.
                # Retire those rows at the successful recovery boundary before
                # exposing any authority to the screenplay compiler.
                persisted = persist_screenplay_character_resolutions(
                    conn,
                    episode_id,
                    [],
                    expected_active_run_id=expected_active_run_id,
                    expected_revision_id=expected_revision_id,
                    retire_stale_identity_scope_fingerprint=(
                        identity_scope_fingerprint
                    ),
                )
                persisted = screenplay_character_resolutions_for_source(
                    persisted,
                    episode_no=episode_no,
                    source_text=source_text,
                )
                if expected_receipt != _structural_identity_resolution_receipt(
                    persisted,
                    candidates=payload["candidates"],
                    identity_scope_fingerprint=identity_scope_fingerprint,
                ):
                    raise StateConflict(
                        "screenplay_identity_resolution_receipt",
                        episode_id,
                        {str(expected_receipt.get("hash") or "")},
                        "changed-during-cache-recovery",
                    )
                return {
                    "checked": 0,
                    "candidates": payload["candidates"],
                    "added": [],
                    "resolutions": persisted,
                    "errors": [],
                    "warnings": [],
                    "reused": True,
                }
            invalid_cached_resolution_keys.update(required_keys)
    existing_coverage_resolutions = [
        item
        for item in load_screenplay_character_resolutions_for_source(
            conn,
            episode_id,
            episode_no=episode_no,
            source_text=source_text,
        )
        if (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
            str(item.get("identity_scope_fingerprint") or "").strip(),
        ) not in invalid_cached_resolution_keys
    ]
    coverage_catalog_receipt: dict[str, object] = {}
    audited = await audit_identity_coverage_from_structural_evidence(
        base_candidates,
        structural_evidence=structural_evidence,
        source_text=source_text,
        bible=bible,
        episode_no=episode_no,
        existing_resolutions=existing_coverage_resolutions,
        catalog_receipt=coverage_catalog_receipt,
    )
    coverage_catalog_input_hash = _structural_identity_catalog_input_hash(
        bible=bible,
        base_candidates=base_candidates,
        structural_evidence_hash=structural_hash,
        existing_resolutions=existing_coverage_resolutions,
        output_candidates=audited,
    )
    if write_guard:
        write_guard()
    base_keys = {
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        )
        for item in base_candidates
    }
    additions = [
        item for item in audited
        if (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        ) not in base_keys
    ]
    recovery_candidates = [
        item
        for item in audited
        if (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
            identity_scope_fingerprint,
        ) in invalid_cached_resolution_keys
    ]
    materialization_candidates = list({
        (
            str(item.get("source_label") or "").strip(),
            str(item.get("identity_group") or "").strip(),
        ): item
        for item in [*additions, *recovery_candidates]
    }.values())
    if not materialization_candidates:
        if write_guard:
            write_guard()
        persisted = persist_screenplay_character_resolutions(
            conn,
            episode_id,
            [],
            expected_active_run_id=expected_active_run_id,
            expected_revision_id=expected_revision_id,
            retire_stale_structural_identity_policy=(
                STRUCTURAL_IDENTITY_COVERAGE_VERSION
            ),
            retire_stale_identity_scope_fingerprint=(
                identity_scope_fingerprint
            ),
            retire_automatic_identity_keys=invalid_cached_resolution_keys,
        )
        persisted = screenplay_character_resolutions_for_source(
            persisted,
            episode_no=episode_no,
            source_text=source_text,
        )
        materialized_bible_names = verified_materialized_bible_names(audited)
        if write_guard:
            write_guard()
        trace = None
        try:
            from app.observability.tracing import current_trace

            trace = current_trace()
        except Exception:  # noqa: BLE001
            pass
        raw_artifact = evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_identity_discovery_raw",
                scope_type="episode",
                scope_id=episode_id,
                status="candidate",
                trust_level="T0",
                content={
                    "mode": "structural_coverage",
                    "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                    "structural_evidence_hash": structural_hash,
                    "coverage_catalog_input_hash": (
                        coverage_catalog_input_hash
                    ),
                    "coverage_catalog_receipt": coverage_catalog_receipt,
                    "model_candidates": [],
                },
                parent_artifact_ids=[parent_artifact_id] if parent_artifact_id else [],
                contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
            ),
            step_run_id=getattr(trace, "step_run_id", None),
        )
        evidence_repository.create_artifact(
            EvidenceArtifact(
                type="screenplay_identity_discovery",
                scope_type="episode",
                scope_id=episode_id,
                status="validated",
                trust_level="T1",
                content={
                    "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                    "episode_no": episode_no,
                    "mode": "structural_coverage",
                    "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                    "candidates": audited,
                    "candidate_semantic_hash": (
                        _structural_identity_candidate_semantic_hash(audited)
                    ),
                    "materialized_resolution_receipt": (
                        _structural_identity_resolution_receipt(
                            persisted,
                            candidates=audited,
                            identity_scope_fingerprint=(
                                identity_scope_fingerprint
                            ),
                        )
                    ),
                    "materialized_bible_names": materialized_bible_names,
                    "source_hash": source_hash,
                    "structural_evidence_hash": structural_hash,
                    "coverage_catalog_input_hash": (
                        coverage_catalog_input_hash
                    ),
                    "coverage_catalog_receipt": coverage_catalog_receipt,
                },
                parent_artifact_ids=[raw_artifact["id"]],
                contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
            ),
            step_run_id=getattr(trace, "step_run_id", None),
        )
        return {
            "checked": 0,
            "candidates": audited,
            "added": [],
            "resolutions": persisted,
            "errors": [],
            "warnings": [],
        }
    result = await ensure_cards_for_text(
        project_id,
        episode_no,
        source_text,
        bible,
        generate_portraits=False,
        _precomputed_candidates=materialization_candidates,
        write_guard=write_guard,
    )
    if write_guard:
        write_guard()
    if result.get("errors"):
        # Provider/schema validation is not the materialization boundary.  A
        # card failure (or any downstream identity error) must never mint a
        # validated coverage Artifact with an empty receipt: that would turn
        # the next run into a false cache success and bypass the identity gate.
        result["resolutions"] = screenplay_character_resolutions_for_source(
            result.get("resolutions") or [],
            episode_no=episode_no,
            source_text=source_text,
        )
        return result
    persisted = persist_screenplay_character_resolutions(
        conn,
        episode_id,
        result.get("resolutions") or [],
        expected_active_run_id=expected_active_run_id,
        expected_revision_id=expected_revision_id,
        retire_stale_structural_identity_policy=(
            STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
        retire_stale_identity_scope_fingerprint=identity_scope_fingerprint,
        retire_automatic_identity_keys=invalid_cached_resolution_keys,
    )
    persisted = screenplay_character_resolutions_for_source(
        persisted,
        episode_no=episode_no,
        source_text=source_text,
    )
    materialized_bible_names = verified_materialized_bible_names(audited)
    if write_guard:
        write_guard()
    result["resolutions"] = persisted
    trace = None
    try:
        from app.observability.tracing import current_trace

        trace = current_trace()
    except Exception:  # noqa: BLE001
        pass
    raw_artifact = evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery_raw",
            scope_type="episode",
            scope_id=episode_id,
            status="candidate",
            trust_level="T0",
            content={
                "mode": "structural_coverage",
                "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "structural_evidence_hash": structural_hash,
                "coverage_catalog_input_hash": coverage_catalog_input_hash,
                "coverage_catalog_receipt": coverage_catalog_receipt,
                "model_candidates": materialization_candidates,
            },
            parent_artifact_ids=[parent_artifact_id] if parent_artifact_id else [],
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    evidence_repository.create_artifact(
        EvidenceArtifact(
            type="screenplay_identity_discovery",
            scope_type="episode",
            scope_id=episode_id,
            status="validated",
            trust_level="T1",
            content={
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "episode_no": episode_no,
                "mode": "structural_coverage",
                "policy_version": STRUCTURAL_IDENTITY_COVERAGE_VERSION,
                "candidates": audited,
                "candidate_semantic_hash": (
                    _structural_identity_candidate_semantic_hash(audited)
                ),
                "materialized_resolution_receipt": (
                    _structural_identity_resolution_receipt(
                        persisted,
                        candidates=audited,
                        identity_scope_fingerprint=identity_scope_fingerprint,
                    )
                ),
                "materialized_bible_names": materialized_bible_names,
                "source_hash": source_hash,
                "structural_evidence_hash": structural_hash,
                "coverage_catalog_input_hash": coverage_catalog_input_hash,
                "coverage_catalog_receipt": coverage_catalog_receipt,
            },
            parent_artifact_ids=[raw_artifact["id"]],
            contract_version=IDENTITY_DISCOVERY_CONTRACT_VERSION,
        ),
        step_run_id=getattr(trace, "step_run_id", None),
    )
    return result


# 人物谱是"可以被选角、被定妆、能出镜表演的人"的登记表。宗门、地点、器物、
# 功法都不是人，它们属于场景库或 reference 身份，绝不能占据人物卡。
#
# 生产事故：模型给「靠山宗」的建卡理由是「属于独立的组织类出场单元…需单独建卡
# 保证漫剧场景一致性」，给「凝气卷」的理由是「靠山宗发放的修行典籍」——两次都
# 如实说明了这不是人，却照样入了人物谱。原因是建卡判定问的一直是"值不值得做
# 一致性锚点"，从来没有人问过"这是不是一个人"。
CHARACTER_SUBJECT_KINDS = (
    "person",
    "organization",
    "place",
    "object",
    "other",
)
CHARACTER_SUBJECT_PERSON = "person"

# role 是合同枚举，不是自由文本。「靠山宗」当初写进来的 role 是"重要场景载体"，
# 根本不在允许值里，却因为只检查了非空而落库。
CHARACTER_CARD_ROLES = ("主角", "重要配角", "反派")


def _candidate_requires_identity_card(item: dict, known_names: set[str]) -> bool:
    """Only a new named identity that appears or speaks needs a visual card."""
    name = str(item.get("name") or "").strip()
    return bool(
        name
        and name not in known_names
        and str(item.get("identity_kind") or "named") == "named"
        and item.get("kind") != "mentioned"
    )


async def assess_new_character(name: str, fragments: str, *, style: str,
                               known_names: list[str], ep_label: str,
                               require_identity_card: bool = False,
                               chapters_by_idx: dict[int, str] | None = None) -> dict:
    """针对一个【具体名字】判断是否值得单独建卡（戏份够 / 画面多），并产出角色卡字段。
    返回 {important, reason, role, appearance_canonical, personality, speech_style,
    relationships, source_evidence}。

    chapters_by_idx：`_forward_fragments` 一并返回的全书原文查找表（未截断），用于核验
    模型申报的 source_evidence（王有材事故修复新增，见
    logs/appearance_provenance_plan.md）。生产路径（`ensure_character_card`）总是显式
    传入；测试直连本函数且不传时按 None 处理，此时无法核验任何证据，安全默认是"全部
    拒绝"（不确定不采信），不是静默放行。
    """
    from app.stages import _appearance_evidence_verified

    known = "、".join(known_names) or "（无）"
    identity_contract = (
        "身份消歧模型已用上下文确认这是稳定真名；本次任务不是重新判断戏份重要度，"
        "而是生成完整的最小人物卡。无论戏份多少都输出 important=true；"
        "原文对这个角色本人确有可视描写的字段，逐字取用；原文没写的可视字段，通用形态"
        "（年龄段/发型发色/服装款式颜色）按项目画风与身份合理设定，不写需要举证的"
        "标志性特征。"
        if require_identity_card else
        "请判断该称谓是否值得单独建人物卡并定妆。"
    )
    decision_contract = (
        f"- identity_card_required=true：固定输出 important=true，并完成 20~80 字"
        f" appearance_canonical；不得因只出现一次而拒绝建卡。"
        if require_identity_card else
        f"- important=true 仅当：「{name}」是【真正的新角色】，且在这段剧情里"
        "【反复出场 / 有正面戏份 / 画面感强】，值得稳定其外观。\n"
        "- important=false：路人、只被提及一两次、纯功能性提及，"
        "或其实是已有角色的别名/外号/尊称。"
    )
    prompt = f"""任务：判断小说角色「{name}」是否值得【单独建人物卡并定妆】（用作漫剧出镜的一致性锚点）。

身份合同：{identity_contract}

已有角色（若「{name}」其实是这些人的别名/外号/尊称，则 important=false）：
{known}

下面是原文中提及「{name}」的片段（{ep_label}）：
{fragments[:12000]}

判定口径：
{decision_contract}
- appearance_canonical 是"固定外观锚点串"：40~60 字，只写视觉可见信息，不写性格。通用
  形态（性别年龄感/发型发色/服装款式与颜色）原文没写处按画风（{style}）合理设定，不需要
  举证；标志性特征只有原文对这个角色本人确有描写才写，且要在 source_evidence 里给出
  evidence_chapter_index（取原文片段【第 N 章】块头里的数字）与 evidence_quote（支撑该
  特征的原文逐字短句，40 字以内、必须原样连续照抄，短句本身要能读出是在写这个角色
  本人，不是同段落里的其他人）；原文没有就不写，source_evidence 留空数组即可，不是缺陷。
- appearance_canonical 只允许常规完整着装、中性站姿下可直接看见、可跨镜稳定复现的静态形态；不得写性格、欲望、气质、眼神行为、对他人的注视方式、裸体、内衣、私密身体部位或必须暴露身体才能看见的特征。

先判断「{name}」指的是什么：
- subject_kind=person：一个具体的人（可以被选角、被定妆、能出镜表演）。
- subject_kind=organization：宗门、门派、家族、势力、商号等组织。
- subject_kind=place：地点、建筑、区域。
- subject_kind=object：器物、法宝、典籍、功法、丹药等物品。
- subject_kind=other：以上都不是。
人物谱只登记 person。组织、地点、器物即使在剧情里极其重要、也确实需要视觉一致性，
也一律 important=false——它们属于场景库，不属于人物谱。

只输出一个 JSON 对象：
{{"subject_kind": "person|organization|place|object|other", "important": true/false, "reason": "一句话依据", "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}], "source_evidence": [{{"evidence_chapter_index": int, "evidence_quote": str}}]}}"""

    async def _assess_once(extra_instruction: str) -> dict:
        messages = [{"role": "user", "content": prompt + extra_instruction}]
        raw = await model_gateway.chat(
            messages,
            temperature=0.3,
            max_tokens=CHARACTER_CARD_MAX_TOKENS,
            call_meta={
                "stage": "assess_new_character",
                "character_name": name,
                "expected_json": True,
            },
        )
        try:
            return extract_json(raw)
        except ValueError as exc:
            raise ContentGenerationError(
                f"新角色「{name}」人物卡结构化输出不完整"
            ) from exc

    def _build_verdict(obj: dict) -> dict:
        important = bool(obj.get("important"))
        subject_kind = str(obj.get("subject_kind") or "").strip()
        appearance = production_appearance_anchor(
            (obj.get("appearance_canonical") or "").strip()
        )
        if len(appearance) > APPEARANCE_MAX:
            appearance = appearance[:APPEARANCE_MAX]
        role = (obj.get("role") or "重要配角").strip() or "重要配角"
        # 完整度只看长度 + role 是否合法：新契约下"标志性特征齐全"不再是必要条件——
        # 通用形态本来就允许没有标志性特征，见 assess_new_character docstring。
        card_complete = (
            APPEARANCE_MIN <= len(appearance) <= APPEARANCE_MAX
            # role 是闭合枚举，不是自由文本。
            and role in CHARACTER_CARD_ROLES
        )
        if important and not card_complete:
            important = False  # 外观太稀薄不足以稳定定妆 → 不建卡
        if subject_kind != CHARACTER_SUBJECT_PERSON:
            # 不是人就不进人物谱，且这一条不受 require_identity_card 影响：
            # 身份消歧确认了"这是一个稳定的专名"，并不等于确认了"这是一个人"。
            # 未声明 subject_kind 的旧响应同样落在这里，方向是保守的。
            important = False
        known_set = set(known_names)
        # 只保留指向【已知角色】且 relation 非空的关系；Relationship.to/relation 必填，漏 relation 会让校验崩。
        rels = [
            {"to": r["to"], "relation": str(r.get("relation") or "").strip()}
            for r in (obj.get("relationships") or [])
            if isinstance(r, dict) and r.get("to") in known_set and str(r.get("relation") or "").strip()
        ]
        # 标志性特征证据：只有非空、逐字核验通过的条目才登记；核验失败的条目单独
        # 记下失败原因，供 require_identity_card 路径的重试逻辑使用（未通过核验时
        # 不做文本手术裁剪 appearance_canonical 本身——那是子句边界识别问题，做不到
        # 可靠的自动化字符串处理，交给模型下一轮自己重写，见
        # logs/appearance_provenance_plan.md 第 8 节风险 7）。
        verified_evidence: list[dict] = []
        rejected_evidence: list[dict] = []
        for item in (obj.get("source_evidence") or []):
            if not isinstance(item, dict):
                continue
            quote = str(item.get("evidence_quote") or "").strip()
            if not quote:
                continue
            try:
                chapter_index = int(item.get("evidence_chapter_index"))
            except (TypeError, ValueError):
                rejected_evidence.append({
                    "evidence_chapter_index": item.get("evidence_chapter_index"),
                    "evidence_quote": quote,
                })
                continue
            if _appearance_evidence_verified(
                chapters_by_idx or {}, {name}, chapter_index, quote,
            ):
                verified_evidence.append({
                    "evidence_chapter_index": chapter_index,
                    "evidence_quote": quote,
                })
            else:
                rejected_evidence.append({
                    "evidence_chapter_index": chapter_index,
                    "evidence_quote": quote,
                })
        return {
            "important": important,
            "card_complete": card_complete,
            "subject_kind": subject_kind,
            "is_person": subject_kind == CHARACTER_SUBJECT_PERSON,
            "reason": (obj.get("reason") or "").strip(),
            "role": role,
            "appearance_canonical": appearance,
            "personality": (obj.get("personality") or "").strip(),
            "speech_style": (obj.get("speech_style") or "").strip(),
            "relationships": rels,
            "source_evidence": verified_evidence,
            "rejected_evidence": rejected_evidence,
        }

    verdict = _build_verdict(await _assess_once(""))
    # 已确认真名却拿到过薄的人物卡、或标志性特征证据核验不通过时，做一次有界重试，
    # 而不是首轮不完整/不实就让整条剧本硬失败（这是与结构化输出同源的单点脆弱性）。
    # 重试提示按实际失败原因动态拼接，不是静态模板：证据核验不通过时明确给出"换一条
    # 真实证据"或"去掉这个特征只写通用形态"两条合法出路，不再逼模型必须保留一个
    # 标志性特征（那正是王有材事故的激励结构）。
    if require_identity_card and (not verdict["card_complete"] or verdict["rejected_evidence"]):
        reasons: list[str] = []
        if not verdict["card_complete"]:
            reasons.append(
                f"appearance_canonical 不完整（当前 {len(verdict['appearance_canonical'])} 字，"
                f"要求 {APPEARANCE_MIN}~{APPEARANCE_MAX} 字；或 role 不是 主角/重要配角/反派 "
                "之一）。请重写为完整外观锚点，只写通用形态（性别年龄感/发型发色/服装款式与"
                "颜色）即可满足长度要求，不必强行加标志性特征。"
            )
        if verdict["rejected_evidence"]:
            detail = "；".join(
                f"第 {item.get('evidence_chapter_index')} 章引句「{item.get('evidence_quote')}」"
                for item in verdict["rejected_evidence"]
            )
            reasons.append(
                "以下标志性特征引用的证据未通过核验（可能不是该章逐字原文、超过 40 字、或角色"
                f"本人姓名没有出现在这句引文本身里）：{detail}。请换一条真实、40 字以内、且角色"
                "本人姓名与所写特征同句出现的原文逐字短句作为新证据；找不到就直接去掉这个"
                "标志性特征，appearance_canonical 只保留通用形态，这同样是合法结果，不要为了"
                "保住这个特征而勉强凑一条证据。"
            )
        retry_instruction = (
            "\n\n上一轮 appearance_canonical 不完整，需要修正：\n"
            + "\n".join(reasons)
            + "\n并固定 important=true。"
        )
        verdict = _build_verdict(await _assess_once(retry_instruction))
    return verdict


async def ensure_character_card(
    project_id: str,
    name: str,
    from_episode_no: int,
    *,
    generate_portrait: bool = True,
    require_identity_card: bool = False,
    write_guard: Callable[[], None] | None = None,
) -> dict:
    """检查新角色的原文份量，并自动完成建卡与定妆包。

    默认由 AI 判断是否需要跨镜头保持一致；若上游身份模型已确认稳定真名，
    ``require_identity_card`` 会要求模型完成最小人物卡，不能再以戏份少降为路人。
    一次性功能角色仍跳过。建卡先落库，定妆包生成失败时保留卡片并由分镜前
    的自愈步骤重试，不再暴露人工待审队列。带 (project,name) 锁，可幂等并发。
    """
    name = (name or "").strip()
    if not name:
        return {"status": "skipped", "reason": "empty"}
    if write_guard:
        write_guard()
    conn = get_conn()
    if _name_in_bible(conn, project_id, name):
        return {"status": "exists", "name": name}
    lock = await _card_lock(project_id, name)
    async with lock:
        if write_guard:
            write_guard()
        if _name_in_bible(conn, project_id, name):  # 拿到锁后复查（并发兜底）
            return {"status": "exists", "name": name}
        if not _has_column(conn, "projects", "bible_auto_changes_json"):
            conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
        pending_row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            change_items = json.loads(pending_row["bible_auto_changes_json"] or "[]") if pending_row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            change_items = []
        existing_change = next((
            item for item in change_items
            if item.get("kind") in {"new_character", "character_discovery", "new_bible_character"}
            and item.get("character") == name
            and item.get("status") in {"pending", "processing", "auto_applied_asset_failed"}
        ), None)
        # 负缓存：近 DISCOVERY_REJUDGE_WINDOW 集内判过"戏份不足"就先不重判；隔得够远会重新评估
        # （龙套后期可能转重要）。
        skip_raw = get_setting(_discovery_skip_key(project_id, name))
        if skip_raw and existing_change is None and not require_identity_card:
            try:
                last = int(skip_raw)
            except (TypeError, ValueError):
                last = 0
            if 0 < from_episode_no - last < DISCOVERY_REJUDGE_WINDOW:
                return {"status": "skipped_minor", "name": name, "reason": "recently judged minor"}
        bible_artifact_supported = _has_column(conn, "projects", "bible_artifact_id")
        select_cols = "bible_json, bible_version"
        if bible_artifact_supported:
            select_cols += ", bible_artifact_id"
        project = conn.execute(
            f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if not project or not project["bible_json"]:
            return {"status": "skipped", "name": name, "reason": "no bible"}
        bible = Bible.model_validate(json.loads(project["bible_json"]))
        style = bible.world.visual_style_canonical
        known = [c.name for c in bible.characters]
        fragments, ep_label, forward_chapters_by_idx = _forward_fragments(
            conn, project_id, name, from_episode_no
        )
        if existing_change is not None:
            change_payload = (
                existing_change.get("payload")
                if isinstance(existing_change.get("payload"), dict) else {}
            )
            try:
                char_obj = Character.model_validate(change_payload.get("character_card"))
            except ValidationError as exc:
                return {"status": "error", "name": name, "reason": f"pending card invalid {exc}"[:240]}
            verdict = {
                "reason": existing_change.get("reason") or "AI 已判定为需要跨镜头保持的新角色",
            }
        else:
            if not fragments:
                # 原文里根本检索不到这个名字（多半是剧本臆造/称谓）。
                if require_identity_card:
                    return {
                        "status": "error", "name": name,
                        "reason": "真名已确认，但人物卡缺少可核验的原文片段",
                    }
                set_setting(_discovery_skip_key(project_id, name), str(from_episode_no))
                return {"status": "skipped_minor", "name": name, "reason": "no fragments in novel"}
            try:
                assessment_options = {
                    "style": style,
                    "known_names": known,
                    "ep_label": ep_label,
                    "chapters_by_idx": forward_chapters_by_idx,
                }
                if require_identity_card:
                    assessment_options["require_identity_card"] = True
                verdict = await assess_new_character(
                    name, fragments, **assessment_options,
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "name": name,
                        "reason": "新角色评估失败" + code_ref(exc, action="assess_new_character",
                                                              context={"project_id": project_id, "name": name})}
            if write_guard:
                write_guard()
            card_complete = bool(verdict.get("card_complete")) or (
                bool(str(verdict.get("role") or "").strip())
                and APPEARANCE_MIN
                <= len(str(verdict.get("appearance_canonical") or "").strip())
                <= APPEARANCE_MAX
            )
            # 以 subject_kind 为准（_build_verdict 一定会给出它），缺失同样拦截：
            # 无法断言"这是一个人"时，保守的方向就是不进人物谱。
            if str(
                verdict.get("subject_kind") or ""
            ).strip() != CHARACTER_SUBJECT_PERSON:
                # 人格是独立的硬闸门，不能被 require_identity_card 绕过：身份消歧
                # 确认的是"这是一个稳定的专名"，不是"这是一个人"。宗门、器物、
                # 地点即使专名稳定、戏份很重，也只能留在场景库/reference 身份里。
                set_setting(
                    _discovery_skip_key(project_id, name), str(from_episode_no)
                )
                set_setting(_non_character_skip_key(project_id, name), "1")
                return {
                    "status": "skipped_not_person",
                    "name": name,
                    "subject_kind": verdict.get("subject_kind") or "",
                    "reason": verdict["reason"],
                }
            if not verdict["important"] and not (
                require_identity_card and card_complete
            ):
                if require_identity_card:
                    return {
                        "status": "error", "name": name,
                        "reason": "身份模型已确认真名，但人物卡模型未返回完整稳定卡片",
                    }
                set_setting(_discovery_skip_key(project_id, name), str(from_episode_no))
                return {"status": "skipped_minor", "name": name, "reason": verdict["reason"]}
            try:
                char_obj = Character.model_validate({
                    "name": name, "role": verdict["role"],
                    "appearance_canonical": verdict["appearance_canonical"],
                    "personality": verdict["personality"], "speech_style": verdict["speech_style"],
                    "relationships": verdict["relationships"], "portrait_prompt_override": None,
                    "source_evidence": verdict.get("source_evidence") or []})
            except ValidationError as exc:
                return {"status": "error", "name": name, "reason": f"card invalid {exc}"[:240]}

        # 保留内部追溯记录，但不再把它当成用户待审任务。
        existing = existing_change
        if existing is None:
            evidence_fragments = [
                part.strip() for part in fragments.split("\n……\n") if part.strip()
            ][:6]
            existing = {
                "id": new_id("change"),
                "kind": "new_character",
                "status": "processing",
                "character": name,
                "ep_start": from_episode_no,
                "reason": verdict["reason"],
                "created_at": now(),
                "payload": {
                    "character_card": char_obj.model_dump(mode="json"),
                    "source_episode": from_episode_no,
                    "source_episode_label": ep_label,
                    "evidence_fragments": evidence_fragments,
                },
            }
            change_items.append(existing)
        else:
            existing["status"] = "processing"
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(change_items, ensure_ascii=False), project_id),
        )
        conn.commit()

        card = char_obj.model_dump(mode="json")
        bible_lock = await _bible_lock(project_id)
        async with bible_lock:
            if write_guard:
                write_guard()
            appended = _append_character_to_bible(conn, project_id, card)
        if not appended and not _name_in_bible(conn, project_id, name):
            existing["status"] = "auto_apply_failed"
            existing["decision_reason"] = "人物卡写入失败"
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {"status": "error", "name": name, "reason": "character card commit failed"}
        set_setting(_discovery_skip_key(project_id, name), "")

        if not generate_portrait:
            existing["status"] = "auto_applied_asset_pending"
            existing["decided_at"] = now()
            existing["decision_reason"] = "人物卡已加入；定妆包等待独立资产环节确认"
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {
                "status": "added",
                "name": name,
                "change_id": existing["id"],
                "has_portrait": False,
                "portrait_deferred": True,
                "reason": verdict["reason"],
                "character_card": card,
            }

        latest = conn.execute(
            "SELECT bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            portrait = await _generate_discovered_character_portrait(
                project_id,
                name,
                style,
                char_obj.appearance_canonical,
                ep_start=from_episode_no,
                bible_version=int(latest["bible_version"] or 0) if latest else 0,
            )
        except Exception as exc:  # noqa: BLE001 -- 卡片仍可约束剧本，分镜前自动重试资产
            public = code_ref(
                exc,
                action="auto_generate_discovered_character_portrait",
                context={"project_id": project_id, "name": name, "episode_no": from_episode_no},
            )
            existing["status"] = "auto_applied_asset_failed"
            existing["decided_at"] = now()
            existing["decision_reason"] = public
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {
                "status": "added", "name": name, "change_id": existing["id"],
                "has_portrait": False, "reason": verdict["reason"],
                "portrait_error": public, "character_card": card,
            }

        if write_guard:
            write_guard()
        existing["status"] = "auto_applied"
        existing["decided_at"] = now()
        existing["decision_reason"] = "AI 判定需要人物卡并已自动生成定妆包"
        existing.setdefault("payload", {})["portrait_id"] = portrait.get("portrait_id")
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(change_items, ensure_ascii=False), project_id),
        )
        conn.commit()
        return {
            "status": "added", "name": name, "change_id": existing["id"],
            "has_portrait": True, "reason": verdict["reason"],
            "character_card": card, **portrait,
        }


def bible_with_provisional_characters(bible: Bible, discovery: dict | None) -> Bible:
    """兼容旧运行记录：把历史临时人物注入当前剧本生成上下文。

    新流程会在发现阶段直接自动入卡；此函数只用于断点续跑的向后兼容。
    """
    cards = (discovery or {}).get("provisional_characters") or []
    if not cards:
        return bible
    characters = list(bible.characters)
    known = {character.name for character in characters}
    for card in cards:
        if not isinstance(card, dict):
            continue
        try:
            character = Character.model_validate(card)
        except ValidationError:
            continue
        if character.name in known:
            continue
        characters.append(character)
        known.add(character.name)
    return bible.model_copy(update={"characters": characters})


def bible_with_pending_characters_for_text(
    project_id: str,
    bible: Bible,
    text: str,
) -> Bible:
    """恢复/续跑时从历史队列恢复本章实际出现的临时人物约束。

    这是只读的旧数据兼容路径，不触发出图。
    """
    if not (text or "").strip():
        return bible
    conn = get_conn()
    if not _has_column(conn, "projects", "bible_auto_changes_json"):
        return bible
    row = conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    try:
        items = json.loads(row["bible_auto_changes_json"] or "[]") if row else []
    except (TypeError, ValueError, json.JSONDecodeError):
        items = []
    cards: list[dict] = []
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("status") != "pending"
            or item.get("kind") not in {"new_character", "character_discovery", "new_bible_character"}
        ):
            continue
        name = str(item.get("character") or "").strip()
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        card = payload.get("character_card")
        if name and name in text and isinstance(card, dict):
            cards.append(card)
    return bible_with_provisional_characters(
        bible, {"provisional_characters": cards},
    )


def _episode_source_text(conn, project_id: str, episode_no: int) -> str:
    """本集对应源章节的正文（按集做漂移判定的依据）。"""
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, episode_no)).fetchone()
    src = json.loads(ep["source_chapters"] or "[]") if ep and ep["source_chapters"] else []
    if not src:
        return ""
    has_title = _has_column(conn, "chapters", "title")
    select_cols = "idx, content" + (", title" if has_title else "")
    rows = conn.execute(
        f"SELECT {select_cols} FROM chapters WHERE project_id=? AND idx>=? AND idx<=? ORDER BY idx",
        (project_id, min(src), max(src))).fetchall()
    if has_title and len(rows) == 1 and chapter_is_stub(dict(rows[0])):
        following = conn.execute(
            "SELECT idx, title, content FROM chapters WHERE project_id=? AND idx>? ORDER BY idx LIMIT 1",
            (project_id, rows[0]["idx"]),
        ).fetchone()
        if following and not chapter_is_stub(dict(following)) and chapter_titles_match(dict(rows[0]), dict(following)):
            rows = [following]
    return "\n".join((r["content"] or "") for r in rows)


def _update_bible_appearance(conn, project_id: str, name: str, appearance: str, ref_image_path: str) -> None:
    """漂移重绘后把 bible 里该角色的外观锚点/参考图同步成最新版（供人物谱 UI 展示）。
    真正驱动按集渲染的是 character_portraits 分段表 + bible_for_episode 的本集视图，所以这里只是展示用。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return
    data = json.loads(row["bible_json"])
    for c in data.get("characters", []):
        if c.get("name") == name:
            c["appearance_canonical"] = appearance
            c["ref_image_path"] = ref_image_path
            break
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?", (json.dumps(data, ensure_ascii=False), project_id))


def reconcile_bible_display_appearances(conn, project_id: str) -> list[str]:
    """Keep the project card on each character's current persistent portrait segment."""
    row = conn.execute(
        "SELECT bible_json FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if not row or not row["bible_json"]:
        return []
    data = json.loads(row["bible_json"])
    changed: list[str] = []
    for character in data.get("characters", []):
        name = str(character.get("name") or "").strip()
        if not name:
            continue
        portrait = _open_portrait(conn, project_id, name)
        if portrait is None:
            continue
        appearance = str(portrait["appearance"] or "").strip()
        image_path = str(portrait["image_path"] or "").strip()
        if appearance and character.get("appearance_canonical") != appearance:
            character["appearance_canonical"] = appearance
            changed.append(name)
        if image_path and character.get("ref_image_path") != image_path:
            character["ref_image_path"] = image_path
            if name not in changed:
                changed.append(name)
    if changed:
        conn.execute(
            "UPDATE projects SET bible_json=? WHERE id=?",
            (json.dumps(data, ensure_ascii=False), project_id),
        )
        conn.commit()
    return changed


async def _refresh_portrait_on_drift(project_id: str, name: str, episode_no: int,
                                     new_appearance: str, style: str, bible_version: int,
                                     *, change_meta: dict | None = None) -> dict | None:
    """外观明显变化：先在临时状态生成完整多视角包，整包 QA 通过后同一事务关闭旧区间并启用新区间。
    返回 {ep_start, image_path, pack_status} 或 None。"""
    lock = await _card_lock(project_id, name)
    async with lock:
        conn = get_conn()
        cur = _open_portrait(conn, project_id, name)
        if not cur or cur["ep_start"] >= episode_no:
            return None  # 并发已处理，或本集（之后）才登场的图，无需切分
        new_path, new_prompt = await _redraw_portrait(
            project_id, name, style, new_appearance, base_path=cur["image_path"], ep_start=episode_no)
        persistence = (change_meta or {}).get("persistence") or "persistent"
        artifact_supported = _has_column(conn, "character_portraits", "artifact_id")
        pack_supported = _has_column(conn, "character_portraits", "pack_status")
        artifact = None
        qa = None
        if artifact_supported:
            project = conn.execute(
                "SELECT bible_artifact_id FROM projects WHERE id=?", (project_id,)
            ).fetchone()
            parent_ids = [
                artifact_id for artifact_id in (
                    cur["artifact_id"], project["bible_artifact_id"] if project else None,
                ) if artifact_id
            ]
            for attempt in range(1, 3):
                qa = await _review_portrait_asset(new_path, new_appearance)
                artifact = record_reference_asset(
                    asset_type="character_portrait",
                    scope_id=f"{project_id}:{name}:{episode_no}",
                    file_path=new_path,
                    content={"character_name": name, "appearance": new_appearance,
                             "prompt": new_prompt, "episode_start": episode_no,
                             "attempt": attempt, "change": change_meta or {}},
                    parent_artifact_ids=parent_ids,
                    qa=qa,
                )
                if artifact["status"] == "approved":
                    break
                if attempt < 2:
                    new_path, new_prompt = await _redraw_portrait(
                        project_id, name, style, new_appearance,
                        base_path=cur["image_path"], ep_start=episode_no,
                    )
            if not artifact or artifact["status"] not in {"approved", "validated"}:
                # 新主图确实不可读时继续使用旧造型；不把内容 QA 变成终态。
                return {
                    "ep_start": int(cur["ep_start"] or 1),
                    "image_path": cur["image_path"],
                    "pack_status": cur["pack_status"] if pack_supported else "ready",
                    "portrait_id": cur["id"],
                    "gate_retry_exhausted": True,
                }

        stale_segment = conn.execute(
            "SELECT id,ep_end FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start=?",
            (project_id, name, episode_no),
        ).fetchone()
        if stale_segment and stale_segment["id"] != cur["id"]:
            stale_end = stale_segment["ep_end"]
            if stale_end is None or int(stale_end) >= episode_no:
                return None
            conn.execute(
                "DELETE FROM character_portraits WHERE id=?",
                (stale_segment["id"],),
            )

        new_portrait_id = new_id("portrait")
        change_json = json.dumps(change_meta or {}, ensure_ascii=False) if change_meta else None
        # 先插入临时段（不关闭旧区间）；整包通过后再原子切换
        if artifact_supported and pack_supported:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, change_json, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, episode_no,  # 临时：仅占本集，未生效
                 new_appearance, new_prompt, new_path, cur["id"], bible_version,
                 artifact["id"] if artifact else None, "generating", change_json, now()))
        elif artifact_supported:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, None, new_appearance,
                 new_prompt, new_path, cur["id"], bible_version, artifact["id"] if artifact else None, now()))
        else:
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_portrait_id, project_id, name, episode_no, None, new_appearance,
                 new_prompt, new_path, cur["id"], bible_version, now()))
        conn.commit()

        pack_status = "ready"
        if pack_supported:
            from app.multiview import (
                PACK_STATUS_FAILED,
                ensure_character_multiview_pack,
                pack_result_ok,
            )
            try:
                pack = await ensure_character_multiview_pack(
                    project_id=project_id,
                    portrait_id=new_portrait_id,
                    character_name=name,
                    appearance=new_appearance,
                    visual_style=style,
                    ep_start=episode_no,
                    base_portrait_id=cur["id"],
                    primary_qa=qa,
                )
            except Exception:
                conn.execute(
                    "UPDATE character_portraits SET ep_end=?,pack_status=? WHERE id=?",
                    (episode_no - 1, PACK_STATUS_FAILED, new_portrait_id),
                )
                conn.commit()
                raise
            if not pack_result_ok(pack):
                conn.execute(
                    "UPDATE character_portraits SET ep_end=?,pack_status=? WHERE id=?",
                    (episode_no - 1, PACK_STATUS_FAILED, new_portrait_id),
                )
                conn.commit()
                raise ContentGenerationError(f"角色多视角包结构不完整：{name}")
            pack_status = "ready"
            # 原子切换：关闭旧区间，开放新区间
            conn.execute("UPDATE character_portraits SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
            new_ep_end = episode_no if persistence == "episode" else None
            conn.execute(
                "UPDATE character_portraits SET ep_end=?, pack_status=? WHERE id=?",
                (new_ep_end, pack_status, new_portrait_id),
            )
            # 若仅本集有效，结束后零付费重新绑定完整旧包（含全部视角，pack_status=ready）
            if persistence == "episode":
                from app.multiview import bind_ready_portrait_reuse
                bind_ready_portrait_reuse(
                    conn,
                    project_id=project_id,
                    character_name=name,
                    source_portrait_id=cur["id"],
                    ep_start=episode_no + 1,
                    bible_version=bible_version,
                )
            conn.commit()
        else:
            conn.execute("UPDATE character_portraits SET ep_end=? WHERE id=?", (episode_no - 1, cur["id"]))
            conn.commit()

        if persistence == "episode":
            _update_bible_appearance(
                conn,
                project_id,
                name,
                str(cur["appearance"] or ""),
                str(cur["image_path"] or ""),
            )
        else:
            _update_bible_appearance(conn, project_id, name, new_appearance, new_path)
        conn.commit()
        return {"ep_start": episode_no, "image_path": new_path, "pack_status": pack_status,
                "portrait_id": new_portrait_id}


def _backfill_matching_future_portrait(
    conn,
    *,
    project_id: str,
    name: str,
    episode_no: int,
    appearance: str,
) -> dict | None:
    """Extend an identical ready pack when discovery assigned a future start."""
    covered = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start<=? "
        "AND (ep_end IS NULL OR ep_end>=?) LIMIT 1",
        (project_id, name, episode_no, episode_no),
    ).fetchone()
    if covered:
        return None
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    pack_clause = "AND pack_status='ready'" if pack_supported else ""
    future = conn.execute(
        "SELECT * FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start>? "
        f"{pack_clause} ORDER BY ep_start ASC LIMIT 1",
        (project_id, name, episode_no),
    ).fetchone()
    if not future:
        return None
    if (future["appearance"] or "").strip() != (appearance or "").strip():
        return None
    image_path = str(future["image_path"] or "")
    if not image_path or not Path(image_path).is_file():
        return None
    same_start = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start=? AND id<>? LIMIT 1",
        (project_id, name, episode_no, future["id"]),
    ).fetchone()
    if same_start:
        return None
    original_start = int(future["ep_start"])
    conn.execute(
        "UPDATE character_portraits SET ep_start=? WHERE id=? AND ep_start=?",
        (episode_no, future["id"], original_start),
    )
    conn.commit()
    return {
        "name": name,
        "portrait_id": future["id"],
        "ep_start": episode_no,
        "previous_ep_start": original_start,
        "image_path": image_path,
        "pack_status": future["pack_status"] if pack_supported else "ready",
        "reused": True,
    }


async def ensure_cards_for_screenplay(project_id: str, episode_no: int, screenplay, bible) -> dict:
    """剧本就绪后（分镜展开前）反应式维护本集出场角色的定妆照：
      ① 剧本外身份在这里只做快速阻断，不再延迟到分镜阶段建卡；
      ② 已有角色漂移：剧本里出现、本集之前已有定妆照的角色 → 用本集源文判断外观是否相比当前锚点
         明显变化，变了就图生图重绘新段并把 bible 锚点同步成最新。
    逐项吞错——单角色失败不阻断分镜。返回 {checked, added:[...], redrawn:[...], errors:[...]}。"""
    bible_names = {c.name for c in bible.characters}
    names: list[str] = []
    seen: set[str] = set()

    def _collect(lst) -> None:
        for n in lst or []:
            n = (n or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)

    for sc in getattr(screenplay, "scene_outline", None) or []:
        _collect(getattr(sc, "characters", None))

    errors: list[str] = []

    # ① Narrative 路径只消费 typed resolver；legacy 仍保留旧分类器。
    narrative_authority = getattr(screenplay, "narrative_plan", None) is not None
    identity_by_token: dict[str, object] = {}
    resolver_error = ""
    if narrative_authority:
        from app.identity_contracts import (
            IdentityContractError,
            narrative_identity_resolver,
        )

        try:
            identity_resolver = narrative_identity_resolver(bible, screenplay)
            for name in names:
                identity_by_token[name] = identity_resolver.resolve(name, usage="visual")
        except IdentityContractError as exc:
            resolver_error = str(exc)
    unknown = (
        ([resolver_error] if resolver_error else [])
        if narrative_authority
        else [n for n in names if n not in bible_names]
    )
    added: list[dict] = []
    blocking_errors: list[str] = []
    if narrative_authority and resolver_error:
        blocking_errors.append(f"剧本 typed identity contract 未完成：{resolver_error}")
    elif not narrative_authority:
        blocking_errors.extend(
            f"剧本人物身份未完成：「{name}」未进入人物谱，也不是已编号的一次性角色；"
            "请回到剧本阶段重跑人物身份预检"
            for name in unknown
        )

    # 剧本阶段若遇到供应商短暂失败，人物卡已保留；分镜前对这些系统失败项
    # 自动补齐定妆包。这是内部自愈，不再转换为用户待审任务。
    conn = get_conn()
    # typed policy 要求资产的非 Bible 身份，直接使用合同的稳定视觉锚点
    # 建立本集定妆包。不需资产的一次性/群体/画外身份不会被名称规则误建卡。
    if narrative_authority and not resolver_error:
        project_row = conn.execute(
            "SELECT bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        bible_version = int(project_row["bible_version"] or 0) if project_row else 0
        generated_asset_ids: set[str] = set()
        for identity in identity_by_token.values():
            if not identity.requires_asset or identity.asset_name in bible_names:
                continue
            if identity.identity_id in generated_asset_ids:
                continue
            generated_asset_ids.add(identity.identity_id)
            try:
                card_lock = await _card_lock(project_id, identity.asset_name)
                async with card_lock:
                    portrait = await _generate_discovered_character_portrait(
                        project_id,
                        identity.asset_name,
                        bible.world.visual_style_canonical,
                        identity.visual_anchor(),
                        ep_start=episode_no,
                        bible_version=bible_version,
                    )
            except Exception as exc:  # noqa: BLE001 - required policy must fail closed
                public = code_ref(
                    exc,
                    action="ensure_narrative_identity_asset",
                    context={
                        "project_id": project_id,
                        "identity_id": identity.identity_id,
                        "episode_no": episode_no,
                    },
                )
                blocking_errors.append(
                    f"身份「{identity.display_name}」合同要求人物资产，但定妆包生成失败{public}"
                )
                continue
            added.append({
                "status": "added",
                "name": identity.display_name,
                "identity_id": identity.identity_id,
                "has_portrait": True,
                **portrait,
            })
    retry_changes: list[dict] = []
    if _has_column(conn, "projects", "bible_auto_changes_json"):
        change_row = conn.execute(
            "SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            all_changes = json.loads(change_row["bible_auto_changes_json"] or "[]") if change_row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            all_changes = []
        retry_changes = [
            item for item in all_changes
            if item.get("kind") in {"new_character", "character_discovery", "new_bible_character"}
            and item.get("status") in {
                "auto_applied_asset_failed",
                "auto_applied_asset_pending",
            }
            and item.get("character") in names
        ]
    else:
        all_changes = []
    if retry_changes:
        refreshed_project = conn.execute(
            "SELECT bible_json,bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        refreshed_bible = Bible.model_validate(json.loads(refreshed_project["bible_json"]))
        refreshed_by_name = {character.name: character for character in refreshed_bible.characters}
        for change in retry_changes:
            retry_name = str(change.get("character") or "").strip()
            character = refreshed_by_name.get(retry_name)
            if character is None:
                continue
            try:
                retry_lock = await _card_lock(project_id, retry_name)
                async with retry_lock:
                    portrait = await _generate_discovered_character_portrait(
                        project_id,
                        retry_name,
                        refreshed_bible.world.visual_style_canonical,
                        character.appearance_canonical,
                        ep_start=max(1, int(change.get("ep_start") or episode_no)),
                        bible_version=int(refreshed_project["bible_version"] or 0),
                    )
            except Exception as exc:  # noqa: BLE001
                public = code_ref(
                    exc,
                    action="retry_auto_character_portrait",
                    context={"project_id": project_id, "name": retry_name, "episode_no": episode_no},
                )
                change["decision_reason"] = public
                blocking_errors.append(f"{retry_name}：自动定妆包生成失败，系统重试后仍未就绪")
                continue
            change["status"] = "auto_applied"
            change["decided_at"] = now()
            change["decision_reason"] = "系统已在分镜前自动补齐定妆包"
            change.setdefault("payload", {})["portrait_id"] = portrait.get("portrait_id")
            if not any(item.get("name") == retry_name for item in added):
                added.append({"status": "added", "name": retry_name, "has_portrait": True, **portrait})
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(all_changes, ensure_ascii=False), project_id),
        )
        conn.commit()

    # 未来章节扫描可能先发现真实姓名，但当前集剧本已经使用该角色。
    # 若完整包外观与人物谱锚点完全一致，零付费向前扩展适用区间。
    backfilled: list[dict] = []
    by_name = {c.name: c for c in bible.characters}
    for name in (item for item in names if item in bible_names):
        result = _backfill_matching_future_portrait(
            conn,
            project_id=project_id,
            name=name,
            episode_no=episode_no,
            appearance=by_name[name].appearance_canonical,
        )
        if result:
            backfilled.append(result)

    # ② 已有角色按集漂移（只判本集之前就已有定妆照的角色；本集新建的天然是最新）
    src_text = _episode_source_text(conn, project_id, episode_no)
    entries: list[dict] = []
    if src_text:
        for n in (x for x in names if x in bible_names):
            cur = _open_portrait(conn, project_id, n)
            if not cur or cur["ep_start"] >= episode_no:
                continue
            frags = extract_character_fragments(src_text, n)
            if not frags:
                continue  # 本集没正面提到 → 沿用，开区间自然覆盖
            entries.append({"name": n, "fragments": frags,
                            "current_appearance": cur["appearance"] or by_name[n].appearance_canonical})

    redrawn: list[dict] = []
    if entries:
        proj = conn.execute("SELECT bible_version FROM projects WHERE id=?", (project_id,)).fetchone()
        bible_version = (proj["bible_version"] if proj else 0) or 0
        style = bible.world.visual_style_canonical
        try:
            verdicts = await screen_appearance_changes(entries, f"第 {episode_no} 集")
        except Exception as exc:  # noqa: BLE001 判定失败不阻断分镜
            verdicts = {}
            errors.append(f"漂移判定失败@第{episode_no}集"
                          + code_ref(exc, action="screen_appearance_changes",
                                     context={"project_id": project_id, "episode_no": episode_no}))
        for name, v in verdicts.items():
            try:
                res = await _refresh_portrait_on_drift(
                    project_id, name, episode_no, v["new_appearance"], style, bible_version,
                    change_meta={
                        "change_dimensions": v.get("change_dimensions") or [],
                        "persistence": v.get("persistence") or "persistent",
                        "reason": v.get("reason") or "",
                        "evidence_excerpt": v.get("evidence_excerpt") or "",
                    },
                )
            except Exception as exc:  # noqa: BLE001 单角色重绘失败不阻断分镜
                errors.append(f"{name}@第{episode_no}集重绘失败"
                              + code_ref(exc, action="refresh_portrait_on_drift",
                                         context={"project_id": project_id, "name": name, "episode_no": episode_no}))
                continue
            if res:
                redrawn.append({"name": name, "reason": v["reason"], **res})

    reconcile_bible_display_appearances(conn, project_id)

    return {
        "checked": len(unknown),
        "added": added,
        "backfilled": backfilled,
        "redrawn": redrawn,
        "errors": errors,
        "blocking_errors": blocking_errors,
    }


# ---------- 定妆照落盘 / 登记 ----------

async def _save_image_item(item: dict, dest: str) -> None:
    """把 hiagent.generate_image 的返回落盘到 dest（url 优先下载，其次写 b64）。"""
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def _portrait_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_portrait_path(project_id: str, name: str, ep_start: int) -> str:
    return str(
        _portrait_dir(project_id)
        / f"{_safe_name(name)}__ep{ep_start}__{new_id('candidate')}.jpg"
    )


async def _review_portrait_asset(image_path: str, appearance: str) -> dict:
    """对反应式人物锚点执行与初始定妆照相同的保守一致性门禁。"""
    from app.stages import review_portrait_image

    try:
        return await review_portrait_image(hiagent.encode_image_file(image_path), appearance)
    except Exception as exc:  # noqa: BLE001 评估失败不能伪装成通过
        return {
            "overall": 0.0,
            "issues": [f"角色一致性评估未完成：{type(exc).__name__}"],
            "qa_recovered": True,
        }


def register_initial_portrait(conn, project_id: str, name: str, image_path: str,
                              appearance: str, prompt: str, bible_version: int,
                              artifact_id: str | None = None) -> str:
    """初次定妆后登记角色首张定妆照（适用集 1~ 至今）。覆盖式：先清掉该角色全部旧分段。"""
    conn.execute("DELETE FROM character_portraits WHERE project_id=? AND character_name=?",
                 (project_id, name))
    portrait_id = new_id("portrait")
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    if pack_supported:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, 1, None, appearance, prompt, image_path, None,
             bible_version, artifact_id, "legacy_partial", now()))
    else:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, 1, None, appearance, prompt, image_path, None,
             bible_version, artifact_id, now()))
    conn.commit()
    return portrait_id


def stage_initial_portrait(conn, project_id: str, name: str, image_path: str,
                           appearance: str, prompt: str, bible_version: int,
                           artifact_id: str | None = None) -> str:
    """暂存新的初始定妆包，不提前删除当前已采用包。

    STAGED_INITIAL_EP_START 是仅供生成/QA 使用的候选槽位，不会命中任何
    真实集号；整包验收通过后再由
    promote_staged_initial_portrait 以单个事务替换 ep_start=1 的当前包。
    """
    current = conn.execute(
        "SELECT id FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_end IS NULL AND ep_start<>? ORDER BY created_at DESC LIMIT 1",
        (project_id, name, STAGED_INITIAL_EP_START),
    ).fetchone()
    base_portrait_id = current["id"] if current else None
    conn.execute(
        "DELETE FROM character_portraits WHERE project_id=? AND character_name=? AND ep_start=?",
        (project_id, name, STAGED_INITIAL_EP_START),
    )
    portrait_id = new_id("portrait")
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    if pack_supported:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, pack_status, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, STAGED_INITIAL_EP_START, None, appearance, prompt, image_path, base_portrait_id,
             bible_version, artifact_id, "legacy_partial", now()),
        )
    else:
        conn.execute(
            "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
            "prompt, image_path, base_portrait_id, bible_version, artifact_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (portrait_id, project_id, name, STAGED_INITIAL_EP_START, None, appearance, prompt, image_path, base_portrait_id,
             bible_version, artifact_id, now()),
        )
    conn.commit()
    return portrait_id


def promote_staged_initial_portrait(conn, project_id: str, name: str, portrait_id: str) -> None:
    """整包验收通过后原子发布为全局初始定妆。

    手工重新定妆与剧情中的分集造型演进是两种操作：前者必须从第 1 集
    起替换全时间线，后者由 ``_refresh_portrait_on_drift`` 继续维护分段。
    """
    row = conn.execute(
        "SELECT id FROM character_portraits "
        "WHERE id=? AND project_id=? AND character_name=? AND ep_start=?",
        (portrait_id, project_id, name, STAGED_INITIAL_EP_START),
    ).fetchone()
    if not row:
        raise ValueError(f"定妆候选不存在：{name}")
    with conn:
        previous = conn.execute(
            "SELECT id FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND id<>? AND ep_start>0 "
            "ORDER BY ep_start, created_at",
            (project_id, name, portrait_id),
        ).fetchall()
        minimum = conn.execute(
            "SELECT MIN(ep_start) AS value FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=0",
            (project_id, name),
        ).fetchone()
        history_start = int(
            minimum["value"] if minimum and minimum["value"] is not None else 0
        ) - len(previous)
        for offset, previous_row in enumerate(previous):
            conn.execute(
                "UPDATE character_portraits SET ep_start=?, ep_end=0 WHERE id=?",
                (history_start + offset, previous_row["id"]),
            )
        conn.execute(
            "UPDATE character_portraits SET ep_start=1, ep_end=NULL WHERE id=?",
            (portrait_id,),
        )


def _open_portrait(
    conn, project_id: str, name: str, *, visual_entity_id: str | None = None
):
    """该角色当前开区间（ep_end IS NULL）的最新定妆照。

    ``visual_entity_id`` 非空时优先按视觉实体 ID 查询（跨集稳定，覆盖未
    具名角色，见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.2）；未命中，
    或该列尚未迁移落地（``sqlite3.OperationalError``），回退到既有的
    ``character_name`` 路径——迁移期双轨并存，向后兼容具名角色的既有行为。
    """
    if visual_entity_id:
        try:
            row = conn.execute(
                "SELECT * FROM character_portraits WHERE project_id=? "
                "AND visual_entity_id=? AND ep_end IS NULL AND ep_start<>? "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, visual_entity_id, STAGED_INITIAL_EP_START),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            return row
    return conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_end IS NULL AND ep_start<>? ORDER BY ep_start DESC LIMIT 1",
        (project_id, name, STAGED_INITIAL_EP_START)).fetchone()


def portrait_for_episode(
    project_id: str,
    name: str,
    episode_no: int | None,
    *,
    visual_entity_id: str | None = None,
) -> str | None:
    """返回覆盖该集的定妆照落盘路径；未命中返回 None（调用方回退到 bible.ref_image_path）。

    ``visual_entity_id`` 非空时优先按视觉实体 ID 查询——同一视觉实体跨集
    复用同一张脸，不受该集本次称谓/是否已具名影响（设计文档 §4.2）；未
    命中（含该列尚未迁移落地）时回退到既有的 ``character_name`` 路径。
    """
    if episode_no is None:
        return None
    if visual_entity_id:
        try:
            row = get_conn().execute(
                "SELECT image_path FROM character_portraits "
                "WHERE project_id=? AND visual_entity_id=? AND ep_start<=? "
                "AND (ep_end IS NULL OR ep_end>=?) "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, visual_entity_id, episode_no, episode_no),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row["image_path"] and Path(row["image_path"]).exists():
            return row["image_path"]
    try:
        row = get_conn().execute(
            "SELECT image_path FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row and row["image_path"] and Path(row["image_path"]).exists():
        return row["image_path"]
    return None


def appearance_for_episode(
    project_id: str,
    name: str,
    episode_no: int | None,
    *,
    visual_entity_id: str | None = None,
) -> str | None:
    """返回覆盖该集的定妆照有效外观锚点。

    ``appearance`` 是验收时单独持久化的结构化外观权威；不得再从
    prompt 文案中按关键词反向提取。``visual_entity_id`` 语义同
    ``portrait_for_episode``：优先按视觉实体 ID 查询，未命中回退
    ``character_name``。
    """
    if episode_no is None:
        return None
    if visual_entity_id:
        try:
            row = get_conn().execute(
                "SELECT appearance,prompt FROM character_portraits "
                "WHERE project_id=? AND visual_entity_id=? AND ep_start<=? "
                "AND (ep_end IS NULL OR ep_end>=?) "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, visual_entity_id, episode_no, episode_no),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row:
            anchor = production_appearance_anchor(row["appearance"] or "")
            if anchor:
                return anchor
    try:
        row = get_conn().execute(
            "SELECT appearance,prompt FROM character_portraits "
            "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
            "ORDER BY ep_start DESC LIMIT 1",
            (project_id, name, episode_no, episode_no)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    return production_appearance_anchor(row["appearance"] or "") or None


def bible_for_episode(project_id: str, bible: "Bible", episode_no: int | None) -> "Bible":
    """返回 bible 的【本集视图】：每个角色的 appearance_canonical / ref_image_path 用覆盖该集的分段
    定妆照覆盖（未命中保留原值）。让关键帧文字锚点与参考图同段同源——同一集永远是同一套外观描述+图。

    已具名角色的 ``visual_entity_id`` 与其命名权威同构（``bible:{name}``，
    设计文档 §4.2 "已具名分支……零迁移成本"），无需 ``Character`` 新增字段
    即可派生：优先经由 ``visual_entity_id_for_resolution`` 计算（依赖尚未
    落地时回退等价的字面量拼接），把查图路径切到按视觉实体 ID 查询——
    对已生效的具名绑定行为不变，同时是向"未具名角色也能查到同一张脸"
    过渡的必要一步（本函数目前仍只遍历 ``bible.characters``，functional
    extras 的接入点在 app/production/prep_pack.py，属另一模块边界）。
    """
    if episode_no is None:
        return bible
    view = bible.model_copy(deep=True)
    for c in view.characters:
        if not c.name:
            continue
        visual_entity_id = (
            _visual_entity_id_for_resolution_safe({
                "resolution": "future_identity",
                "canonical_name": c.name,
            })
            or f"bible:{c.name}"
        )
        anchor = appearance_for_episode(
            project_id, c.name, episode_no, visual_entity_id=visual_entity_id
        )
        if anchor:
            c.appearance_canonical = anchor
        img = portrait_for_episode(
            project_id, c.name, episode_no, visual_entity_id=visual_entity_id
        )
        if img:
            c.ref_image_path = img
    return view


def portrait_views_for_episode(project_id: str, name: str, episode_no: int | None, *, ready_only: bool = False):
    """本集有效人物多视角包；供新链路使用。"""
    from app.multiview import portrait_views_for_episode as _views
    return _views(project_id, name, episode_no, ready_only=ready_only)


def redraw_prompt(style: str, appearance: str) -> str:
    """图生图重绘提示词：以参考图（旧定妆照）为身份锚点，只按新外观调整。"""
    return (
        f"{style}。参考图是同一角色的既有定妆照，请在保持【同一个人、同一角色身份】的前提下，"
        f"按新外观重绘其全身定妆照：{appearance}。"
        "正面站立，中性表情，双臂自然下垂，纯浅米色背景，全身完整可见，无文字无水印"
    )


async def _redraw_portrait(project_id: str, name: str, style: str, appearance: str,
                           *, base_path: str | None, ep_start: int) -> tuple[str, str]:
    """以上一张定妆照为底【图生图】重绘新定妆照，落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = redraw_prompt(style, appearance)
    image_inputs = None
    if base_path and Path(base_path).exists():
        image_inputs = [hiagent.data_url_from_file(base_path)]
    item = await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        image_inputs=image_inputs,
        call_meta={
            "asset_kind": "portrait",
            "character_name": name,
            "episode_no": ep_start,
            "portrait_mode": "redraw",
        })
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt


async def _generate_fresh_portrait(project_id: str, name: str, style: str, appearance: str,
                                   *, ep_start: int) -> tuple[str, str]:
    """为新登场角色生成一张全新定妆照（无底图，不走图生图），落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = portrait_prompt(style, appearance)
    item = await hiagent.generate_image(
        prompt,
        size=config.REF_IMAGE_SIZE,
        call_meta={
            "asset_kind": "portrait",
            "character_name": name,
            "episode_no": ep_start,
            "portrait_mode": "fresh",
        })
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt


def _append_character_to_bible(conn, project_id: str, char: dict) -> bool:
    """Atomically append a discovered character and advance bible lineage/version."""
    artifact_supported = (
        _has_column(conn, "projects", "bible_artifact_id")
        and _has_table(conn, "artifacts")
    )
    select_cols = "bible_json, bible_version"
    if artifact_supported:
        select_cols += ", bible_artifact_id"
    row = conn.execute(f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    if char.get("name") in {c.get("name") for c in data.get("characters", [])}:
        return False
    data.setdefault("characters", []).append(char)
    payload = json.dumps(data, ensure_ascii=False)
    next_artifact_id = None
    if artifact_supported:
        try:
            previous_id = row["bible_artifact_id"]
            artifact = evidence_repository.create_artifact(EvidenceArtifact(
                type="character_bible",
                scope_type="project",
                scope_id=project_id,
                status="approved",
                trust_level="T2",
                content=data,
                parent_artifact_ids=[previous_id] if previous_id else [],
                contract_version="character-bible-1.0.0",
                prompt_version="incremental-character-discovery-1.0.0",
                model_snapshot={"operation": "incremental_add", "character_name": char.get("name")},
            ))
            next_artifact_id = artifact["id"]
        except Exception as exc:  # noqa: BLE001 - authority mutation must fail closed
            code_ref(
                exc,
                action="append_character_bible_artifact",
                context={"project_id": project_id, "character_name": char.get("name")},
            )
            return False
    expected_version = int(row["bible_version"] or 0)
    if artifact_supported:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=?,bible_artifact_id=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (
                payload,
                expected_version + 1,
                next_artifact_id,
                project_id,
                expected_version,
            ),
        )
    else:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (payload, expected_version + 1, project_id, expected_version),
        )
    conn.commit()
    return cursor.rowcount == 1


async def _generate_discovered_character_portrait(
    project_id: str,
    name: str,
    style: str,
    appearance: str,
    *,
    ep_start: int,
    bible_version: int,
) -> dict:
    """为后续剧情自动发现的角色生成并原子接入定妆包。

    Score-only（PRD QA-SO #15）：第一张技术有效主图即可接入；QA 只评分，
    不因低分重生。多视角包完整性只看必需视角文件是否齐全。
    """
    conn = get_conn()
    pack_supported = _has_column(conn, "character_portraits", "pack_status")
    candidate = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_start=? ORDER BY created_at DESC LIMIT 1",
        (project_id, name, ep_start),
    ).fetchone()

    async def _complete_candidate(
        row,
        *,
        primary_qa: dict | None = None,
        purge_on_failure: bool,
    ) -> dict:
        """补齐并发布同一个候选；重启恢复时不得再占用相同分段键。"""
        portrait_id = str(row["id"])
        image_path = str(row["image_path"] or "")
        candidate_appearance = str(row["appearance"] or appearance)
        try:
            if pack_supported:
                from app.multiview import ensure_character_multiview_pack, pack_result_ok

                existing_status = str(row["pack_status"] or "")
                if existing_status == "ready":
                    pack = {"status": "ready", "portrait_id": portrait_id, "reused": True}
                else:
                    pack = await ensure_character_multiview_pack(
                        project_id=project_id,
                        portrait_id=portrait_id,
                        character_name=name,
                        appearance=candidate_appearance,
                        visual_style=style,
                        ep_start=ep_start,
                        base_portrait_id=row["base_portrait_id"],
                        primary_qa=primary_qa,
                    )
                if not pack_result_ok(pack):
                    conn.execute(
                        "UPDATE character_portraits SET pack_status='failed' WHERE id=?",
                        (portrait_id,),
                    )
                    conn.commit()
                    raise ContentGenerationError(f"角色多视角包结构不完整：{name}")

                # 候选在多视角完成前只占本集闭区间。发布时再原子切换为开区间；
                # 服务重启后重复执行本段仍更新同一 portrait_id，不会触发唯一键冲突。
                current = _open_portrait(conn, project_id, name)
                if current and current["id"] != portrait_id:
                    if int(current["ep_start"] or 1) < ep_start:
                        conn.execute(
                            "UPDATE character_portraits SET ep_end=? WHERE id=?",
                            (ep_start - 1, current["id"]),
                        )
                    else:
                        conn.execute("DELETE FROM character_portraits WHERE id=?", (current["id"],))
                conn.execute(
                    "UPDATE character_portraits SET ep_end=NULL,pack_status=? WHERE id=?",
                    ("ready", portrait_id),
                )
                conn.commit()

            _update_bible_appearance(conn, project_id, name, candidate_appearance, image_path)
            conn.commit()
        except Exception:
            # 新候选在本调用内失败可沿用原清理语义；重启前已经付费落盘的候选必须保留，
            # 让下一次恢复继续使用，不能因为恢复代码自身异常再次烧图。
            if purge_on_failure:
                from app.rejected_media import purge_character_portrait
                purge_character_portrait(conn, portrait_id)
            raise
        return {
            "portrait_id": portrait_id,
            "image_path": image_path,
            "pack_status": "ready",
            "reused": not purge_on_failure,
            "gate_retry_exhausted": False,
        }

    # 服务重启可能发生在主图和候选行已落盘、侧视角尚未完成之间。此时该行以
    # ep_start=ep_end 占用候选槽；必须在原 portrait_id 上续补，不能重生主图后重复 INSERT。
    if candidate is not None:
        candidate_path = str(candidate["image_path"] or "")
        if candidate_path and Path(candidate_path).is_file():
            return await _complete_candidate(candidate, purge_on_failure=False)
        from app.rejected_media import purge_character_portrait
        purge_character_portrait(conn, str(candidate["id"]))

    current = _open_portrait(conn, project_id, name)
    if current and current["image_path"] and Path(current["image_path"]).is_file():
        current_pack = current["pack_status"] if pack_supported else "ready"
        if current_pack == "ready" and int(current["ep_start"] or 1) <= ep_start:
            return {
                "portrait_id": current["id"], "image_path": current["image_path"],
                "pack_status": "ready", "reused": True,
            }

    artifact_supported = (
        _has_column(conn, "character_portraits", "artifact_id")
        and _has_table(conn, "artifacts")
    )
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    parent_ids = []
    if project and "bible_artifact_id" in project.keys() and project["bible_artifact_id"]:
        parent_ids.append(project["bible_artifact_id"])

    artifact = None
    qa = None
    image_path, prompt = await _generate_fresh_portrait(
        project_id, name, style, appearance, ep_start=ep_start,
    )
    if artifact_supported:
        qa = await _review_portrait_asset(image_path, appearance)
        artifact = record_reference_asset(
            asset_type="character_portrait",
            scope_id=f"{project_id}:{name}:{ep_start}",
            file_path=image_path,
            content={
                "character_name": name,
                "appearance": appearance,
                "prompt": prompt,
                "episode_start": ep_start,
                "attempt": 1,
                "origin": "automatic_character_discovery",
            },
            parent_artifact_ids=parent_ids,
            qa=qa,
        )
        if artifact["status"] not in {"approved", "validated"}:
            if current:
                return {
                    "portrait_id": current["id"], "image_path": current["image_path"],
                    "pack_status": current["pack_status"] if pack_supported else "ready",
                    "reused": True, "gate_retry_exhausted": True,
                }
            raise hiagent.ProviderError(f"新角色定妆照文件不可用：{name}")

    portrait_id = new_id("portrait")
    values = {
        "id": portrait_id,
        "project_id": project_id,
        "character_name": name,
        "ep_start": ep_start,
        # 多视角尚未通过时只占本集候选槽，不开放右区间。
        "ep_end": ep_start if pack_supported else None,
        "appearance": appearance,
        "prompt": prompt,
        "image_path": image_path,
        "base_portrait_id": current["id"] if current else None,
        "bible_version": bible_version,
        "created_at": now(),
    }
    if _has_column(conn, "character_portraits", "artifact_id"):
        values["artifact_id"] = artifact["id"] if artifact else None
    if pack_supported:
        values["pack_status"] = "generating"
    columns = list(values)
    conn.execute(
        f"INSERT INTO character_portraits({', '.join(columns)}) "
        f"VALUES({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    conn.commit()
    inserted = conn.execute(
        "SELECT * FROM character_portraits WHERE id=?", (portrait_id,),
    ).fetchone()
    return await _complete_candidate(
        inserted,
        primary_qa=qa,
        purge_on_failure=True,
    )


def _has_column(conn, table: str, column: str) -> bool:
    """Support focused tests/old snapshots before app.db runs migrations."""
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall())


def _has_table(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None
