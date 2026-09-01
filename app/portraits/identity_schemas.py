"""身份判定发给 provider 的响应 schema：current/future/structural 三类
pydantic 模型与对应的 JSON Schema / response_format 构造函数。
"""

from __future__ import annotations


from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._identity_tokens import _identity_source_label_has_list_separator
from .constants import (
    IDENTITY_NAME_FORMS,
    IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH,
)
from .evidence_merge import _current_identity_decision_cap

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
                # "" 对每个 key 都无条件合法：真正的业务约束是"只有该组
                # decisions 选 F: 时才允许 ''，选 N: 时必须是本组证据目录
                # 里的一个真实 evidence_id"，但这是一条跨 decisions/
                # reveal_evidence_ids 两个字段的条件约束，strict provider
                # schema 只保留 $defs/$ref/additionalProperties/enum/items/
                # properties/required/type 这几个关键字（见
                # _IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS），
                # oneOf/if-then-else 会被 _identity_strict_provider_schema
                # 的白名单静默剥离（实测验证，见
                # test_future_identity_reveal_evidence_id_conditional_
                # constraint_is_not_representable_in_strict_schema）——不是
                # 忘了写，是这层扁平 schema 结构性表达不了。真实事故
                # ERR-20260831-45404d（EP1 run_c14d8e02d220）：模型选了
                # N:G002 却把 reveal_evidence_ids 留空，schema 认为合法，
                # 业务校验才拒绝，两头各占一半理。这里不放宽业务校验（那会
                # 允许模型用空 evidence_id 伪造"揭示"），改在
                # _future_identity_prompt 的规则 4 里正面写清"选 N 必须给
                # 真实 evidence_id，空串只在选 F 时合法"，把模型能看到的
                # 提示词与它能看到的这份 schema 对齐，不再自相矛盾。
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

