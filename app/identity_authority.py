"""Stable screenplay identity authority helpers.

Semantic identity decisions come from the character-discovery model.  This
module only gives those decisions durable IDs and validates their structural
shape; it never classifies a person from a name, title, age, costume, or role
word list.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Iterable


IDENTITY_AUTHORITY_VERSION = "screenplay-identity-authority.v1"
BACKEND_OWNED_IDENTITY_AUTHORITY_VERSION = (
    "screenplay-backend-owned-identity-authority.v1"
)
NON_AUTHORITATIVE_SOURCE_LABEL_PROVENANCES = frozenset({
    "provider_synthetic_functional.v1",
})


class IdentityAuthorityConflictError(ValueError):
    def __init__(self, issues: list[dict[str, Any]]):
        self.issues = list(issues)
        super().__init__(
            "；".join(
                str(issue.get("message") or issue.get("reason") or "")
                for issue in self.issues
            )
        )


def backend_owned_identity_authority(
    *,
    identity_key: str,
    display_name: str,
    role_type: str,
    source_names: Iterable[str] | None = None,
) -> dict[str, Any] | None:
    """Return authority that follows directly from the typed IR contract.

    This boundary is intentionally structural: it does not inspect names,
    titles, professions, appearance, source prose, or any vocabulary list.
    A pure narrator is an episode-local voice identity owned by the compiler,
    so a provider-supplied ID must not turn it into a semantic adjudication.
    """
    if str(role_type or "").strip() != "narrator":
        return None
    key = str(identity_key or "").strip()
    if not key:
        return None
    return {
        "authority_id": f"narrator:{key}",
        "canonical_name": str(display_name or "").strip() or key,
        "identity_kind": "narrator",
        "source_labels": [
            label
            for value in (source_names or [])
            if (label := str(value or "").strip())
        ],
        "authority_version": BACKEND_OWNED_IDENTITY_AUTHORITY_VERSION,
        "binding_operation": "bind_backend_owned_identity_authority",
        "binding_reason": "typed_role_contract_is_compiler_owned",
    }


def model_identity_authority_prompt_rule() -> str:
    """Keep provider, registry, adjudicator, and compiler authority in sync."""
    return (
        "authority_id 只允许逐字引用人物谱或身份预解析中已有 ID，模型不得自行生成；"
        "没有精确已登记 authority 的身份必须留空，交由后端根据 owned source evidence 条件式仲裁；"
        "role_type=narrator 的纯旁白也必须留空，由后端根据 identity.key 确定性生成。"
    )


def authority_id_for_resolution(value: dict[str, Any]) -> str:
    """Return a deterministic episode-local authority ID for one decision."""
    explicit = str(value.get("authority_id") or "").strip()
    if explicit:
        return explicit

    canonical_name = str(value.get("canonical_name") or "").strip()
    resolution = str(value.get("resolution") or "").strip()
    # ``future_identity`` and ``reference_identity`` are both named-family
    # decisions: a stable named entity that either materializes a card now
    # (future) or is only referenced offscreen this episode (reference).  Both
    # must resolve to the canonical named-authority namespace so the persisted
    # ``authority_id`` agrees with the ``named:`` identity_group that the
    # discovery projector assigns to the very same label.  Minting a
    # ``functional:`` authority for a named group is the self-contradictory row
    # that makes the structural-coverage gate see two authority classes in one
    # group.  Only functional extras (no stable canonical name) fall through to
    # the synthetic functional namespace below.
    if canonical_name and resolution in {"future_identity", "reference_identity"}:
        return f"bible:{canonical_name}"

    source_label = str(value.get("source_label") or "").strip()
    identity_group = str(value.get("identity_group") or "").strip()
    identity_scope_fingerprint = str(
        value.get("identity_scope_fingerprint") or ""
    ).strip()
    seed = {
        "canonical_name": canonical_name,
        "identity_group": identity_group or f"source:{source_label}",
        # current-1:F1 and similar model-local group tokens are only meaningful
        # inside one discovery input.  Never let the same token from a changed
        # source epoch silently reuse the old authority.
        "identity_scope_fingerprint": identity_scope_fingerprint,
    }
    digest = hashlib.sha256(
        json.dumps(
            seed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"functional:{digest}"


def _normalize_visual_entity_label(text: str) -> str:
    """纯字符串规整：只是同一字面量的等价形式折叠，不读取任何集/批上下文。

    折叠内容仅限于——(1) 首尾空白；(2) 内部连续空白压成单个空格（模型偶发的
    格式抖动，不是语义差异）；(3) Unicode NFKC 规整（全角/半角、兼容变体字符
    统一）。三者都只是同一个字符串在不同渲染下的等价写法，机械可复算，
    与"这是第几集/第几次调用"无关，因此对同一称谓在任意集、任意批调用都
    返回完全相同的结果——这正是下面 ``visual_entity_id_for_resolution`` 需要
    的稳定性前提。
    """
    collapsed = " ".join(text.split())
    return unicodedata.normalize("NFKC", collapsed)


def visual_entity_id_for_resolution(value: dict[str, Any]) -> str:
    """Return a cross-episode-stable visual entity ID for one decision.

    与 ``authority_id_for_resolution`` 并列、解耦：命名权威（上面那个函数）
    回答"这个人叫什么"，必须保守（不确定不绑，判错=事实错误）；视觉实体（本
    函数）回答"这张脸该配哪一张定妆照"，必须激进（同一人复用同一张脸，判错
    代价远低于"每集换脸"）。二者共享输入形状、但绝不共享构造逻辑——本函数
    不修改、不调用 ``authority_id_for_resolution``，也不改变它的既有行为。
    详见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §3、§4.2。

    - 已具名分支（``resolution`` in {future_identity, reference_identity} 且
      ``canonical_name`` 非空）：复用 ``f"bible:{canonical_name}"``——与
      ``authority_id_for_resolution`` 第 97 行同格式。共享前缀是刻意设计：
      一旦具名绑定成立，命名权威与视觉实体天然是同一个键，对现有已正确工作
      的具名绑定零迁移成本。

    - 功能分支（未具名/群演/绰号阶段）：
      ``sha256({"source_label": 归一化(source_label), "scope_qualifier":
      归一化(scope_qualifier)})`` 取前 16 位，``f"entity:{digest}"``。

      **关键约束，务必保持——种子里绝不能出现任何随集/随批变化的量：**

      * ``identity_scope_fingerprint``：``authority_id_for_resolution`` 第
        101-109 行自己的注释承认它"only meaningful inside one discovery
        input"，换一次模型调用（=换一集）指纹就变。这正是 functional
        authority 按构造跨集不稳定的根因（设计文档 §2.4），也是"同一个人
        每集换脸"这个故障现象的直接成因之一。视觉实体存在的唯一意义就是
        "跨集复用同一张脸"，一旦把这个指纹也掺进种子，稳定性诉求当场自我
        推翻——所以本函数刻意不读取这个字段。
      * ``identity_group``：同样是 "current-1:F1" 这类模型批内分组 token
        （discovery projector 生成，与 ``identity_scope_fingerprint`` 在同一
        条注释里被点名），只在一次判别调用内有意义，不是跨集语义键，同样
        不得入种子。
      * 任何证据侧标识（``episode_no``、``evidence_ref``、
        ``source_segment_id`` 等）：这些描述的是"这句话在哪一集哪一句被
        说出"，不是"这是谁"；纳入会让同一个角色因为不同集引用了不同证据
        而派生出不同的视觉实体 ID，重新制造设计文档 §0 描述的原始症状。

      种子只保留两个纯语义量：``source_label``（模型申报的称谓字符串）与
      ``scope_qualifier``（模型按现行 K 决议提示词规则 8 申报的限定语，
      专门用于区分"同一称谓指不同人"，见 ``app/portraits.py`` 规则 8 及
      ``_project_current_identity_response`` 里的 ``(source_label,
      scope_qualifier)`` 复合键逻辑）——这把尺子在批内已经被验证是消歧
      "同一称谓、不同人" 的正确复合键；本函数只是把它的适用范围从
      "批内去重" 扩展为 "跨集稳定键的输入"，不新增模型职责、不改变它的
      填写规则。两者都只描述"这是谁"，不描述"这次是怎么被观测到的"，因此
      同一角色跨集重复出现时种子恒定、digest 恒定。

      新增开发者如果想"顺手"把某个 identity_scope_fingerprint / episode_no
      / evidence_ref 之类的字段加进这个种子——请先重读这段注释：那正是
      functional authority 当年跨集不稳定的构造性根因，把同样的错误在这里
      重犯一次，视觉实体就会退化成 authority_id 的翻版，整个三层设计的意义
      随之消失。
    """
    canonical_name = str(value.get("canonical_name") or "").strip()
    resolution = str(value.get("resolution") or "").strip()
    if canonical_name and resolution in {"future_identity", "reference_identity"}:
        return f"bible:{canonical_name}"

    seed = {
        "source_label": _normalize_visual_entity_label(
            str(value.get("source_label") or "")
        ),
        "scope_qualifier": _normalize_visual_entity_label(
            str(value.get("scope_qualifier") or "")
        ),
    }
    digest = hashlib.sha256(
        json.dumps(
            seed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"entity:{digest}"


def normalize_character_resolution(
    value: dict[str, Any],
) -> dict[str, Any]:
    """Backfill authority metadata without changing the semantic decision."""
    normalized = dict(value)
    normalized["source_label"] = str(
        normalized.get("source_label") or ""
    ).strip()
    normalized["canonical_name"] = str(
        normalized.get("canonical_name") or ""
    ).strip()
    normalized["identity_group"] = str(
        normalized.get("identity_group") or ""
    ).strip()
    identity_scope_fingerprint = str(
        normalized.get("identity_scope_fingerprint") or ""
    ).strip()
    if identity_scope_fingerprint:
        normalized["identity_scope_fingerprint"] = identity_scope_fingerprint
    else:
        normalized.pop("identity_scope_fingerprint", None)
    source_instance_key = str(
        normalized.get("source_instance_key") or ""
    ).strip()
    if source_instance_key:
        normalized["source_instance_key"] = source_instance_key
    else:
        normalized.pop("source_instance_key", None)
    normalized["authority_id"] = authority_id_for_resolution(normalized)
    normalized.setdefault("authority_version", IDENTITY_AUTHORITY_VERSION)
    return normalized


def normalize_character_resolutions(
    values: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    return [
        normalize_character_resolution(value)
        for value in (values or [])
        if isinstance(value, dict)
        and str(value.get("source_label") or "").strip()
        and str(value.get("canonical_name") or "").strip()
    ]


def identity_resolution_is_authoritative(value: object) -> bool:
    """Synthetic provider labels are observations, never identity authority."""
    return bool(
        isinstance(value, dict)
        and str(value.get("source_label_provenance") or "").strip()
        not in NON_AUTHORITATIVE_SOURCE_LABEL_PROVENANCES
    )


def identity_authority_registry(
    bible: object,
    resolutions: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Project Bible and preflight decisions into one exact-reference registry."""
    entries: dict[str, dict[str, Any]] = {}
    groups_by_authority: dict[str, set[str]] = {}
    authorities_by_group: dict[str, set[str]] = {}
    authorities_by_named_canonical: dict[str, set[str]] = {}

    def scoped_group_key(
        identity_group: str,
        identity_scope_fingerprint: str = "",
    ) -> str:
        return (
            f"{identity_scope_fingerprint}:{identity_group}"
            if identity_scope_fingerprint
            else identity_group
        )

    def register_group(
        authority_id: str,
        identity_group: str,
        identity_scope_fingerprint: str = "",
        *,
        semantic_group: str | None = None,
    ) -> None:
        if not identity_group:
            return
        raw_scoped_group = scoped_group_key(
            identity_group, identity_scope_fingerprint
        )
        # Forward uniqueness always uses the raw model decision group.  It must
        # catch a future identity and a functional identity that both claim the
        # same scoped F1 but resolve to different authorities.
        authorities_by_group.setdefault(raw_scoped_group, set()).add(authority_id)
        # Reverse uniqueness uses the semantic canonical group.  Confirmed
        # aliases from multiple raw groups may all join one Bible identity.
        groups_by_authority.setdefault(authority_id, set()).add(
            semantic_group or raw_scoped_group
        )

    for character in getattr(bible, "characters", None) or []:
        name = str(getattr(character, "name", "") or "").strip()
        if not name:
            continue
        register_group(f"bible:{name}", f"bible:{name}")
        authorities_by_named_canonical.setdefault(name, set()).add(
            f"bible:{name}"
        )
        # 持久别名（Character.aliases，docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md
        # §4.1）已经过代码三闸核验 + 候选判别裁决 + 段号钉证才登记进人物谱，
        # 不再是模型的临时猜测——必须并入该角色的 source_labels，否则身份决议
        # 层永远看不到"许师姐"这类只以别名出现的角色，K 决议目录与
        # reserved_authority_labels 都不会收录它，别名在这条链路上就完全无效。
        # 防御：aliases 可能缺字段/为空串/与真名或彼此重复，一律去重后再收录。
        seen_labels: set[str] = {name}
        alias_labels: list[str] = []
        for alias in getattr(character, "aliases", None) or []:
            alias_text = str(getattr(alias, "text", "") or "").strip()
            if not alias_text or alias_text in seen_labels:
                continue
            seen_labels.add(alias_text)
            alias_labels.append(alias_text)
        entries[f"bible:{name}"] = {
            "authority_id": f"bible:{name}",
            "canonical_name": name,
            "identity_kind": "named",
            "source_labels": [name, *alias_labels],
            "identity_group": f"bible:{name}",
            "source_instance_key": f"bible:{name}",
            "evidence": "角色圣经已登记身份",
            "future_evidence": "",
        }

    for item in normalize_character_resolutions(resolutions):
        if not identity_resolution_is_authoritative(item):
            continue
        authority_id = item["authority_id"]
        identity_group = str(item.get("identity_group") or "").strip()
        identity_scope_fingerprint = str(
            item.get("identity_scope_fingerprint") or ""
        ).strip()
        raw_identity_group = identity_group
        semantic_group = None
        if (
            str(item.get("resolution") or "") == "future_identity"
        ):
            # A confirmed name is no longer an episode-local functional group.
            # This applies equally to Bible and backend-signed future-name /
            # candidate authorities: multiple former F1 tokens may be verified
            # aliases of one authority, while the forward raw-group check below
            # still forbids one F1 from resolving to two authorities.
            semantic_group = authority_id
            authorities_by_named_canonical.setdefault(
                item["canonical_name"], set()
            ).add(authority_id)
        register_group(
            authority_id,
            raw_identity_group,
            identity_scope_fingerprint,
            semantic_group=semantic_group,
        )
        entry = entries.setdefault(authority_id, {
            "authority_id": authority_id,
            "canonical_name": item["canonical_name"],
            "identity_kind": (
                "reference"
                if str(item.get("resolution") or "")
                == "reference_identity"
                else (
                    "named"
                    if str(item.get("resolution") or "")
                    == "future_identity"
                    else "functional"
                )
            ),
            "resolution": str(item.get("resolution") or ""),
            "source_labels": [],
            "identity_group": semantic_group or raw_identity_group,
            "decision_identity_group": raw_identity_group,
            "identity_scope_fingerprint": identity_scope_fingerprint,
            "source_instance_key": (
                item.get("source_instance_key")
                or scoped_group_key(
                    raw_identity_group,
                    identity_scope_fingerprint,
                )
                or authority_id
            ),
            "evidence": item.get("evidence") or "",
            "future_evidence": item.get("future_evidence") or "",
        })
        source_label = item["source_label"]
        if source_label and source_label not in entry["source_labels"]:
            entry["source_labels"].append(source_label)
        if entry["canonical_name"] != item["canonical_name"]:
            names = sorted({
                entry["canonical_name"],
                item["canonical_name"],
            })
            raise ValueError(
                f"authority_id={authority_id} 同时声明了多个 canonical_name："
                f"{names}"
            )
    issues = [
        {
            "reason": "identity_group_multiple_canonical_identities",
            "identity_group": identity_group,
            "authority_ids": sorted(authority_ids),
            "message": (
                f"identity_group={identity_group} 对应多个 canonical identity："
                f"{sorted(authority_ids)}"
            ),
        }
        for identity_group, authority_ids in authorities_by_group.items()
        if len(authority_ids) > 1
    ]
    issues.extend(
        {
            "reason": "canonical_identity_multiple_identity_groups",
            "authority_id": authority_id,
            "identity_groups": sorted(identity_groups),
            "message": (
                f"authority_id={authority_id} 跨多个 identity_group："
                f"{sorted(identity_groups)}"
            ),
        }
        for authority_id, identity_groups in groups_by_authority.items()
        if len(identity_groups) > 1
    )
    issues.extend(
        {
            "reason": "canonical_name_multiple_named_authorities",
            "canonical_name": canonical_name,
            "authority_ids": sorted(authority_ids),
            "message": (
                f"canonical_name={canonical_name} 对应多个 named authority："
                f"{sorted(authority_ids)}"
            ),
        }
        for canonical_name, authority_ids in (
            authorities_by_named_canonical.items()
        )
        if len(authority_ids) > 1
    )
    if issues:
        raise IdentityAuthorityConflictError(issues)
    return list(entries.values())
