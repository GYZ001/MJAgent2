"""身份判定的结构化重试（resample）与候选证据的基础谓词：
规范化姓名权威 id、判定候选是否可具体化、外观片段抽取与跨集外观变化预审。
"""

from __future__ import annotations

import re

from collections.abc import Callable
from typing import Any

from app.evidence import repository as evidence_repository
from app import hiagent
from app.harness import model_gateway
from app.schemas import extract_json

from .constants import (
    APPEARANCE_MAX,
    FRAGMENT_BUDGET,
    FRAGMENT_WINDOW,
    IDENTITY_DISCOVERY_CONTRACT_VERSION,
)

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

