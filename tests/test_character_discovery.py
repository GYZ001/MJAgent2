import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from app import api, db, hiagent, portraits, screenplay_scene_shards, stages
from app import errors as app_errors
from app.harness import model_gateway
from app.narrative_blueprint import NarrativeBlueprint
from app.schemas import (Bible, Character, EpisodeScreenplay,
                         IdentityContractEvidence, InformationItem,
                         KeyDialogueChain, KeyDialogueTurn,
                         NarrativeContinuityPlan, NarrativeIdentityContract,
                         PlotSpine, PlotSpineBeat, ScriptScene,
                         VoiceCanonical, World)


def _current_identity_wire(
    characters: list[dict],
    *,
    provider_schema: dict,
    messages: list[dict] | None = None,
) -> dict:
    evidence_refs = list(
        provider_schema["$defs"]["CurrentNewNamedIdentityDecision"]
        ["properties"]["evidence_ref"]["enum"]
    )
    evidence_by_ref: dict[str, dict] = {}
    known_by_label: dict[tuple[str, str], list[dict]] = {}
    prior_functional: list[dict] = []
    if messages:
        prompt = str(messages[0].get("content") or "")
        prior_marker = "前批已确定的 functional 分组 P 决议"
        prior_end = "\n\n本批 backend-owned 当前身份证据目录"
        if prior_marker in prompt and prior_end in prompt:
            raw_prior = prompt.split(prior_marker, 1)[1]
            raw_prior = raw_prior.split("\n", 1)[1]
            raw_prior = raw_prior.split(prior_end, 1)[0]
            prior_functional = json.loads(raw_prior)
        marker = (
            "backend-owned 当前身份证据目录。E ref 已绑定完整证据 receipt，"
            "禁止跨 E 搬运人物："
        )
        end_marker = "\n\n本批已登记身份 K 决议目录"
        if marker in prompt and end_marker in prompt:
            raw_catalog = prompt.split(marker, 1)[1]
            raw_catalog = raw_catalog.split("\n", 1)[1]
            raw_catalog = raw_catalog.split(end_marker, 1)[0]
            evidence_by_ref = {
                str(item["evidence_ref"]): dict(item)
                for item in json.loads(raw_catalog)
            }
        known_marker = "目录为空则所有 k=[]）："
        known_end = "\n\n本集已有功能身份决议"
        if known_marker in prompt and known_end in prompt:
            raw_known = prompt.split(known_marker, 1)[1]
            raw_known = raw_known.split("\n", 1)[1]
            raw_known = raw_known.split(known_end, 1)[0]
            for item in json.loads(raw_known):
                known_by_label.setdefault((
                    str(item.get("source_label") or ""),
                    str(item.get("canonical_name") or ""),
                ), []).append(dict(item))

    def evidence_ref_for(item: dict, source_label: str) -> str:
        explicit = str(
            item.get("evidence_ref") or item.get("evidence_id") or ""
        )
        if explicit in evidence_refs:
            return explicit
        evidence_hint = str(item.get("evidence") or "")
        return next(
            (
                evidence_ref for evidence_ref in evidence_refs
                if (
                    source_label in str(
                        evidence_by_ref.get(evidence_ref, {}).get("text") or ""
                    )
                    or (
                        evidence_hint
                        and evidence_hint in str(
                            evidence_by_ref.get(evidence_ref, {}).get("text")
                            or ""
                        )
                    )
                )
            ),
            evidence_refs[0],
        )

    decisions = {"k": [], "n": [], "f": []}
    for index, raw in enumerate(characters, start=1):
        item = dict(raw)
        source_label = str(item.get("source_label") or item.get("name") or "")
        kind = "mentioned" if item.get("kind") == "mentioned" else "onscreen"
        evidence_ref = evidence_ref_for(item, source_label)
        if str(item.get("identity_kind") or "named") == "functional":
            reuse_prior = bool(item.get("reuse_prior"))
            prior_label = str(
                item.get("prior_source_label") or source_label
            )
            prior = next((
                candidate for candidate in prior_functional
                if prior_label in (candidate.get("source_labels") or [])
            ), None)
            decisions["f"].append({
                "evidence_ref": evidence_ref,
                "source_label": source_label,
                "functional_identity_key": str(
                    prior["decision_id"]
                    if reuse_prior and prior is not None
                    else item.get("functional_identity_key") or f"F{index}"
                ),
                "kind": kind,
            })
        else:
            canonical_name = str(
                item.get("canonical_name") or item.get("name") or ""
            )
            known = next((
                candidate
                for candidate in known_by_label.get(
                    (source_label, canonical_name), []
                )
                if candidate.get("evidence_ref") == evidence_ref
            ), None)
            if known is not None:
                decisions["k"].append({
                    "decision_id": known["decision_id"],
                    "kind": kind,
                })
            else:
                decisions["n"].append({
                    "evidence_ref": evidence_ref,
                    "identity_label": source_label,
                    # Fixtures declare a real name unless they say otherwise;
                    # 真名 > 尊称 > 代称 is exercised by its own tests.
                    "name_kind": str(
                        item.get("name_kind") or "personal_name"
                    ),
                    "kind": kind,
                })
    return decisions


def _future_identity_wire(
    characters: list[dict],
    *,
    provider_schema: dict,
    evidence_text_by_id: dict[str, str] | None = None,
) -> dict:
    decision_properties = provider_schema["properties"]["decisions"][
        "properties"
    ]
    evidence_properties = provider_schema["properties"][
        "reveal_evidence_ids"
    ]["properties"]
    group_keys = list(decision_properties)
    grouped: dict[str, list[dict]] = {}
    for index, raw in enumerate(characters, start=1):
        item = dict(raw)
        group_marker = str(
            item.get("functional_identity_key")
            or item.get("identity_group")
            or item.get("canonical_name")
            or item.get("source_label")
            or f"fixture-{index}"
        )
        grouped.setdefault(group_marker, []).append(item)

    decisions: dict[str, str] = {}
    revealed_names: dict[str, str] = {}
    reveal_evidence_ids: dict[str, str] = {}
    revealed_name_kinds: dict[str, str] = {}
    desired_groups = list(grouped.values())
    for index, group_key in enumerate(group_keys):
        options = decision_properties[group_key]["enum"]
        desired = desired_groups[index][0] if index < len(desired_groups) else {}
        canonical_name = str(desired.get("canonical_name") or "")
        identity_kind = str(desired.get("identity_kind") or "functional")
        known_option = next(
            (
                option for option in options
                if canonical_name
                and f":bible:{canonical_name}:" in str(option)
            ),
            "",
        )
        if identity_kind == "named" and known_option:
            decisions[group_key] = known_option
            revealed_names[group_key] = ""
            reveal_evidence_ids[group_key] = ""
            revealed_name_kinds[group_key] = ""
        elif identity_kind == "named":
            decisions[group_key] = next(
                option for option in options if str(option).startswith("N:")
            )
            revealed_names[group_key] = canonical_name
            evidence_options = [
                value for value in evidence_properties[group_key]["enum"]
                if value
            ]
            reveal_evidence_ids[group_key] = next(
                (
                    value for value in evidence_options
                    if canonical_name in str(
                        (evidence_text_by_id or {}).get(value) or ""
                    )
                ),
                evidence_options[0],
            )
            # Fixtures propose a real name unless a test says otherwise;
            # 真名 > 尊称 > 代称 is exercised by its own tests.
            revealed_name_kinds[group_key] = str(
                desired.get("name_kind") or "personal_name"
            )
        else:
            decisions[group_key] = next(
                option for option in options if str(option).startswith("F:")
            )
            revealed_names[group_key] = ""
            reveal_evidence_ids[group_key] = ""
            revealed_name_kinds[group_key] = ""
    return {
        "decisions": decisions,
        "revealed_names": revealed_names,
        "reveal_evidence_ids": reveal_evidence_ids,
        "revealed_name_kinds": revealed_name_kinds,
    }


def _coverage_identity_wire(
    characters: list[dict],
    *,
    provider_schema: dict,
    messages: list[dict] | None = None,
) -> dict:
    decision_properties = provider_schema["properties"]["decisions"][
        "properties"
    ]
    decision_catalog: dict[str, dict] = {}
    group_labels: dict[str, str] = {}
    if messages:
        prompt = str(messages[0].get("content") or "")
        group_marker = "未决引用目录"
        evidence_marker = "\nowned SRC 证据目录"
        decision_marker = "可选决议目录"
        rules_marker = "\n规则："
        if group_marker in prompt and evidence_marker in prompt:
            raw_groups = prompt.split(group_marker, 1)[1]
            raw_groups = raw_groups.split("\n", 1)[1]
            raw_groups = raw_groups.split(evidence_marker, 1)[0]
            group_labels = {
                str(item["group_key"]): str(item["source_label"])
                for item in json.loads(raw_groups)
            }
        if decision_marker in prompt and rules_marker in prompt:
            raw_catalog = prompt.split(decision_marker, 1)[1]
            raw_catalog = raw_catalog.split("\n", 1)[1]
            raw_catalog = raw_catalog.split(rules_marker, 1)[0]
            decision_catalog = {
                str(item["decision_id"]): item
                for item in json.loads(raw_catalog)
            }
    desired_by_label = {
        str(item.get("source_label") or item.get("name") or ""): item
        for item in characters
    }
    decisions: dict[str, str] = {}
    for group_key, decision_schema in decision_properties.items():
        options = list(decision_schema["enum"])
        desired = desired_by_label.get(group_labels.get(group_key, ""), {})
        desired_kind = str(desired.get("identity_kind") or "functional")
        desired_name = str(
            desired.get("canonical_name") or desired.get("name") or ""
        )
        desired_group = str(desired.get("identity_group_ref") or "")
        selected = next(
            (
                option for option in options
                if (
                    desired_kind == "named"
                    and decision_catalog.get(option, {}).get("identity_kind")
                    == "named"
                    and (
                        not desired_name
                        or decision_catalog.get(option, {}).get(
                            "canonical_name"
                        ) == desired_name
                    )
                    and (
                        not desired_group
                        or decision_catalog.get(option, {}).get(
                            "identity_group_ref"
                        ) == desired_group
                    )
                )
            ),
            "",
        )
        if not selected:
            selected = next(
                option for option in options
                if str(option).startswith("F:")
                and (
                    not desired_group
                    or decision_catalog.get(option, {}).get(
                        "identity_group_ref"
                    ) == desired_group
                )
            )
        decisions[group_key] = selected
    return {"decisions": decisions}


def _identity_wire_for_call(
    kwargs: dict,
    characters: list[dict],
    *,
    messages: list[dict] | None = None,
) -> dict:
    phase = str(kwargs.get("call_meta", {}).get("discovery_phase") or "")
    if phase == "current":
        return _current_identity_wire(
            characters,
            provider_schema=kwargs["response_format"]["json_schema"]["schema"],
            messages=messages,
        )
    if phase == "future_identity":
        provider_schema = kwargs["response_format"]["json_schema"]["schema"]
        evidence_text_by_id: dict[str, str] = {}
        if messages:
            prompt = str(messages[0].get("content") or "")
            marker = "后续证据目录"
            decision_marker = "\n可选决议目录"
            if marker in prompt and decision_marker in prompt:
                raw_catalog = prompt.split(marker, 1)[1]
                raw_catalog = raw_catalog.split("\n", 1)[1]
                raw_catalog = raw_catalog.split(decision_marker, 1)[0]
                catalog = json.loads(raw_catalog)
                evidence_text_by_id = {
                    str(item["evidence_id"]): str(item["text"])
                    for item in catalog
                }
        return _future_identity_wire(
            characters,
            provider_schema=provider_schema,
            evidence_text_by_id=evidence_text_by_id,
        )
    if phase == "coverage":
        return _coverage_identity_wire(
            characters,
            provider_schema=kwargs["response_format"]["json_schema"][
                "schema"
            ],
            messages=messages,
        )
    raise AssertionError(f"unexpected identity phase: {phase}")


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects(id TEXT PRIMARY KEY, bible_json TEXT, bible_version INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE chapters(project_id TEXT, idx INTEGER, content TEXT)")
    conn.execute("CREATE TABLE episodes(project_id TEXT, episode_no INTEGER, source_chapters TEXT)")
    conn.execute(
        "CREATE TABLE character_portraits(id TEXT, project_id TEXT, character_name TEXT, ep_start INTEGER, "
        "ep_end INTEGER, appearance TEXT, prompt TEXT, image_path TEXT, base_portrait_id TEXT, "
        "bible_version INTEGER, created_at REAL)")
    return conn


def test_structural_identity_audit_accepts_typed_blueprint_key(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    async def fake_structured(messages, **kwargs):
        seen["prompt"] = messages[0]["content"]
        seen["operation_id"] = kwargs["operation_id"]
        seen["kwargs"] = kwargs
        return portraits.StructuralIdentityCoverageResponse.model_validate(
            _coverage_identity_wire([{
                "source_label": "北区杂役处未知闯入者",
                "identity_kind": "functional",
            }], provider_schema=kwargs["response_format"]["json_schema"][
                "schema"
            ], messages=messages)
        )

    monkeypatch.setattr(
        portraits.model_gateway,
        "chat_structured",
        fake_structured,
    )
    source = "就在这时，房门被人一脚踹开，一声冷哼随之传入房间。"
    audited = asyncio.run(
        portraits.audit_identity_coverage_from_structural_evidence(
            [],
            structural_evidence=[{
                "identity_key": "北区杂役处未知闯入者",
                "source_label": "北区杂役处未知闯入者",
                "source_segment_ids": ["SRC0001"],
                "usage": "voice",
                "node_key": "node-1",
            }],
            source_text=source,
            bible=Bible(
                characters=[],
                world=World(visual_style_canonical="测试"),
            ),
            episode_no=2,
        )
    )

    assert audited[0]["source_label"] == "北区杂役处未知闯入者"
    assert audited[0]["identity_kind"] == "functional"
    assert seen["operation_id"].startswith("screenplay.identity.coverage.v6:")
    assert "每个不透明 decision_id 已绑定" in str(seen["prompt"])
    kwargs = seen["kwargs"]
    assert kwargs["model_type"] is portraits.StructuralIdentityCoverageResponse
    assert kwargs["format_retry_limit"] == 0
    assert kwargs["semantic_retry_limit"] == 0
    assert kwargs["require_response_format"] is True
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["response_format"]["json_schema"]["strict"] is True
    assert kwargs["call_meta"]["contract_version"] == (
        portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
    )


def test_structural_identity_coverage_schema_is_closed_and_provider_safe(
) -> None:
    group_keys = ["I001", "I002"]
    local_schema = portraits._structural_identity_coverage_schema(
        group_keys,
        decision_ids_by_group={
            "I001": ["F:I001:one", "K:I001:known"],
            "I002": ["F:I002:two"],
        },
    )
    local_before = json.loads(json.dumps(local_schema, ensure_ascii=False))
    response_format = (
        portraits._structural_identity_coverage_response_format(
            local_schema
        )
    )

    assert local_schema == local_before
    assert local_schema["required"] == ["decisions"]
    decisions = local_schema["properties"]["decisions"]
    assert decisions["additionalProperties"] is False
    assert decisions["required"] == group_keys
    assert decisions["properties"]["I001"]["enum"] == [
        "F:I001:one", "K:I001:known",
    ]

    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    provider_schema = response_format["json_schema"]["schema"]
    assert provider_schema == local_schema
    assert response_format["json_schema"]["name"] == (
        "screenplay_structural_identity_coverage_v6"
    )
    provider_keywords: set[str] = set()

    def collect(schema_node: dict) -> None:
        provider_keywords.update(schema_node)
        for mapping_keyword in ("$defs", "properties"):
            for child in schema_node.get(mapping_keyword, {}).values():
                collect(child)
        items = schema_node.get("items")
        if isinstance(items, dict):
            collect(items)

    collect(provider_schema)
    assert provider_keywords <= (
        portraits._IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS
    )
    assert provider_keywords.isdisjoint({
        "title",
        "default",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "uniqueItems",
    })


def test_future_identity_exact_map_schema_is_closed_and_provider_safe(
) -> None:
    local_schema = portraits._future_identity_schema(
        ["G001", "G002"],
        decision_ids_by_group={
            "G001": ["F:G001", "K:G001:bible:许清:receipt", "N:G001"],
            "G002": ["F:G002", "N:G002"],
        },
        evidence_ids_by_group={
            "G001": ["E:first"],
            "G002": ["E:second"],
        },
    )
    local_before = json.loads(json.dumps(local_schema, ensure_ascii=False))
    response_format = portraits._identity_strict_response_format(
        local_schema,
        name="screenplay_future_identity_resolution_v10",
    )

    assert local_schema == local_before
    assert set(local_schema["required"]) == {
        "decisions", "revealed_names", "reveal_evidence_ids",
        "revealed_name_kinds",
    }
    for field_name in local_schema["required"]:
        field_schema = local_schema["properties"][field_name]
        assert field_schema["additionalProperties"] is False
        assert field_schema["required"] == ["G001", "G002"]
        assert list(field_schema["properties"]) == ["G001", "G002"]
    assert local_schema["properties"]["decisions"]["properties"][
        "G001"
    ]["enum"] == ["F:G001", "K:G001:bible:许清:receipt", "N:G001"]
    assert local_schema["properties"]["reveal_evidence_ids"][
        "properties"
    ]["G002"]["enum"] == ["", "E:second"]
    assert local_schema["properties"]["revealed_names"]["properties"][
        "G001"
    ]["maxLength"] == portraits.IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH

    provider_schema = response_format["json_schema"]["schema"]
    assert response_format["json_schema"]["strict"] is True
    assert "maxLength" not in provider_schema["properties"][
        "revealed_names"
    ]["properties"]["G001"]
    provider_keywords: set[str] = set()

    def collect(schema_node: dict) -> None:
        provider_keywords.update(schema_node)
        for mapping_keyword in ("$defs", "properties"):
            for child in schema_node.get(mapping_keyword, {}).values():
                collect(child)
        items = schema_node.get("items")
        if isinstance(items, dict):
            collect(items)

    collect(provider_schema)
    assert provider_keywords <= (
        portraits._IDENTITY_COVERAGE_STRICT_PROVIDER_SCHEMA_KEYWORDS
    )
    assert provider_keywords.isdisjoint({
        "anyOf", "oneOf", "if", "then", "else", "allOf", "const",
        "title", "default", "minLength", "maxLength", "minItems",
        "maxItems", "uniqueItems",
    })


# --- source_label 约束维度回归：从长度换成分隔符标点 ------------------------
#
# 生产事故 provider_calls.id=8233（第 7 集）：模型对 f 项如实回答
# source_label='一只约莫一人大小，样子如猴般的凶兽'（17 字），被旧的
# max_length=16 硬拒绝，触发 StructuredFormatError 并杀掉整集。真正应该拒绝
# 的原因是其中的全角逗号——下游按 `_IDENTITY_LIST_SEPARATOR_PATTERN` 切分身份
# 列表，混入分隔符会让一个人被错误切成两段。长度只是不精确的代理：更短但带
# 逗号的标签一样危险，更长但不带分隔符的标签反而无害。见
# `IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH` 旁的完整说明。


def test_current_functional_source_label_ep7_regression_rejects_separator_not_length() -> None:
    """EP7 真实夹具：provider_calls.id=8233 response_json 里的原样 source_label。"""
    ep7_label = "一只约莫一人大小，样子如猴般的凶兽"
    assert len(ep7_label) == 17
    # 17 字远小于新的防御性上限 64；如果修复后仍被拒绝，必须是因为分隔符。
    assert len(ep7_label) <= portraits.IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH

    with pytest.raises(ValidationError, match="分隔符"):
        portraits.CurrentFunctionalIdentityDecision(
            evidence_ref="E013",
            source_label=ep7_label,
            functional_identity_key="F1",
            kind="onscreen",
        )

    # 证明约束维度真的换了：去掉那个全角逗号后，字符串仍然比旧的 16 字上限长，
    # 但不再含分隔符，必须放行。
    without_separator = ep7_label.replace("，", "")
    assert len(without_separator) == 16
    accepted = portraits.CurrentFunctionalIdentityDecision(
        evidence_ref="E013",
        source_label=without_separator,
        functional_identity_key="F1",
        kind="onscreen",
    )
    assert accepted.source_label == without_separator


def test_current_functional_source_label_accepts_shortest_literal_span() -> None:
    """第七章原文里「凶兽」本身就是一个更短、合法的逐字称谓。"""
    accepted = portraits.CurrentFunctionalIdentityDecision(
        evidence_ref="E013",
        source_label="凶兽",
        functional_identity_key="F1",
        kind="onscreen",
    )
    assert accepted.source_label == "凶兽"


def test_current_functional_source_label_accepts_long_label_without_separators() -> None:
    """约束维度是分隔符标点，不是长度：远超旧 16 字上限但不含分隔符必须放行。"""
    long_label = "一只约莫一人大小样子如猴般的凶兽并无任何逗号顿号或空白字符插入其中三十字整"
    assert len(long_label) > 16
    assert len(long_label) <= portraits.IDENTITY_SOURCE_LABEL_DEFENSIVE_MAX_LENGTH
    accepted = portraits.CurrentFunctionalIdentityDecision(
        evidence_ref="E013",
        source_label=long_label,
        functional_identity_key="F1",
        kind="onscreen",
    )
    assert accepted.source_label == long_label


@pytest.mark.parametrize("separator", [
    "、", "，", ",", "／", "/", "；", ";", "｜", "|", "＆", "&", "＋", "+", " ", "\t",
])
def test_current_functional_source_label_rejects_every_list_separator(
    separator: str,
) -> None:
    with pytest.raises(ValidationError, match="分隔符"):
        portraits.CurrentFunctionalIdentityDecision(
            evidence_ref="E013",
            source_label=f"甲{separator}乙",
            functional_identity_key="F1",
            kind="onscreen",
        )


def test_current_functional_source_label_defensive_cap_still_applies() -> None:
    """长度上限没有消失，只是从业务约束降级为防御性上限（64，对齐
    functional_identity_key 的 max_length=64）。"""
    with pytest.raises(ValidationError):
        portraits.CurrentFunctionalIdentityDecision(
            evidence_ref="E013",
            source_label="甲" * 65,
            functional_identity_key="F1",
            kind="onscreen",
        )
    accepted = portraits.CurrentFunctionalIdentityDecision(
        evidence_ref="E013",
        source_label="甲" * 64,
        functional_identity_key="F1",
        kind="onscreen",
    )
    assert len(accepted.source_label) == 64


def test_current_identity_projection_accepts_long_separator_free_functional_label() -> None:
    """append_candidate 的手写业务校验必须和 Pydantic Field 一起放宽。

    此前 `_project_current_identity_response` 内部的 `append_candidate` 单独
    用 `len(source_label) > 16` 复查了一遍同一条规则；只放宽 Pydantic Field
    而漏改这里，会让一个已经通过 schema 校验的合法长标签在业务校验阶段再次
    被拒。
    """
    long_label = "一只约莫一人大小样子如猴般的凶兽并无任何逗号顿号或空白字符插入其中三十字整"
    source_text = f"{long_label}忽然从林中窜出，众人大惊。"
    records = portraits._current_identity_evidence_records(source_text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    evidence_ref = next(
        ref for ref, record in evidence_by_ref.items()
        if long_label in str(record.get("text") or "")
    )
    payload = {
        "k": [],
        "n": [],
        "f": [{
            "evidence_ref": evidence_ref,
            "source_label": long_label,
            "functional_identity_key": "F1",
            "kind": "onscreen",
        }],
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    assert len(projected) == 1
    assert projected[0]["source_label"] == long_label


# ---------------------------------------------------------------------------
# k/n/f 计数帽参数重推导（第22轮总审计 ERR-20260824-aeee2d）：帽子最早是
# "每个 evidence ref 自带一份 k/n/f，各自封顶 64"形状下的产物，分母是一个
# ≤900 字的段落；响应压平成"整批只输出一次全局 k/n/f"（RF11）后分母变成了
# 一整批 evidence ref，但常量原样照抄，从未随分母重新推导。真实回归
# provider_calls.id=8964（第 22 轮 EP3）：84 个 evidence ref 的对话密集批次，
# 78 条全部合法、全部命中 known_decisions 目录的 k 决议被旧的固定 64 硬拒。
# 见 _current_identity_decision_cap 的完整推导：
#   cap = max(64, evidence_ref 数量 * 3)
# ---------------------------------------------------------------------------

def test_current_identity_k_cap_scales_with_evidence_batch_size_ep3_regression() -> None:
    """红灯→绿灯（真实第22轮 EP3 回归 ERR-20260824-aeee2d，provider_calls.
    id=8964 的真实规模）：84 个 evidence ref 的批次里，78 条全部逐字锚定、
    全部命中 known_decisions 目录的合法 k 决议，不能被计数帽当成失控拒绝
    ——旧的固定 64（真实响应 k=78）会硬拒，重参数化后 cap=max(64,84*3)=252
    必须放行。"""
    evidence_ref_count = 84
    known_k_count = 78
    evidence_by_ref: dict[str, dict] = {}
    known_decisions: dict[str, dict] = {}
    k_items: list[dict] = []
    for index in range(1, known_k_count + 1):
        evidence_ref = f"E{index:03d}"
        label = f"角色{index}"
        evidence_by_ref[evidence_ref] = {"text": f"{label}忽然开口说话。"}
        decision_id = f"K{index:03d}"
        known_decisions[decision_id] = {
            "evidence_ref": evidence_ref,
            "source_label": label,
            "canonical_name": label,
            "authority_id": f"bible:{label}",
            "decision_type": "registered_authority",
        }
        k_items.append({"decision_id": decision_id, "kind": "mentioned"})
    # 补齐没有出人物的多余 evidence ref，凑够真实批次的 84 个 ref，跟
    # provider_calls.id=8964 的批规模一致。
    for index in range(known_k_count + 1, evidence_ref_count + 1):
        evidence_by_ref[f"E{index:03d}"] = {"text": "（无人物的过场句）。"}
    response = portraits.CurrentIdentityCandidateResponse.model_validate({
        "k": k_items, "n": [], "f": [],
    })
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known_decisions,
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert not any("过多" in message for message in errors)
    assert errors == []
    assert len(projected) == known_k_count


def _current_identity_f_items_over_shared_refs(
    *, evidence_ref_count: int, f_count: int,
) -> tuple[dict[str, dict], list[dict]]:
    """构造 f_count 条各自 source_label 唯一、循环复用 evidence_ref_count 个
    evidence ref 的合法 f 决议——每个 ref 的文本一次性拼进它将被引用到的全部
    label，避免任何一次复用互相覆盖导致某个 label 的逐字锚定失真。"""
    labels_by_ref: dict[str, list[str]] = {
        f"E{index:03d}": [] for index in range(1, evidence_ref_count + 1)
    }
    f_items: list[dict] = []
    for index in range(1, f_count + 1):
        evidence_ref = f"E{(index % evidence_ref_count) + 1:03d}"
        label = f"路人{index}"
        labels_by_ref[evidence_ref].append(label)
        f_items.append({
            "evidence_ref": evidence_ref,
            "source_label": label,
            "functional_identity_key": f"F{index}",
            "kind": "mentioned",
        })
    evidence_by_ref = {
        ref: {"text": "、".join(labels) + "先后匆匆走过。" if labels else "占位过场句。"}
        for ref, labels in labels_by_ref.items()
    }
    return evidence_by_ref, f_items


def test_current_identity_decision_cap_floor_preserves_small_batch_headroom() -> None:
    """帽子公式的下限分量（floor=64）必须继续生效：一个只有 10 个 evidence
    ref 的小批次，如果单纯按"数量 * 3"算会封顶在 30，但 50 条合法、互不
    冲突的 f 决议在改动前就能通过（旧固定帽是 64）——下限存在就是为了不让
    任何现有小批次的既有行为变严。"""
    evidence_by_ref, f_items = _current_identity_f_items_over_shared_refs(
        evidence_ref_count=10, f_count=50,
    )
    response = portraits.CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [], "f": f_items,
    })
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    assert len(projected) == len(f_items)


def test_current_identity_decision_cap_still_rejects_genuine_overflow_in_small_batch() -> None:
    """计数帽仍然是一道真实的失控防线，不是被参数重推导拿掉了：一个只有 5
    个 evidence ref 的小批次里出现 70 条 f 决议，远超这批次能有的正常密度
    （cap=max(64,5*3)=64），必须继续硬拒。"""
    evidence_by_ref, f_items = _current_identity_f_items_over_shared_refs(
        evidence_ref_count=5, f_count=70,
    )
    response = portraits.CurrentIdentityCandidateResponse.model_validate({
        "k": [], "n": [], "f": f_items,
    })
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any(
        message == "current identity f decisions 过多" for message in errors
    )


def test_current_identity_known_decision_catalog_exposes_long_separator_free_alias() -> None:
    """K 目录过滤器（历史注册别名）必须使用同一条业务规则，而不是独立的旧
    len()>16 检查，否则修复后新签发的长标签永远无法在后续集数被重新认领。
    """
    long_alias = "一只约莫一人大小样子如猴般的凶兽并无任何逗号顿号或空白字符插入其中三十字整"
    source_text = f"{long_alias}忽然现身。"
    records = portraits._current_identity_evidence_records(source_text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    authorities = [{
        "authority_id": "bible:凶兽头领",
        "canonical_name": "凶兽头领",
        "identity_kind": "named",
        "identity_group": "bible:凶兽头领",
        "source_labels": [long_alias],
        "source_instance_key": "bible:凶兽头领",
    }]
    known = portraits._current_identity_known_decision_catalog(
        evidence_by_ref, authorities=authorities,
    )
    assert any(item["source_label"] == long_alias for item in known.values())


def test_current_identity_known_decision_catalog_still_rejects_separator_alias() -> None:
    """同一过滤器仍必须拒绝带分隔符的历史别名（哪怕它很短）。"""
    bad_alias = "凶兽，头领"
    source_text = f"{bad_alias}忽然现身。"
    records = portraits._current_identity_evidence_records(source_text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    authorities = [{
        "authority_id": "bible:凶兽头领",
        "canonical_name": "凶兽头领",
        "identity_kind": "named",
        "identity_group": "bible:凶兽头领",
        "source_labels": [bad_alias],
        "source_instance_key": "bible:凶兽头领",
    }]
    known = portraits._current_identity_known_decision_catalog(
        evidence_by_ref, authorities=authorities,
    )
    assert not any(item["source_label"] == bad_alias for item in known.values())


def test_future_identity_new_name_rejects_list_separator_not_length(
    monkeypatch,
) -> None:
    """revealed_names（未来揭示真名）与 source_label 同源同险：canonical_name
    一旦混入分隔符，会被下游按同一 pattern 错误切成两段身份。这里验证真正的
    业务约束（validate_response 里的检查）已经从长度换成分隔符。
    """
    bible = Bible(world=World(visual_style_canonical="都市漫画"), characters=[])
    calls = 0
    # 8 字，远小于旧的 16 字上限：证明拒绝理由是分隔符，不是长度。
    bad_name = "陈某、王某"

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        schema = kwargs["response_format"]["json_schema"]["schema"]
        group_key = next(iter(
            schema["properties"]["decisions"]["properties"]
        ))
        decisions = schema["properties"]["decisions"]["properties"]
        evidence = schema["properties"]["reveal_evidence_ids"]["properties"]
        return json.dumps({
            "decisions": {
                group_key: next(
                    value for value in decisions[group_key]["enum"]
                    if value.startswith("N:")
                ),
            },
            "revealed_names": {group_key: bad_name},
            "revealed_name_kinds": {group_key: "personal_name"},
            "reveal_evidence_ids": {
                group_key: next(
                    value for value in evidence[group_key]["enum"]
                    if value
                ),
            },
        }, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="真名不得包含身份列表分隔符",
    ):
        asyncio.run(portraits.resolve_future_identity_candidates(
            [{
                "name": "三哥",
                "source_label": "三哥",
                "identity_kind": "functional",
                "identity_group": "current:third-brother",
                "kind": "onscreen",
            }],
            source_text="三哥推门进来。",
            future_text=f"后来才知道，三哥其实是{bad_name}两人假扮的。",
            bible=bible,
            episode_no=7,
            future_label="后续章节",
        ))
    assert calls == 1


def _coverage_audit_kwargs() -> dict:
    return {
        "structural_evidence": [{
            "identity_key": "未知求救者",
            "source_label": "未知求救者",
            "source_segment_ids": ["SRC0001"],
            "usage": "visible",
            "node_key": "node-1",
        }],
        "source_text": "裂缝中有未知求救者探出半个身子。",
        "bible": Bible(
            characters=[],
            world=World(visual_style_canonical="测试"),
        ),
        "episode_no": 1,
    }


def test_structural_identity_coverage_strict_success_is_one_call(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    async def fake_chat(_messages, **kwargs):
        calls.append(kwargs)
        return json.dumps(_coverage_identity_wire(
            [{
                "source_label": "未知求救者",
                "identity_kind": "functional",
            }],
            provider_schema=kwargs["response_format"]["json_schema"][
                "schema"
            ],
            messages=_messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(
        portraits.audit_identity_coverage_from_structural_evidence(
            [],
            **_coverage_audit_kwargs(),
        )
    )

    assert len(calls) == 1
    assert result[0]["source_label"] == "未知求救者"
    assert result[0]["name"] == "未知求救者"
    call = calls[0]
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["call_meta"]["response_format_required"] is True
    assert call["call_meta"]["format_attempt"] == 0
    assert call["call_meta"]["semantic_attempt"] == 0


def test_structural_coverage_does_not_treat_current_synthetic_as_authority(
    monkeypatch,
) -> None:
    synthetic_group = "current-1:synthetic:owned-receipt"
    synthetic = {
        "name": "未知求救者",
        "source_label": "未知求救者",
        "identity_kind": "functional",
        "identity_group": synthetic_group,
        "kind": "onscreen",
        "source_label_provenance": (
            portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        ),
    }
    calls = 0

    async def fake_structured(messages, **kwargs):
        nonlocal calls
        calls += 1
        prompt = str(messages[0]["content"])
        decision_marker = "可选决议目录"
        raw_catalog = prompt.split(decision_marker, 1)[1].split("\n", 1)[1]
        raw_catalog = raw_catalog.split("\n规则：", 1)[0]
        decision_catalog = json.loads(raw_catalog)
        assert all(
            item["identity_group_ref"] != synthetic_group
            for item in decision_catalog
        )
        return portraits.StructuralIdentityCoverageResponse.model_validate(
            _coverage_identity_wire(
                [{
                    "source_label": "未知求救者",
                    "identity_kind": "functional",
                }],
                provider_schema=kwargs["response_format"]["json_schema"][
                    "schema"
                ],
                messages=messages,
            )
        )

    monkeypatch.setattr(
        portraits.model_gateway,
        "chat_structured",
        fake_structured,
    )
    result = asyncio.run(
        portraits.audit_identity_coverage_from_structural_evidence(
            [synthetic],
            **_coverage_audit_kwargs(),
        )
    )

    assert calls == 1
    authoritative = [
        item for item in result
        if item.get("source_label_provenance")
        != portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
    ]
    assert len(authoritative) == 1
    assert authoritative[0]["source_label"] == "未知求救者"
    assert authoritative[0]["identity_group"].startswith("structural:")


def test_structural_coverage_named_reference_keeps_single_group_authority(
    monkeypatch,
) -> None:
    """A mentioned named reference must not add a second authority class.

    Regression for the EP1 blueprint-stage failure ``structural identity group
    缺少唯一权威``: a ``reference_identity`` for a named label ("许师姐") lands in a
    ``named:`` identity_group.  When its authority_id was minted as
    ``functional:...`` the very same group also derived ``bible:许师姐`` from the
    named branch, so the coverage gate saw two authority classes in one group
    and hard-failed.  The authority for a named-family reference must resolve to
    the ``bible:`` namespace so the group carries exactly one authority.
    """
    source_text = "许师姐出门带回了四个拥有资质的小娃。"
    scope = portraits.screenplay_identity_scope_fingerprint(1, source_text)
    named_group = "current-1:named:" + portraits.evidence_repository.content_hash(
        "许师姐"
    )[:16]
    reference = {
        "source_label": "许师姐",
        "canonical_name": "许师姐",
        "resolution": "reference_identity",
        "identity_group": named_group,
        "identity_scope_fingerprint": scope,
        "decision_provenance": (
            portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
        ),
        "decision_contract_version": portraits.FUTURE_IDENTITY_DECISION_VERSION,
        "structural_identity_policy_version": (
            portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
    }
    # The named-family reference must own the ``bible:`` authority that agrees
    # with its ``named:`` group; a ``functional:`` authority here is the
    # self-contradictory row the coverage gate rejects.
    normalized = portraits.normalize_character_resolution(reference)
    assert normalized["authority_id"] == "bible:许师姐"

    calls = 0

    async def fake_structured(messages, **kwargs):
        nonlocal calls
        calls += 1
        return portraits.StructuralIdentityCoverageResponse.model_validate(
            _coverage_identity_wire(
                [{
                    "source_label": "许师姐",
                    "canonical_name": "许师姐",
                    "identity_kind": "named",
                    "identity_group_ref": named_group,
                }],
                provider_schema=kwargs["response_format"]["json_schema"][
                    "schema"
                ],
                messages=messages,
            )
        )

    monkeypatch.setattr(
        portraits.model_gateway,
        "chat_structured",
        fake_structured,
    )
    # Must not raise "structural identity group 缺少唯一权威".
    result = asyncio.run(
        portraits.audit_identity_coverage_from_structural_evidence(
            [],
            structural_evidence=[{
                "identity_key": "许师姐",
                "source_label": "许师姐",
                "source_segment_ids": ["SRC0001"],
                "usage": "mentioned",
                "node_key": "node-1",
            }],
            source_text=source_text,
            bible=Bible(
                characters=[],
                world=World(visual_style_canonical="国风"),
            ),
            episode_no=1,
            existing_resolutions=[normalized],
        )
    )
    assert calls == 1
    named = next(item for item in result if item["source_label"] == "许师姐")
    assert named["authority_id"] == "bible:许师姐"


def test_structural_coverage_bound_group_uses_selected_owned_src(
    monkeypatch,
) -> None:
    source_text = "第一段只有风声。\n\n守卫在第二段高声喝止。"
    scope = portraits.screenplay_identity_scope_fingerprint(1, source_text)
    existing_resolution = {
        "source_label": "守卫",
        "canonical_name": "丁力",
        "resolution": "future_identity",
        "identity_group": "current-1:guard",
        "authority_id": "bible:丁力",
        "identity_scope_fingerprint": scope,
        "decision_provenance": (
            portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
        ),
        "decision_contract_version": portraits.FUTURE_IDENTITY_DECISION_VERSION,
        "structural_identity_policy_version": (
            portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
    }
    calls = 0

    async def fake_structured(messages, **kwargs):
        nonlocal calls
        calls += 1
        wire = _coverage_identity_wire(
            [{
                "source_label": "守卫",
                "canonical_name": "丁力",
                "identity_kind": "named",
                "identity_group_ref": "current-1:guard",
            }],
            provider_schema=kwargs["response_format"]["json_schema"][
                "schema"
            ],
            messages=messages,
        )
        return portraits.StructuralIdentityCoverageResponse.model_validate(
            wire
        )

    monkeypatch.setattr(
        portraits.model_gateway,
        "chat_structured",
        fake_structured,
    )
    result = asyncio.run(
        portraits.audit_identity_coverage_from_structural_evidence(
            [],
            structural_evidence=[{
                "identity_key": "守卫",
                "source_label": "守卫",
                "source_segment_ids": ["SRC0001", "SRC0002"],
                "usage": "visible",
                "node_key": "node-1",
            }],
            source_text=source_text,
            bible=Bible(
                characters=[Character(
                    name="丁力",
                    role="山门守卫",
                    appearance_canonical="黑发男子，深灰皮甲，腰佩长刀",
                )],
                world=World(visual_style_canonical="国风"),
            ),
            episode_no=1,
            existing_resolutions=[existing_resolution],
        )
    )

    assert calls == 1
    assert len(result) == 1
    candidate = result[0]
    assert candidate["name"] == "丁力"
    assert candidate["source_segment_ids"] == ["SRC0001", "SRC0002"]
    assert candidate["source_segment_id"] == "SRC0002"
    assert candidate["source_quote"] == "守卫在第二段高声喝止。"
    assert candidate["evidence"] == candidate["source_quote"]


@pytest.mark.parametrize(
    ("provider_result", "error_type"),
    [
        # Corrupt bytes (no JSON object ever decodes) are deliberately absent:
        # nothing was authored to preserve, so that case is resampled once and
        # is covered by test_undelivered_identity_answer_is_resampled_once.
        (
            '{"characters":[{"source_label":"未知求救者"}]}',
            model_gateway.StructuredFormatError,
        ),
        (
            '{"decisions":{"I001":"F:I001:forged"}}',
            model_gateway.StructuredSemanticError,
        ),
        (
            '{"decisions":{}}',
            model_gateway.StructuredSemanticError,
        ),
        (
            '{"decisions":{"I001":"F:I001:forged","I002":"extra"}}',
            model_gateway.StructuredSemanticError,
        ),
        (
            json.dumps({
                "named": [],
                "functional": [{
                    "source_label": "未知求救者",
                    "canonical_name": "   ",
                    "identity_group_ref": "new:bogus",
                    "evidence": "未知求救者",
                }],
            }, ensure_ascii=False),
            model_gateway.StructuredFormatError,
        ),
    ],
)
def test_structural_identity_coverage_http_200_contract_failure_is_one_call(
    monkeypatch,
    provider_result: str,
    error_type: type[Exception],
) -> None:
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["call_meta"]["response_format_required"] is True
        return provider_result

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(error_type):
        asyncio.run(
            portraits.audit_identity_coverage_from_structural_evidence(
                [],
                **_coverage_audit_kwargs(),
            )
        )

    assert calls == 1


def test_strict_identity_invalid_raw_is_not_reused_across_authorized_runs(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["reuse_successful_operation"] is False
        return '{"decisions":{}}'

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    for _retry_epoch in ("run-one", "run-two"):
        with pytest.raises(model_gateway.StructuredSemanticError):
            asyncio.run(
                portraits.audit_identity_coverage_from_structural_evidence(
                    [],
                    **_coverage_audit_kwargs(),
                )
            )

    assert calls == 2


def test_structural_identity_coverage_subset_is_one_call_hard_failure(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["response_format"]["type"] == "json_schema"
        decision_properties = kwargs["response_format"]["json_schema"][
            "schema"
        ]["properties"]["decisions"]["properties"]
        first_key = next(iter(decision_properties))
        first_option = decision_properties[first_key]["enum"][0]
        return json.dumps({
            "decisions": {first_key: first_option},
        }, ensure_ascii=False)

    kwargs = _coverage_audit_kwargs()
    kwargs["structural_evidence"].append({
        "identity_key": "被困同伴",
        "source_label": "被困同伴",
        "source_segment_ids": ["SRC0001"],
        "usage": "visible",
        "node_key": "node-1",
    })
    kwargs["source_text"] = "裂缝中有未知求救者与被困同伴探出半个身子。"
    monkeypatch.setattr(model_gateway, "chat", fake_chat)

    with pytest.raises(model_gateway.StructuredSemanticError):
        asyncio.run(
            portraits.audit_identity_coverage_from_structural_evidence(
                [],
                **kwargs,
            )
        )

    assert calls == 1


def test_structural_identity_coverage_unsupported_schema_is_one_call(
    monkeypatch,
) -> None:
    calls = 0
    original = hiagent.ProviderError(
        "strict response_format unsupported",
        retryable=False,
        failure_kind="response_format_unsupported",
    )

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["response_format_required"] is True
        raise original

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(
            portraits.audit_identity_coverage_from_structural_evidence(
                [],
                **_coverage_audit_kwargs(),
            )
        )

    assert caught.value is original
    assert calls == 1


def test_generic_discovery_rejects_legacy_coverage_cache(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE artifacts("
        "id TEXT PRIMARY KEY, scope_type TEXT, scope_id TEXT, type TEXT, "
        "status TEXT, content_json TEXT, created_at REAL)"
    )
    source_text = "裂缝中有未知求救者探出半个身子。"
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="测试"),
    )
    structural_evidence = _coverage_audit_kwargs()[
        "structural_evidence"
    ]
    legacy_input_hash = portraits.evidence_repository.content_hash({
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "mode": "targeted",
        "episode_no": 1,
        "source_text": source_text,
        "draft_text": "",
        "future_text": "",
        "future_label": "",
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": [],
        "structural_evidence": structural_evidence,
    })
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
        (
            "legacy-v3-generic",
            "episode",
            "ep-attempt10-cache",
            "screenplay_identity_discovery",
            "validated",
            json.dumps({
                "contract_version": (
                    portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION
                ),
                "input_hash": legacy_input_hash,
                "mode": "targeted",
                "candidates": [{"source_label": "stale-v3"}],
            }),
            1.0,
        ),
    )
    conn.commit()
    phases: list[str] = []
    artifacts: list[dict] = []

    async def fake_current(*_args, **_kwargs):
        phases.append("current")
        return []

    async def fake_future(candidates, **_kwargs):
        phases.append("future")
        return candidates

    async def fake_coverage(candidates, **_kwargs):
        phases.append("coverage")
        return [*candidates, {"source_label": "fresh-v4"}]

    def record_artifact(artifact, **_kwargs):
        artifacts.append(dict(artifact.content))
        return {"id": f"artifact-{len(artifacts)}"}

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "true")
    monkeypatch.setattr(
        portraits,
        "extract_current_identity_candidates",
        fake_current,
    )
    monkeypatch.setattr(
        portraits,
        "resolve_future_identity_candidates",
        fake_future,
    )
    monkeypatch.setattr(
        portraits,
        "audit_identity_coverage_from_structural_evidence",
        fake_coverage,
    )
    monkeypatch.setattr(
        portraits.evidence_repository,
        "create_artifact",
        record_artifact,
    )

    result = asyncio.run(portraits.discover_character_candidates(
        source_text,
        bible,
        1,
        structural_evidence=structural_evidence,
        scope_id="ep-attempt10-cache",
    ))

    assert phases == ["current", "future", "coverage"]
    assert result == [{"source_label": "fresh-v4"}]
    assert len(artifacts) == 2
    assert all(
        artifact["structural_coverage_policy_version"]
        == portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
        and artifact["structural_coverage_applied"] is True
        for artifact in artifacts
    )


def test_generic_discovery_keeps_current_contract_cache_compatible(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE artifacts("
        "id TEXT PRIMARY KEY, scope_type TEXT, scope_id TEXT, type TEXT, "
        "status TEXT, content_json TEXT, content_hash TEXT, created_at REAL)"
    )
    source_text = "萌浩独自前行。"
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="测试"),
    )
    current_contract_hash = portraits.evidence_repository.content_hash({
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": (
            portraits._current_identity_evidence_catalog_hash(source_text)
        ),
        "mode": "targeted",
        "episode_no": 1,
        "source_text": source_text,
        "draft_text": "",
        "future_text": "",
        "future_label": "",
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": [],
        "structural_evidence": [],
    })
    receipt = portraits._current_identity_evidence_records(source_text)[0]
    expected = [{
        "name": "萌浩",
        "source_label": "萌浩",
        "identity_kind": "functional",
        "identity_group": "current-1:F1",
        "authority_id": "",
        "kind": "onscreen",
        "source_label_provenance": (
            portraits.CURRENT_IDENTITY_LITERAL_PROVENANCE
        ),
        "source_evidence_receipt": receipt,
        "source_evidence_receipts": [receipt],
        "source_segment_id": receipt["source_segment_id"],
        "source_segment_ids": [receipt["source_segment_id"]],
        "source_quote": receipt["text"],
    }]
    cached_content = {
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": (
            portraits._current_identity_evidence_catalog_hash(source_text)
        ),
        "input_hash": current_contract_hash,
        "mode": "targeted",
        "candidates": expected,
    }
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
        (
            "ordinary-current",
            "episode",
            "ep-attempt10-ordinary-cache",
            "screenplay_identity_discovery",
            "validated",
            json.dumps(cached_content),
            portraits.evidence_repository.content_hash(cached_content),
            1.0,
        ),
    )
    conn.commit()

    async def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("current contract cache should remain compatible")

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "true")
    monkeypatch.setattr(
        portraits,
        "extract_current_identity_candidates",
        forbidden_provider,
    )

    result = asyncio.run(portraits.discover_character_candidates(
        source_text,
        bible,
        1,
        scope_id="ep-attempt10-ordinary-cache",
    ))

    assert result == expected


def test_generic_discovery_rejects_tampered_validated_artifact_content(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE artifacts("
        "id TEXT PRIMARY KEY, scope_type TEXT, scope_id TEXT, type TEXT, "
        "status TEXT, content_json TEXT, content_hash TEXT, created_at REAL)"
    )
    source_text = "守卫独自值守山门。"
    bible = Bible(characters=[], world=World(visual_style_canonical="测试"))
    discovery_input = {
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": (
            portraits._current_identity_evidence_catalog_hash(source_text)
        ),
        "mode": "targeted",
        "episode_no": 1,
        "source_text": source_text,
        "draft_text": "",
        "future_text": "",
        "future_label": "",
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": [],
        "structural_evidence": [],
    }
    original = {
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": discovery_input[
            "current_evidence_catalog_hash"
        ],
        "input_hash": portraits.evidence_repository.content_hash(
            discovery_input
        ),
        "mode": "targeted",
        "candidates": [],
    }
    tampered = {**original, "candidates": [{"source_label": "伪造身份"}]}
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
        (
            "tampered", "episode", "ep-seal", "screenplay_identity_discovery",
            "validated", json.dumps(tampered, ensure_ascii=False),
            portraits.evidence_repository.content_hash(original), 1.0,
        ),
    )
    calls: list[str] = []

    async def fresh_current(*_args, **_kwargs):
        calls.append("current")
        return []

    async def passthrough(candidates, **_kwargs):
        return candidates

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "true")
    monkeypatch.setattr(
        portraits, "extract_current_identity_candidates", fresh_current,
    )
    monkeypatch.setattr(
        portraits, "resolve_future_identity_candidates", passthrough,
    )
    monkeypatch.setattr(
        portraits,
        "audit_identity_coverage_from_structural_evidence",
        passthrough,
    )
    monkeypatch.setattr(
        portraits.evidence_repository,
        "create_artifact",
        lambda artifact, **_kwargs: {"id": artifact.type},
    )

    result = asyncio.run(portraits.discover_character_candidates(
        source_text, bible, 1, scope_id="ep-seal",
    ))

    assert result == []
    assert calls == ["current"]


def test_generic_discovery_reuses_sealed_structural_applied_artifact(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE artifacts("
        "id TEXT PRIMARY KEY, scope_type TEXT, scope_id TEXT, type TEXT, "
        "status TEXT, content_json TEXT, content_hash TEXT, created_at REAL)"
    )
    source_text = "被困者从裂缝中探出半个身子。"
    bible = Bible(characters=[], world=World(visual_style_canonical="测试"))
    structural_evidence = [{
        "identity_key": "被困者",
        "source_segment_ids": ["SRC0001"],
        "usage": "visible",
        "node_key": "S001-N001",
    }]
    discovery_input = {
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": (
            portraits._current_identity_evidence_catalog_hash(source_text)
        ),
        "mode": "targeted",
        "episode_no": 1,
        "source_text": source_text,
        "draft_text": "",
        "future_text": "",
        "future_label": "",
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": [],
        "structural_evidence": structural_evidence,
        "structural_coverage_policy_version": (
            portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
        "structural_coverage_applied": True,
    }
    expected = [{
        "name": "被困者",
        "source_label": "被困者",
        "identity_kind": "functional",
        "identity_group": "structural:trapped",
        "kind": "onscreen",
        "source_segment_id": "SRC0001",
        "source_segment_ids": ["SRC0001"],
        "source_quote": source_text,
    }]
    cached = {
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": discovery_input[
            "current_evidence_catalog_hash"
        ],
        "structural_coverage_policy_version": (
            portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
        ),
        "structural_coverage_applied": True,
        "input_hash": portraits.evidence_repository.content_hash(
            discovery_input
        ),
        "mode": "targeted",
        "candidates": expected,
    }
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
        (
            "sealed", "episode", "ep-sealed-structural",
            "screenplay_identity_discovery", "validated",
            json.dumps(cached, ensure_ascii=False),
            portraits.evidence_repository.content_hash(cached), 1.0,
        ),
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("sealed structural-applied cache should be reused")

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "true")
    monkeypatch.setattr(
        portraits, "extract_current_identity_candidates", forbidden,
    )

    result = asyncio.run(portraits.discover_character_candidates(
        source_text,
        bible,
        1,
        structural_evidence=structural_evidence,
        scope_id="ep-sealed-structural",
    ))

    assert result == expected


def test_generic_discovery_rejects_attempt15_previous_contract_cache(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE artifacts("
        "id TEXT PRIMARY KEY, scope_type TEXT, scope_id TEXT, type TEXT, "
        "status TEXT, content_json TEXT, created_at REAL)"
    )
    source_text = "白袍老人摘下面具，自称苍玄。"
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="测试"),
    )
    previous_contract = "screenplay-identity-discovery.v12"
    previous_hash = portraits.evidence_repository.content_hash({
        "contract_version": previous_contract,
        "mode": "targeted",
        "episode_no": 1,
        "source_text": source_text,
        "draft_text": "",
        "future_text": "",
        "future_label": "",
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": [],
        "structural_evidence": [],
    })
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
        (
            "pre-authority-contract",
            "episode",
            "ep-pre-authority-contract",
            "screenplay_identity_discovery",
            "validated",
            json.dumps({
                "contract_version": previous_contract,
                "current_identity_version": "screenplay-current-identity.v9",
                "input_hash": previous_hash,
                "mode": "targeted",
                "candidates": [{
                    "source_label": "白袍老人",
                    "authority_id": "future-name:legacy-cangxuan",
                }],
            }),
            1.0,
        ),
    )
    conn.commit()
    phases: list[str] = []

    async def fresh_current(*_args, **_kwargs):
        phases.append("current")
        return [{"source_label": "fresh-current"}]

    async def fresh_future(candidates, **_kwargs):
        phases.append("future")
        return candidates

    async def fresh_coverage(candidates, **_kwargs):
        phases.append("coverage")
        return candidates

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "true")
    monkeypatch.setattr(
        portraits, "extract_current_identity_candidates", fresh_current,
    )
    monkeypatch.setattr(
        portraits, "resolve_future_identity_candidates", fresh_future,
    )
    monkeypatch.setattr(
        portraits,
        "audit_identity_coverage_from_structural_evidence",
        fresh_coverage,
    )
    monkeypatch.setattr(
        portraits.evidence_repository,
        "create_artifact",
        lambda artifact, **_kwargs: {"id": artifact.type},
    )

    result = asyncio.run(portraits.discover_character_candidates(
        source_text,
        bible,
        1,
        scope_id="ep-pre-authority-contract",
    ))

    assert phases == ["current", "future", "coverage"]
    assert result == [{"source_label": "fresh-current"}]


def test_structural_coverage_parse_failure_stops_before_scene_writing(
    monkeypatch,
) -> None:
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "n1",
            "source_segment_ids": ["SRC0001"],
            "summary": "未知求救者从裂缝探身",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "日",
            "time_relation": "episode_start",
            "location_key": "mountain",
            "location_label": "半山腰",
            "participants": ["未知求救者"],
            "participant_evidence": [{
                "identity_key": "未知求救者",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": ["SRC0001:unit:001"],
                "usage": "visible",
            }],
            "action_logic": "未知求救者从裂缝探身",
            "scene_boundary_before": True,
        }],
    })
    source_text = "未知求救者从裂缝探出半个身子。"
    provider_calls: list[str] = []
    downstream_calls: list[str] = []

    async def malformed_provider(_messages, **kwargs):
        provider_calls.append(str(kwargs["call_meta"]["stage_key"]))
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["call_meta"]["response_format_required"] is True
        return '{"characters":[{"source_label" "未知求救者"}]}'

    async def coverage_without_persistence(*args, **_kwargs):
        return await portraits.audit_identity_coverage_from_structural_evidence(
            [],
            structural_evidence=args[5],
            source_text=args[3],
            bible=args[4],
            episode_no=args[2],
        )

    async def forbidden_scene_writing(*_args, **_kwargs):
        downstream_calls.append("scene-writing-ledger")
        raise AssertionError("coverage failure reached scene writing")

    monkeypatch.setattr(model_gateway, "chat", malformed_provider)
    monkeypatch.setattr(
        portraits,
        "ensure_structural_identity_coverage",
        coverage_without_persistence,
    )
    monkeypatch.setattr(
        screenplay_scene_shards,
        "generate_screenplay_scene_shards",
        forbidden_scene_writing,
    )
    monkeypatch.setattr(
        "app.production.revision.get_active_production_revision",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(
            run_id="run-attempt10-fail-fast",
            step_run_id="step-attempt10-fail-fast",
        ),
    )

    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(stages._generate_screenplay_scene_sharded_baseline(
            {
                "id": "ep-attempt10-fail-fast",
                "project_id": "project-attempt10-fail-fast",
                "episode_no": 1,
                "character_resolutions": [{
                    "source_label": "未知求救者",
                    "canonical_name": "未知求救者",
                    "resolution": "functional_identity",
                    "identity_group": "legacy-v3:F1",
                    "decision_provenance": (
                        portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
                    ),
                    "decision_contract_version": (
                        portraits.FUTURE_IDENTITY_DECISION_VERSION
                    ),
                    # Attempt10/v3 persisted rows had no structural policy tag.
                }],
            },
            source_text,
            Bible(
                characters=[],
                world=World(visual_style_canonical="测试"),
            ),
            narrative_blueprint=blueprint,
        ))

    # Corrupt bytes carry no authored identity judgement, so the contract
    # allows exactly one clean resample; the point of this test is that a
    # coverage failure still never reaches scene writing.
    assert provider_calls == ["screenplay_character_discovery"] * 2
    assert downstream_calls == []


def test_current_synthetic_resolution_cannot_suppress_blueprint_coverage(
    monkeypatch,
) -> None:
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "n1",
            "source_segment_ids": ["SRC0001"],
            "summary": "面色苍白的女子从裂缝探身",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "日",
            "time_relation": "episode_start",
            "location_key": "mountain",
            "location_label": "半山腰",
            "participants": ["面色苍白的女子"],
            "participant_evidence": [{
                "identity_key": "面色苍白的女子",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": ["SRC0001:unit:001"],
                "usage": "visible",
            }],
            "action_logic": "面色苍白的女子从裂缝探身",
            "scene_boundary_before": True,
        }],
    })
    source_text = "一个面色苍白，看不出年纪的女子从裂缝探身。"
    scope = portraits.screenplay_identity_scope_fingerprint(1, source_text)
    coverage_inputs: list[list[dict]] = []
    downstream: list[str] = []

    class CoverageReached(RuntimeError):
        pass

    async def assert_coverage(
        _project_id,
        _episode_id,
        _episode_no,
        _source_text,
        _bible,
        structural_evidence,
        **_kwargs,
    ):
        coverage_inputs.append(list(structural_evidence))
        raise CoverageReached("synthetic participant reached coverage")

    async def forbidden_scene(*_args, **_kwargs):
        downstream.append("scene-writing")
        raise AssertionError("synthetic participant skipped coverage")

    monkeypatch.setattr(
        portraits,
        "ensure_structural_identity_coverage",
        assert_coverage,
    )
    monkeypatch.setattr(
        screenplay_scene_shards,
        "generate_screenplay_scene_shards",
        forbidden_scene,
    )
    monkeypatch.setattr(
        "app.production.revision.get_active_production_revision",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.observability.tracing.current_trace",
        lambda: SimpleNamespace(
            run_id="run-attempt14-synthetic",
            step_run_id="step-attempt14-synthetic",
        ),
    )

    with pytest.raises(CoverageReached):
        asyncio.run(stages._generate_screenplay_scene_sharded_baseline(
            {
                "id": "ep-attempt14-synthetic",
                "project_id": "project-attempt14-synthetic",
                "episode_no": 1,
                "character_resolutions": [{
                    "source_label": "面色苍白的女子",
                    "canonical_name": "面色苍白的女子",
                    "resolution": "functional_identity",
                    "identity_group": "current-1:synthetic:owned-receipt",
                    "identity_scope_fingerprint": scope,
                    "decision_provenance": (
                        portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
                    ),
                    "decision_contract_version": (
                        portraits.FUTURE_IDENTITY_DECISION_VERSION
                    ),
                    "structural_identity_policy_version": (
                        portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
                    ),
                    "source_label_provenance": (
                        portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
                    ),
                }],
            },
            source_text,
            Bible(
                characters=[],
                world=World(visual_style_canonical="测试"),
            ),
            narrative_blueprint=blueprint,
        ))

    assert [
        item["identity_key"] for item in coverage_inputs[0]
    ] == ["面色苍白的女子"]
    assert downstream == []


def test_reference_identity_cannot_hide_visible_blueprint_participant(
    monkeypatch,
) -> None:
    blueprint = NarrativeBlueprint.model_validate({
        "episode_no": 1,
        "nodes": [{
            "key": "n1",
            "source_segment_ids": ["SRC0001"],
            "summary": "师尊走入大殿",
            "narrative_layer": "story",
            "event_priority": "causal",
            "render_policy": "standalone",
            "temporal_domain_key": "present",
            "time_label": "日",
            "time_relation": "episode_start",
            "location_key": "hall",
            "location_label": "大殿",
            "participants": ["师尊"],
            "participant_evidence": [{
                "identity_key": "师尊",
                "source_segment_ids": ["SRC0001"],
                "source_unit_keys": ["SRC0001:unit:001"],
                "usage": "visible",
            }],
            "action_logic": "师尊走入大殿",
            "scene_boundary_before": True,
        }],
    })
    downstream: list[str] = []

    async def forbidden_coverage(*_args, **_kwargs):
        downstream.append("coverage")
        raise AssertionError("non-materializable reference reached coverage")

    async def forbidden_scene(*_args, **_kwargs):
        downstream.append("scene")
        raise AssertionError("non-materializable reference reached scene")

    monkeypatch.setattr(
        portraits,
        "ensure_structural_identity_coverage",
        forbidden_coverage,
    )
    monkeypatch.setattr(
        screenplay_scene_shards,
        "generate_screenplay_scene_shards",
        forbidden_scene,
    )

    with pytest.raises(
        portraits.ContentGenerationError,
        match="只有不可物化的引用身份",
    ):
        asyncio.run(stages._generate_screenplay_scene_sharded_baseline(
            {
                "id": "ep-reference-visible",
                "project_id": "project-reference-visible",
                "episode_no": 1,
                "character_resolutions": [{
                    "source_label": "师尊",
                    "canonical_name": "苍玄",
                    "resolution": "reference_identity",
                    "identity_group": "manual:master",
                    "authority_id": "manual:cangxuan",
                    "decision_provenance": "manual",
                }],
            },
            "师尊走入大殿。",
            Bible(
                characters=[],
                world=World(visual_style_canonical="测试"),
            ),
            narrative_blueprint=blueprint,
        ))

    assert downstream == []


def _seed_project(conn: sqlite3.Connection, chapter_content: str) -> None:
    bible = Bible(world=World(visual_style_canonical="国风"),
                  characters=[Character(name="萧炎", role="主角",
                                        appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩")])
    conn.execute("INSERT INTO projects(id, bible_json, bible_version) VALUES('p1', ?, 1)",
                 (json.dumps(bible.model_dump(), ensure_ascii=False),))
    conn.execute("INSERT INTO episodes(project_id, episode_no, source_chapters) VALUES('p1', 21, '[30]')")
    conn.execute("INSERT INTO chapters(project_id, idx, content) VALUES('p1', 30, ?)", (chapter_content,))
    conn.commit()


def _patch_settings(monkeypatch, conn) -> dict:
    settings: dict[str, str] = {}
    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda k: settings.get(k))
    monkeypatch.setattr(portraits, "set_setting", lambda k, v: settings.__setitem__(k, v))
    return settings


def test_ensure_character_card_auto_adds_prominent_character_and_portrait(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎现身，紫色长发，妖娆冷艳。美杜莎再次出手。美杜莎统领蛇人一族。" * 3)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(name, fragments, *, style, known_names, ep_label):
        assert name == "美杜莎" and "美杜莎" in fragments  # 检索到的是该角色片段
        return {"subject_kind": "person", "important": True, "reason": "反复出场", "role": "重要配角",
                "appearance_canonical": "紫发妖娆女子，紫色长发，金瞳蛇眸，蛇纹长裙，气场冷艳标志性蛇瞳",
                "personality": "高傲", "speech_style": "冷冽",
                "relationships": [{"to": "萧炎", "relation": "宿敌"}]}

    async def fake_portrait(project_id, name, style, appearance, *, ep_start):
        return (f"/tmp/{name}.jpg", "fake prompt")

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", fake_portrait)

    res = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 21))
    assert res["status"] == "added"
    assert res["has_portrait"] is True

    names = [c["name"] for c in json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]]
    assert "美杜莎" in names
    assert conn.execute("SELECT bible_version FROM projects WHERE id='p1'").fetchone()["bible_version"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"] == 1
    queue = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert queue[0]["status"] == "auto_applied"
    assert queue[0]["payload"]["character_card"]["name"] == "美杜莎"

    # 幂等：第二次识别到同名角色时不重复建卡/出图。
    res2 = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 22))
    assert res2["status"] == "exists"
    cnt = conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"]
    assert cnt == 1


def test_required_identity_card_accepts_complete_card_despite_importance_vote(
    monkeypatch,
) -> None:
    conn = _make_conn()
    _seed_project(conn, "丁力听令后带人巡查山门。")
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*_args, **kwargs):
        assert kwargs["require_identity_card"] is True
        return {
            "subject_kind": "person",
            "important": False,
            "reason": "只出现一次",
            "role": "重要配角",
            "appearance_canonical": (
                "成年黑发男子，身穿深灰色皮甲短衫，腰间佩刀，"
                "体格壮实，左眉留有一道浅疤"
            ),
            "personality": "服从命令",
            "speech_style": "简短应答",
            "relationships": [],
        }

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)

    result = asyncio.run(portraits.ensure_character_card(
        "p1",
        "丁力",
        21,
        generate_portrait=False,
        require_identity_card=True,
    ))

    assert result["status"] == "added"
    characters = json.loads(
        conn.execute(
            "SELECT bible_json FROM projects WHERE id='p1'",
        ).fetchone()["bible_json"],
    )["characters"]
    assert any(character["name"] == "丁力" for character in characters)


def test_required_identity_card_prompt_does_not_reapply_importance_gate(
    monkeypatch,
) -> None:
    captured: list[str] = []
    call_options: list[dict] = []

    async def fake_chat(messages, **_kwargs):
        captured.append(messages[0]["content"])
        call_options.append(_kwargs)
        return json.dumps({
            "subject_kind": "person",
            "important": False,
            "reason": "只出现一次",
            "role": "重要配角",
            "appearance_canonical": (
                "成年黑发男子，身穿深灰色皮甲短衫，腰间佩刀，"
                "体格壮实，左眉留有一道浅疤"
            ),
            "personality": "服从命令",
            "speech_style": "简短应答",
            "relationships": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)

    result = asyncio.run(portraits.assess_new_character(
        "丁力",
        "丁力听令后带人巡查山门。",
        style="国风",
        known_names=["萧炎"],
        ep_label="第 21 集",
        require_identity_card=True,
    ))

    assert result["important"] is False
    assert result["card_complete"] is True
    assert "本次任务不是重新判断戏份重要度" in captured[0]
    assert "不得因只出现一次而拒绝建卡" in captured[0]
    assert call_options[0]["max_tokens"] >= 4096
    assert call_options[0]["call_meta"]["expected_json"] is True


def test_required_identity_card_retries_once_when_first_card_too_thin(
    monkeypatch,
) -> None:
    """已确认真名但首轮人物卡过薄时，应有界重试补全，而不是首轮不完整就报错。"""
    prompts: list[str] = []
    responses = [
        # 首轮：外观过薄，缺可视维度 → 触发补全重试。
        json.dumps({
            "subject_kind": "person",
            "important": True,
            "reason": "已确认真名",
            "role": "重要配角",
            "appearance_canonical": "一个男子",
            "personality": "",
            "speech_style": "",
            "relationships": [],
        }, ensure_ascii=False),
        # 第二轮：补全为完整外观锚点。
        json.dumps({
            "subject_kind": "person",
            "important": True,
            "reason": "已确认真名",
            "role": "重要配角",
            "appearance_canonical": (
                "成年黑发男子，身穿深灰色皮甲短衫，腰间佩刀，"
                "体格壮实，左眉留有一道浅疤"
            ),
            "personality": "服从命令",
            "speech_style": "简短应答",
            "relationships": [],
        }, ensure_ascii=False),
    ]

    async def fake_chat(messages, **_kwargs):
        prompts.append(messages[0]["content"])
        return responses[len(prompts) - 1]

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)

    result = asyncio.run(portraits.assess_new_character(
        "丁力",
        "丁力听令后带人巡查山门。",
        style="国风",
        known_names=["萧炎"],
        ep_label="第 21 集",
        require_identity_card=True,
    ))

    assert len(prompts) == 2  # 恰好重试一次
    assert "上一轮 appearance_canonical 不完整" in prompts[1]
    assert result["card_complete"] is True
    assert result["important"] is True


def test_mentioned_only_unknown_character_does_not_require_identity_card() -> None:
    known = {"白洁", "小晶"}

    assert portraits._candidate_requires_identity_card(
        {
            "name": "钟五",
            "identity_kind": "named",
            "kind": "mentioned",
        },
        known,
    ) is False
    assert portraits._candidate_requires_identity_card(
        {
            "name": "钟五",
            "identity_kind": "named",
            "kind": "onscreen",
        },
        known,
    ) is True
    assert portraits._candidate_requires_identity_card(
        {
            "name": "小晶",
            "identity_kind": "named",
            "kind": "onscreen",
        },
        known,
    ) is False
    assert portraits._candidate_requires_identity_card(
        {
            "name": "魂天帝",
            "kind": "onscreen",
        },
        known,
    ) is True


def test_character_card_truncation_is_reported_as_generation_error(
    monkeypatch,
) -> None:
    async def truncated_chat(*_args, **_kwargs):
        return '{"subject_kind":"person","important":true,"reason":"响应被截断'

    monkeypatch.setattr(portraits.model_gateway, "chat", truncated_chat)

    with pytest.raises(
        portraits.ContentGenerationError,
        match="人物卡结构化输出不完整",
    ):
        asyncio.run(portraits.assess_new_character(
            "丁力",
            "丁力走进大厅。",
            style="国风",
            known_names=[],
            ep_label="第 1 集",
            require_identity_card=True,
        ))


def test_ensure_character_card_keeps_auto_added_card_when_portrait_fails(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎现身，紫色长发。美杜莎再次出手。美杜莎统领蛇人一族。" * 3)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*a, **k):
        return {"subject_kind": "person", "important": True, "reason": "反复出场", "role": "反派",
                "appearance_canonical": "紫发妖娆女子，紫色长发，金瞳蛇眸，蛇纹长裙，气场冷艳标志性蛇瞳",
                "personality": "", "speech_style": "", "relationships": []}

    portrait_calls = 0

    async def boom(*a, **k):
        nonlocal portrait_calls
        portrait_calls += 1
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", boom)
    monkeypatch.setattr(portraits, "code_ref", lambda *_args, **_kwargs: "（测试错误）")

    res = asyncio.run(portraits.ensure_character_card("p1", "美杜莎", 21))
    assert res["status"] == "added" and res["has_portrait"] is False
    assert portrait_calls == 1
    # 供应商失败不回滚 AI 已确认的卡片；分镜前自动重试定妆资产。
    names = [c["name"] for c in json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]]
    assert "美杜莎" in names
    assert conn.execute("SELECT COUNT(*) c FROM character_portraits WHERE character_name='美杜莎'").fetchone()["c"] == 0
    queue = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert queue[0]["status"] == "auto_applied_asset_failed"


def test_existing_pending_character_is_auto_applied_without_reassessment(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "葛叶陪同纳兰嫣然现身。葛叶出手阻拦萧炎。" * 4)
    _patch_settings(monkeypatch, conn)
    conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
    card = Character(
        name="葛叶",
        role="重要配角",
        appearance_canonical="老年男性，灰白长发束起，身着云岚宗青灰长袍，面容沉稳，腰佩宗门令牌",
    )
    pending = [{
        "id": "change_old",
        "kind": "new_character",
        "status": "pending",
        "character": "葛叶",
        "ep_start": 5,
        "reason": "有具名台词与持续行动",
        "payload": {"character_card": card.model_dump(mode="json")},
    }]
    conn.execute(
        "UPDATE projects SET bible_auto_changes_json=? WHERE id='p1'",
        (json.dumps(pending, ensure_ascii=False),),
    )
    conn.commit()

    async def forbidden_assess(*_args, **_kwargs):
        raise AssertionError("已有待审卡不应重复调用 AI 评估")

    async def fake_portrait(project_id, name, style, appearance, *, ep_start):
        assert name == "葛叶" and ep_start == 5
        return ("/tmp/葛叶.jpg", "fake prompt")

    monkeypatch.setattr(portraits, "assess_new_character", forbidden_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", fake_portrait)

    result = asyncio.run(portraits.ensure_character_card("p1", "葛叶", 5))

    assert result["status"] == "added"
    bible = json.loads(conn.execute(
        "SELECT bible_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_json"])
    assert "葛叶" in {item["name"] for item in bible["characters"]}
    changes = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert changes[0]["status"] == "auto_applied"


def test_auto_discovered_character_pack_starts_at_first_appearance(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "auto-character-pack.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,bible_version,bible_status,created_at) "
        "VALUES('p1','斗破苍穹','planned',?,1,'ready',1)",
        (bible.model_dump_json(),),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) VALUES(?,?,?,?,?)",
        ("p1", 5, "葛叶登场", "葛叶陪同纳兰嫣然现身，并与萧炎正面交锋。" * 8, 240),
    )
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,source_chapters,status,created_at) "
        "VALUES('e5','p1',5,'葛叶登场','[5]','planned',1)"
    )
    conn.commit()

    async def fake_assess(*_args, **_kwargs):
        return {
            "subject_kind": "person",
            "important": True, "reason": "具名对手且持续参与主线", "role": "重要配角",
            "appearance_canonical": "老年男性，灰白长发束起，身着云岚宗青灰长袍，面容沉稳，腰佩宗门令牌",
            "personality": "沉稳", "speech_style": "克制", "relationships": [],
        }

    async def fake_portrait(project_id, name, style, appearance, *, ep_start):
        path = tmp_path / f"{name}-{ep_start}.jpg"
        path.write_bytes(b"\xff\xd8\xff\xe0automatic-character")
        return str(path), "fake prompt"

    async def fake_review(*_args, **_kwargs):
        return {
            "identity_match": 1.0, "presentation_match": 1.0, "clean_frame": 1.0,
            "overall": 1.0, "issues": [], "hard_failures": [], "hard_gate_passed": True,
        }

    pack_calls = []

    async def fake_pack(**kwargs):
        pack_calls.append(kwargs)
        return {"status": "ready", "portrait_id": kwargs["portrait_id"]}

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", fake_portrait)
    monkeypatch.setattr(portraits, "_review_portrait_asset", fake_review)
    monkeypatch.setattr("app.multiview.ensure_character_multiview_pack", fake_pack)

    result = asyncio.run(portraits.ensure_character_card("p1", "葛叶", 5))

    assert result["status"] == "added" and result["has_portrait"] is True
    row = conn.execute(
        "SELECT ep_start,ep_end,pack_status FROM character_portraits "
        "WHERE project_id='p1' AND character_name='葛叶'"
    ).fetchone()
    assert (row["ep_start"], row["ep_end"], row["pack_status"]) == (5, None, "ready")
    assert pack_calls[0]["ep_start"] == 5


def test_minor_character_is_skipped_and_negatively_cached(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "路人甲走过。" * 6)
    _patch_settings(monkeypatch, conn)

    calls = {"assess": 0}

    async def fake_assess(*a, **k):
        calls["assess"] += 1
        return {"subject_kind": "person", "important": False, "reason": "路人", "role": "重要配角",
                "appearance_canonical": "", "personality": "", "speech_style": "", "relationships": []}

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)

    res = asyncio.run(portraits.ensure_character_card("p1", "路人甲", 21))
    assert res["status"] == "skipped_minor"
    assert calls["assess"] == 1
    # 21 集判过不重要 → 22 集在重判窗口内，直接命中负缓存，不再调模型
    res2 = asyncio.run(portraits.ensure_character_card("p1", "路人甲", 22))
    assert res2["status"] == "skipped_minor"
    assert calls["assess"] == 1


def test_ensure_cards_for_screenplay_blocks_unknown_names_without_building_cards(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎现身，紫色长发。美杜莎再次出手。美杜莎统领蛇人一族。" * 3)
    _patch_settings(monkeypatch, conn)

    seen: list[tuple[str, int]] = []

    async def fake_ensure(project_id, name, episode_no):
        seen.append((name, episode_no))
        return {"status": "added", "name": name, "has_portrait": True}

    monkeypatch.setattr(portraits, "ensure_character_card", fake_ensure)

    class _Scene:
        def __init__(self, chars): self.characters = chars

    class _Screenplay:
        scene_outline = [_Scene(["萧炎", "美杜莎"]), _Scene(["美杜莎", "纳兰嫣然"])]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible))

    assert seen == []
    assert out["checked"] == 2 and out["added"] == []
    assert len(out["blocking_errors"]) == 2
    assert all("请回到剧本阶段" in message for message in out["blocking_errors"])


def _insert_portrait(conn, pid, name, ep_start, ep_end, appearance, image_path) -> None:
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
        "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (f"po_{name}_{ep_start}", pid, name, ep_start, ep_end, appearance, "p", image_path, None, 1, 0.0))
    conn.commit()


def test_ensure_cards_for_screenplay_redraws_on_appearance_drift(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "萧炎一夜白头，玄色劲装染血，左眼覆着一道狰狞刀疤。萧炎冷然出手。" * 3)
    _patch_settings(monkeypatch, conn)
    # 已有开区间定妆照（适用集 1~ 至今）
    _insert_portrait(conn, "p1", "萧炎", 1, None, "黑发少年，玄色劲装，目光坚定，身形修长", "/tmp/xiao_ep1.jpg")

    async def fake_screen(entries, ep_label):
        assert any(e["name"] == "萧炎" for e in entries) and "萧炎" in entries[0]["fragments"]
        return {"萧炎": {"new_appearance": "白发青年，玄色染血劲装，左眼狰狞刀疤，目光冷峻", "reason": "白头+刀疤"}}

    async def fake_redraw(project_id, name, style, appearance, *, base_path, ep_start):
        assert base_path == "/tmp/xiao_ep1.jpg" and ep_start == 21  # 以旧图为底、新段从本集起
        return (f"/tmp/{name}_ep{ep_start}.jpg", "redraw prompt")

    monkeypatch.setattr(portraits, "screen_appearance_changes", fake_screen)
    monkeypatch.setattr(portraits, "_redraw_portrait", fake_redraw)

    class _Scene:
        def __init__(self, chars): self.characters = chars

    class _Screenplay:
        scene_outline = [_Scene(["萧炎"])]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible))

    assert [r["name"] for r in out["redrawn"]] == ["萧炎"]
    rows = conn.execute(
        "SELECT ep_start, ep_end, appearance FROM character_portraits WHERE character_name='萧炎' ORDER BY ep_start"
    ).fetchall()
    # 旧段右区间关到本集-1，新开区间段从本集起
    assert (rows[0]["ep_start"], rows[0]["ep_end"]) == (1, 20)
    assert (rows[1]["ep_start"], rows[1]["ep_end"]) == (21, None)
    assert "白发" in rows[1]["appearance"]
    # bible 锚点同步成最新（供人物谱 UI 展示）
    chars = json.loads(conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"])["characters"]
    assert "白发" in next(c for c in chars if c["name"] == "萧炎")["appearance_canonical"]


def test_no_drift_redraw_when_portrait_starts_at_or_after_this_episode(monkeypatch) -> None:
    """本集（之后）才登场的定妆照天然是最新，不应再判漂移/重绘。"""
    conn = _make_conn()
    _seed_project(conn, "萧炎一夜白头，玄色劲装染血，左眼覆着一道狰狞刀疤。" * 3)
    _patch_settings(monkeypatch, conn)
    _insert_portrait(conn, "p1", "萧炎", 21, None, "黑发少年，玄色劲装，目光坚定", "/tmp/xiao_ep21.jpg")

    calls = {"screen": 0}

    async def fake_screen(entries, ep_label):
        calls["screen"] += 1
        return {}

    monkeypatch.setattr(portraits, "screen_appearance_changes", fake_screen)

    class _Scene:
        def __init__(self, chars): self.characters = chars

    class _Screenplay:
        scene_outline = [_Scene(["萧炎"])]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible))
    assert out["redrawn"] == [] and calls["screen"] == 0  # ep_start>=本集 → 直接跳过，连判定都不调


def test_ensure_cards_backfills_identical_ready_future_portrait(
    monkeypatch, tmp_path,
) -> None:
    conn = _make_conn()
    _seed_project(conn, "萧炎在本集登场。")
    _patch_settings(monkeypatch, conn)
    image = tmp_path / "xiao_ep22.jpg"
    image.write_bytes(b"ready")
    appearance = "黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩"
    _insert_portrait(conn, "p1", "萧炎", 22, None, appearance, str(image))

    async def unexpected_screen(*_args, **_kwargs):
        raise AssertionError("向前扩展相同完整包后不应再判外观漂移")

    monkeypatch.setattr(portraits, "screen_appearance_changes", unexpected_screen)

    class _Scene:
        characters = ["萧炎"]

    class _Screenplay:
        scene_outline = [_Scene()]
        beats: list = []

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    out = asyncio.run(
        portraits.ensure_cards_for_screenplay("p1", 21, _Screenplay(), bible)
    )

    row = conn.execute(
        "SELECT ep_start,ep_end FROM character_portraits WHERE character_name='萧炎'"
    ).fetchone()
    assert (row["ep_start"], row["ep_end"]) == (21, None)
    assert out["backfilled"] == [{
        "name": "萧炎",
        "portrait_id": "po_萧炎_22",
        "ep_start": 21,
        "previous_ep_start": 22,
        "image_path": str(image),
        "pack_status": "ready",
        "reused": True,
    }]
    assert out["redrawn"] == []


def test_bible_for_episode_picks_segment_anchor(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "x")
    _patch_settings(monkeypatch, conn)
    _insert_portrait(conn, "p1", "萧炎", 1, 20, "早期：黑发少年，玄色劲装，目光坚定", "/tmp/a.jpg")
    _insert_portrait(conn, "p1", "萧炎", 21, None, "后期：白发青年，染血劲装，左眼刀疤", "/tmp/b.jpg")

    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]))
    original = bible.characters[0].appearance_canonical

    v10 = portraits.bible_for_episode("p1", bible, 10)
    v25 = portraits.bible_for_episode("p1", bible, 25)
    assert "黑发少年" in v10.characters[0].appearance_canonical
    assert "白发青年" in v25.characters[0].appearance_canonical
    # 取本集视图不应改动传入的原 bible
    assert bible.characters[0].appearance_canonical == original


def test_discover_character_candidates_keeps_typed_functionals(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎",
            role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )

    async def fake_chat(*_args, **kwargs):
        # Raw HTTP-200 responses are not durable structured successes.  The
        # validated discovery Artifact owns recovery; provider raw reuse stays
        # disabled so a malformed/semantic-invalid response cannot stick.
        assert kwargs["call_meta"]["reuse_successful_operation"] is False
        return json.dumps(_identity_wire_for_call(kwargs, [
                {
                    "source_label": "魂天帝", "canonical_name": "魂天帝",
                    "identity_kind": "named", "kind": "onscreen",
                    "evidence": "魂天帝踏着血云现身",
                },
                {
                    "source_label": "萧炎", "canonical_name": "萧炎",
                    "identity_kind": "named", "kind": "onscreen",
                    "evidence": "萧炎迎空而起",
                },
                {
                    "source_label": "守卫", "canonical_name": "",
                    "identity_kind": "functional", "kind": "onscreen",
                    "evidence": "守卫后退",
                },
            ], messages=_args[0]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.discover_character_candidates(
        "魂天帝踏着血云现身，萧炎迎空而起，守卫仓促后退。",
        bible,
        1926,
    ))

    assert [item["name"] for item in result] == ["魂天帝", "萧炎", "守卫"]
    assert result[-1]["identity_kind"] == "functional"


def test_discover_character_candidates_rejects_malformed_json_without_retry(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="孟浩",
            role="主角",
            appearance_canonical="黑发书生，青色长衫，目光清澈",
        )],
    )

    async def fake_chat(messages, **_kwargs):
        return (
            '```json\n{"characters":[{"source_label":"孟浩",'
            '"canonical_name":"孟浩","identity_kind":"named","kind":"onscreen",'
            '"evidence":"原文写道"孟浩说道"。","future_evidence":""}]}\n```'
        )

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(portraits.discover_character_candidates(
            "孟浩说道，他要去靠山宗。",
            bible,
            1,
        ))


def test_current_identity_rejects_same_scene_person_misbinding(
    monkeypatch,
) -> None:
    """Literal co-occurrence cannot prove that one label is another person."""
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["reuse_successful_operation"] is False
        return json.dumps({
            "named": [{
                "source_label": "银袍女子",
                "canonical_name": "孟浩",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence_id": "CE:legacy-free-combination",
            }],
            "functional": [],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredFormatError,
    ):
        asyncio.run(portraits.discover_character_candidates(
            "孟浩走在前面，银袍女子跟在后面。",
            Bible(
                world=World(visual_style_canonical="国风"),
                characters=[Character(
                    name="孟浩",
                    role="主角",
                    appearance_canonical="黑发书生，青色长衫，目光坚定",
                )],
            ),
            1,
        ))

    assert calls == 1


def test_screenplay_discovery_resolves_appearance_label_from_next_ten_chapters(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "绿袍男子拦在萧炎面前，厉声呵斥。")
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',31,?)",
        ("绿袍男子摘下斗笠，众人这才认出他正是丁力。",),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',40,?)",
        ("丁力再次现身。",),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',41,?)",
        ("超出十章的内容不得进入身份预检。",),
    )
    conn.commit()
    _patch_settings(monkeypatch, conn)

    prompts: list[str] = []

    async def fake_chat(_messages, **_kwargs):
        prompt = _messages[0]["content"]
        prompts.append(prompt)
        phase = _kwargs["call_meta"]["discovery_phase"]
        if phase == "current":
            assert "绿袍男子摘下斗笠" not in prompt
            return json.dumps(_identity_wire_for_call(_kwargs, [{
                    "source_label": "绿袍男子",
                    "canonical_name": "",
                    "identity_kind": "functional",
                    "kind": "onscreen",
                    "evidence": "绿袍男子拦路呵斥",
                    "future_evidence": "",
                }]), ensure_ascii=False)
        assert "绿袍男子摘下斗笠" in prompt
        assert "丁力再次现身" in prompt
        assert "超出十章" not in prompt
        return json.dumps(_identity_wire_for_call(_kwargs, [{
                "source_label": "绿袍男子",
                "canonical_name": "丁力",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "绿袍男子拦路呵斥",
                "future_evidence": "绿袍男子摘下斗笠，众人这才认出他正是丁力。",
            }]), ensure_ascii=False)

    ensured: list[str] = []

    async def fake_ensure(
        _project_id, name, _episode_no, *,
        generate_portrait=True, require_identity_card=False,
    ):
        ensured.append(name)
        assert generate_portrait is False
        assert require_identity_card is True
        return {"status": "added", "name": name, "has_portrait": False, "portrait_deferred": True}

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(portraits, "ensure_character_card", fake_ensure)
    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    ))

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 21, "绿袍男子拦在萧炎面前，厉声呵斥。", bible,
        generate_portraits=False,
    ))

    assert ensured == ["丁力"]
    assert len(prompts) == 2
    assert result["future_context_label"] == "第 31-40 章（仅姓名消歧）"
    assert len(result["resolutions"]) == 1
    resolution = result["resolutions"][0]
    assert resolution["source_label"] == "绿袍男子"
    assert resolution["canonical_name"] == "丁力"
    assert resolution["resolution"] == "future_identity"
    assert resolution["identity_group"] == "current-1:F1"
    assert resolution["decision_contract_version"] == (
        portraits.FUTURE_IDENTITY_DECISION_VERSION
    )
    assert resolution["structural_identity_policy_version"] == (
        portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
    )
    assert resolution["authority_id"] == "bible:丁力"


def test_future_identity_model_scans_all_batches_and_named_evidence_wins(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎",
            role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    future_text = (
        "前批章节暂无身份线索。"
        + "甲" * (portraits.CAST_DISCOVERY_FUTURE_CONTEXT_BUDGET * 2)
        + "青衣人摘下面具，萧炎这才认出他就是丁力。"
    )
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        if _kwargs["call_meta"]["discovery_phase"] == "future_identity":
            assert "前批章节暂无身份线索" not in prompt
            return json.dumps(_identity_wire_for_call(_kwargs, [{
                "source_label": "青衣人",
                "canonical_name": "丁力",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "青衣人拦路",
                "future_evidence": "青衣人摘下面具，萧炎这才认出他就是丁力。",
            }], messages=messages), ensure_ascii=False)
        return json.dumps(_identity_wire_for_call(_kwargs, [{
            "source_label": "青衣人",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "青衣人拦路",
            "future_evidence": "",
        }], messages=messages), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "青衣人拦住萧炎。",
        bible,
        21,
        future_text=future_text,
        future_label="第 31-40 章",
    ))

    assert len(prompts) == 2
    assert "他就是丁力" in prompts[-1]
    assert [(item["source_label"], item["name"], item["identity_kind"]) for item in candidates] == [
        ("青衣人", "丁力", "named"),
    ]


def test_future_identity_does_not_bind_bible_name_from_raw_cooccurrence(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="都市漫画"),
        characters=[Character(
            name="赵振",
            role="重要配角",
            appearance_canonical="中年男子，深色西装，方脸短发，体格高大",
        )],
    )
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["discovery_phase"] == "future_identity"
        return json.dumps(_identity_wire_for_call(kwargs, [{
            "source_label": "那间学校的校长",
            "identity_kind": "functional",
        }]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "那间学校的校长",
            "source_label": "那间学校的校长",
            "identity_kind": "functional",
            "identity_group": "current:school-principal",
            "kind": "onscreen",
        }],
        source_text="那间学校的校长从楼上走下来。",
        future_text=(
            "到了酒店，原来那个男人是王申学校的校长。"
            "聪慧的白洁马上反应过来是那个“大象”赵振的主意。"
        ),
        bible=bible,
        episode_no=5,
        future_label="后续章节",
    ))

    assert calls == 1
    assert [
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    ] == [("那间学校的校长", "那间学校的校长", "functional")]


def test_future_identity_untraceable_name_is_one_call_hard_failure(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="都市漫画"),
        characters=[Character(
            name="陈三",
            role="重要配角",
            appearance_canonical="成年男子，短发，深色夹克，体格结实，神情强硬",
        )],
    )
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["discovery_phase"] == "future_identity"
        schema = kwargs["response_format"]["json_schema"]["schema"]
        group_key = next(iter(
            schema["properties"]["decisions"]["properties"]
        ))
        decisions = schema["properties"]["decisions"]["properties"]
        evidence = schema["properties"]["reveal_evidence_ids"][
            "properties"
        ]
        return json.dumps({
            "decisions": {
                group_key: next(
                    value for value in decisions[group_key]["enum"]
                    if value.startswith("N:")
                ),
            },
            "revealed_names": {group_key: "陈三"},
            "revealed_name_kinds": {group_key: "personal_name"},
            "reveal_evidence_ids": {
                group_key: next(
                    value for value in evidence[group_key]["enum"]
                    if value
                ),
            },
        }, ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    with pytest.raises(model_gateway.StructuredSemanticError):
        asyncio.run(portraits.resolve_future_identity_candidates(
            [{
                "name": "三哥",
                "source_label": "三哥",
                "identity_kind": "functional",
                "identity_group": "current:third-brother",
                "kind": "onscreen",
            }],
            source_text="三哥推门进来。",
            future_text="后来众人仍只叫他三哥，没有交代真名。",
            bible=bible,
            episode_no=7,
            future_label="后续章节",
        ))
    assert calls == 1


def test_future_identity_cannot_bind_unanchored_bible_authority(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        schema = kwargs["response_format"]["json_schema"]["schema"]
        group_key = next(iter(
            schema["properties"]["decisions"]["properties"]
        ))
        allowed = schema["properties"]["decisions"]["properties"][
            group_key
        ]["enum"]
        assert not any(":bible:孟浩:" in value for value in allowed)
        # Simulate an HTTP-200 provider which ignored the strict enum.
        return json.dumps({
            "decisions": {
                group_key: f"K:{group_key}:bible:孟浩:forged",
            },
            "revealed_names": {group_key: ""},
            "revealed_name_kinds": {group_key: ""},
            "reveal_evidence_ids": {group_key: ""},
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="decision_id 越界",
    ):
        asyncio.run(portraits.resolve_future_identity_candidates(
            [{
                "name": "陌生门卫",
                "source_label": "陌生门卫",
                "identity_kind": "functional",
                "identity_group": "grp:gate",
                "kind": "onscreen",
            }],
            source_text="陌生门卫守在山门。",
            future_text="后来陌生门卫仍守在山门，从未说明姓名。",
            bible=Bible(
                world=World(visual_style_canonical="国风"),
                characters=[Character(
                    name="孟浩",
                    role="主角",
                    appearance_canonical="黑发书生，青色长衫，目光坚定",
                )],
            ),
            episode_no=1,
        ))

    assert calls == 1


def test_attempt12_future_identity_group_decision_is_exact_and_one_call(
    monkeypatch,
) -> None:
    """One backend group decision must fan out to all aliases without quote copying."""
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name="许清",
                role="重要配角",
                appearance_canonical="银袍女子，黑发冷眸，身形高挑",
            ),
            Character(
                name="李富贵",
                role="重要配角",
                appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
            ),
        ],
    )
    candidates = [
        {
            "name": "会飞的女人",
            "source_label": "会飞的女人",
            "identity_kind": "functional",
            "identity_group": "current-1:F3",
            "kind": "onscreen",
        },
        {
            "name": "许师姐",
            "source_label": "许师姐",
            "identity_kind": "functional",
            "identity_group": "current-1:F3",
            "kind": "mentioned",
        },
        {
            "name": "白白净净身子较胖",
            "source_label": "白白净净身子较胖",
            "identity_kind": "functional",
            "identity_group": "current-1:F4",
            "kind": "onscreen",
        },
    ]
    future_text = (
        "许师姐收起风幡，绿袍修士向她行礼。\n\n"
        "白白净净身子较胖的少年仍被众人叫作小胖子。"
    )
    calls: list[dict] = []

    async def fake_chat(messages, **kwargs):
        calls.append(kwargs)
        assert kwargs["response_format"]["type"] == "json_schema"
        assert kwargs["response_format"]["json_schema"]["strict"] is True
        assert kwargs["response_format"]["json_schema"]["name"] == (
            "screenplay_future_identity_resolution_v10"
        )
        assert kwargs["call_meta"]["response_format_required"] is True
        assert kwargs["call_meta"]["format_attempt"] == 0
        assert kwargs["call_meta"]["semantic_attempt"] == 0
        schema = kwargs["response_format"]["json_schema"]["schema"]
        assert list(schema["properties"]["decisions"]["properties"]) == [
            "G001", "G002",
        ]
        return json.dumps(
            _identity_wire_for_call(
                kwargs,
                [
                    {
                        "source_label": "许师姐",
                        "identity_kind": "functional",
                    },
                    {
                        "source_label": "白白净净身子较胖",
                        "identity_kind": "functional",
                    },
                ],
                messages=messages,
            ),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        candidates,
        source_text="会飞的女人携着白净胖少年离开。",
        future_text=future_text,
        bible=bible,
        episode_no=1,
        future_label="后续章节",
    ))

    assert len(calls) == 1
    assert {
        (item["source_label"], item["name"], item["identity_kind"])
        for item in resolved
    } == {
        ("会飞的女人", "会飞的女人", "functional"),
        ("许师姐", "许师姐", "functional"),
        ("白白净净身子较胖", "白白净净身子较胖", "functional"),
    }
    assert all(item["identity_kind"] == "functional" for item in resolved)


def test_future_identity_cooccurrence_does_not_mint_known_authority(
    monkeypatch,
) -> None:
    """A named person mentioning an alias is not proof they are one person."""
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        decision_properties = kwargs["response_format"]["json_schema"][
            "schema"
        ]["properties"]["decisions"]["properties"]
        options = next(iter(decision_properties.values()))["enum"]
        assert not any(
            option.startswith("K:") and "bible:赵武刚" in option
            for option in options
        )
        return json.dumps(
            _identity_wire_for_call(
                kwargs,
                [{
                    "source_label": "许师姐",
                    "identity_kind": "functional",
                }],
                messages=messages,
            ),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    candidates = [{
        "name": "赵武刚",
        "source_label": "赵武刚",
        "identity_kind": "named",
        "identity_group": "current-1:named:zhao",
        "authority_id": "bible:赵武刚",
        "decision_provenance": (
            portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
        ),
        "kind": "onscreen",
    }, {
        "name": "许师姐",
        "source_label": "许师姐",
        "identity_kind": "functional",
        "identity_group": "current-1:F5",
        "kind": "mentioned",
    }]
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        candidates,
        source_text="许师姐收起风幡。",
        future_text="赵武刚谈到许师姐，嘱咐众人留意她的动向。",
        bible=Bible(
            characters=[Character(
                name="赵武刚",
                role="外宗弟子",
                appearance_canonical="黑发男子，灰色长袍，背负长剑",
            )],
            world=World(visual_style_canonical="国风"),
        ),
        episode_no=1,
    ))

    assert calls == 1
    alias = next(
        item for item in resolved if item["source_label"] == "许师姐"
    )
    assert alias["identity_kind"] == "functional"
    assert alias["name"] == "许师姐"
    assert not alias.get("authority_id")


def test_attempt12_invalid_future_decision_is_one_call_and_zero_downstream(
    monkeypatch,
) -> None:
    """A strict HTTP-200 cannot duplicate a group or re-sign an existing name."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    candidates = [
        {
            "name": "会飞的女人",
            "source_label": "会飞的女人",
            "identity_kind": "functional",
            "identity_group": "current-1:F3",
            "kind": "onscreen",
        },
        {
            "name": "许师姐",
            "source_label": "许师姐",
            "identity_kind": "functional",
            "identity_group": "current-1:F3",
            "kind": "mentioned",
        },
        {
            "name": "白白净净身子较胖",
            "source_label": "白白净净身子较胖",
            "identity_kind": "functional",
            "identity_group": "current-1:F4",
            "kind": "onscreen",
        },
    ]
    future_text = (
        "许师姐收起风幡。\n\n"
        "白白净净身子较胖的少年被称作小胖子。"
    )
    provider_calls = 0
    downstream: list[str] = []

    async def fake_current(*_args, **_kwargs):
        return candidates

    async def invalid_future_provider(_messages, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        schema = kwargs["response_format"]["json_schema"]["schema"]
        decision_properties = schema["properties"]["decisions"][
            "properties"
        ]
        evidence_properties = schema["properties"][
            "reveal_evidence_ids"
        ]["properties"]
        return json.dumps({
            "decisions": {
                "G001": next(
                    value for value in decision_properties["G001"]["enum"]
                    if value.startswith("F:")
                ),
                "G002": next(
                    value for value in decision_properties["G002"]["enum"]
                    if value.startswith("N:")
                ),
            },
            "revealed_names": {"G001": "", "G002": "李富贵"},
            "revealed_name_kinds": {"G001": "", "G002": "personal_name"},
            "reveal_evidence_ids": {
                "G001": "",
                "G002": next(
                    value for value in evidence_properties["G002"]["enum"]
                    if value
                ),
            },
        }, ensure_ascii=False)

    async def forbidden_coverage(*_args, **_kwargs):
        downstream.append("coverage")
        raise AssertionError("invalid future identity reached coverage")

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(
        portraits, "extract_current_identity_candidates", fake_current,
    )
    monkeypatch.setattr(
        portraits,
        "audit_identity_coverage_from_structural_evidence",
        forbidden_coverage,
    )
    monkeypatch.setattr(model_gateway, "chat", invalid_future_provider)

    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="NEW 不得重新签发已有 authority",
    ):
        asyncio.run(portraits.discover_character_candidates(
            "会飞的女人携着白净胖少年离开。",
            Bible(
                world=World(visual_style_canonical="国风"),
                characters=[
                    Character(
                        name="许清",
                        role="重要配角",
                        appearance_canonical="银袍女子，黑发冷眸，身形高挑",
                    ),
                    Character(
                        name="李富贵",
                        role="重要配角",
                        appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
                    ),
                ],
            ),
            1,
            future_text=future_text,
            future_label="后续章节",
        ))

    assert provider_calls == 1
    assert downstream == []


def test_attempt12_old_split_wire_is_one_call_hard_failure(
    monkeypatch,
) -> None:
    calls = 0

    async def old_attempt12_wire(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["format_attempt"] == 0
        return json.dumps({
            "known_named": [{
                "source_label": "许师姐",
                "authority_id": "bible:许清",
                "future_evidence": "许师姐收起风幡",
            }],
            "new_named": [{
                "source_label": "白白净净身子较胖",
                "canonical_name": "李富贵",
                "future_evidence": "小胖子跟上来",
            }],
            "functional": [
                {"source_label": "白白净净身子较胖"},
            ],
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", old_attempt12_wire)
    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(portraits.resolve_future_identity_candidates(
            [{
                "name": "白白净净身子较胖",
                "source_label": "白白净净身子较胖",
                "identity_kind": "functional",
                "identity_group": "current-1:F4",
                "kind": "onscreen",
            }],
            source_text="白净胖少年跟随众人。",
            future_text="小胖子跟上来，但没有交代真名。",
            bible=Bible(
                world=World(visual_style_canonical="国风"),
                characters=[],
            ),
            episode_no=1,
        ))

    assert calls == 1


def test_future_identity_operation_binds_exact_outbound_semantics(
    monkeypatch,
) -> None:
    bible = Bible(world=World(visual_style_canonical="都市漫画"), characters=[])
    operations: list[tuple[str, str]] = []

    async def fake_structured(*_args, **kwargs):
        assert kwargs["operation_id"].startswith(
            "screenplay.identity.future.v11:"
        )
        assert (
            kwargs["response_format"]["json_schema"]["name"]
            == "screenplay_future_identity_resolution_v10"
        )
        assert (
            kwargs["call_meta"]["contract_version"]
            == portraits.FUTURE_IDENTITY_DECISION_VERSION
        )
        operations.append((kwargs["operation_id"], kwargs["call_meta"]["model"]))
        return portraits.FutureIdentityCandidateResponse.model_validate(
            _future_identity_wire(
                [],
                provider_schema=kwargs["response_format"]["json_schema"][
                    "schema"
                ],
            )
        )

    monkeypatch.setattr(
        portraits.model_gateway, "chat_structured", fake_structured,
    )
    monkeypatch.setattr(
        portraits.hiagent,
        "text_request_semantic_settings",
        lambda _provider: {"uses_temperature": True},
    )
    model = "model-a"
    monkeypatch.setattr(
        portraits.hiagent,
        "text_request_token_limits",
        lambda **_kwargs: ("hiagent", model, 4096),
    )
    arguments = dict(
        candidates=[{
            "name": "三哥",
            "source_label": "三哥",
            "identity_kind": "functional",
            "identity_group": "current:third-brother",
            "kind": "onscreen",
        }],
        source_text="三哥推门进来。",
        future_text="后来仍称三哥。",
        bible=bible,
        episode_no=7,
        future_label="后续章节",
    )
    asyncio.run(portraits.resolve_future_identity_candidates(**arguments))
    asyncio.run(portraits.resolve_future_identity_candidates(**arguments))
    model = "model-b"
    asyncio.run(portraits.resolve_future_identity_candidates(**arguments))

    assert operations[0][0] == operations[1][0]
    assert operations[2][0] != operations[0][0]
    assert [item[1] for item in operations] == ["model-a", "model-a", "model-b"]


def test_identity_discovery_preserves_nonliteral_functional_label_as_synthetic(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="李富贵",
            role="重要配角",
            appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
        )],
    )

    async def fake_chat(messages, **_kwargs):
        return json.dumps(_identity_wire_for_call(_kwargs, [{
            "source_label": "白白净净身较胖的少年",
            "canonical_name": "",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "onscreen",
            "evidence": "原文中的白净胖少年",
        }], messages=messages), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "王有材身边另一个则是白白净净身较胖，正缩在裂缝里。",
        bible,
        1,
    ))

    assert candidates[0]["source_label"] == "白白净净身较胖的少年"
    assert candidates[0]["source_label_provenance"] == (
        portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
    )
    assert candidates[0]["identity_kind"] == "functional"
    assert candidates[0]["source_evidence_receipt"]["origin"] == (
        "current_source"
    )
    assert candidates[0]["source_quote"] == (
        "王有材身边另一个则是白白净净身较胖，正缩在裂缝里。"
    )


def test_attempt14_call_63221_current_rf9_preserves_all_distinct_identities(
    monkeypatch,
) -> None:
    """Mirror 63221 with its Bible identity corrected to the RF9 named branch."""
    source_text = "\n\n".join([
        "王伯与王老伯的木匠铺子赚钱，孟浩还欠周员外三两银子。",
        "孟浩看到裂缝里的王有材，旁边有一个虎头虎脑的少年。",
        "王有材说他们被一个会飞的女人抓来。",
        "一个面色苍白，看不出年纪的女子穿着银色长袍，后来被称为许师姐。",
        "王有材身边另一个则是白白净净身子较胖。前方有两个穿着绿色长袍的男子，"
        "两个男子中的一人恭维许师姐，另一个绿袍修士提到掌教。作者耳根请读者收藏。",
    ])
    captured = [
        ("王伯", "F1", "mentioned", "王伯"),
        ("王老伯", "F1", "mentioned", "王老伯"),
        ("周员外", "F2", "mentioned", "周员外"),
        ("虎头虎脑的少年", "F3", "onscreen", "虎头虎脑的少年"),
        ("会飞的女人", "F4", "onscreen", "会飞的女人"),
        ("面色苍白的女子", "F4", "onscreen", "一个面色苍白"),
        ("许师姐", "F4", "onscreen", "许师姐"),
        ("白白净净身子较胖的少年", "F5", "onscreen", "白白净净身子较胖"),
        ("绿袍修士一", "F6", "onscreen", "两个男子中的一人"),
        ("绿袍修士二", "F7", "onscreen", "另一个绿袍修士"),
        ("掌教", "F8", "mentioned", "掌教"),
    ]
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        meta = kwargs["call_meta"]
        assert meta["contract_version"] == (
            portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION
        )
        assert meta["current_identity_version"] == (
            portraits.CURRENT_IDENTITY_DECISION_VERSION
        )
        assert meta["source_batches"] == 1
        assert kwargs["response_format"]["json_schema"]["name"] == (
            "screenplay_current_identity_discovery_v11"
        )
        provider_schema = kwargs["response_format"]["json_schema"]["schema"]
        evidence_refs = provider_schema["$defs"][
            "CurrentNewNamedIdentityDecision"
        ]["properties"]["evidence_ref"]["enum"]
        assert len(evidence_refs) == 5
        characters = [
            {
                "source_label": label,
                "identity_kind": "functional",
                "functional_identity_key": group,
                "kind": kind,
                "evidence": anchor,
            }
            for label, group, kind, anchor in captured
        ]
        characters.extend([
            {
                "source_label": "孟浩",
                "canonical_name": "孟浩",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "孟浩",
            },
            {
                "source_label": "王有材",
                "canonical_name": "王有材",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "王有材",
            },
            {
                "source_label": "耳根",
                "canonical_name": "耳根",
                "identity_kind": "named",
                "kind": "mentioned",
                "evidence": "耳根",
            },
        ])
        return json.dumps(
            _identity_wire_for_call(
                kwargs,
                characters,
                messages=messages,
            ),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        source_text,
        Bible(
            world=World(visual_style_canonical="国风"),
            characters=[Character(
                name="耳根",
                role="已登记稳定身份",
                appearance_canonical="中年男子，素色长衫，面容沉静",
            )],
        ),
        1,
    ))

    assert calls == 1
    assert len(candidates) == 14
    assert {item["source_label"] for item in candidates} == {
        *(label for label, _group, _kind, _anchor in captured),
        "孟浩",
        "王有材",
        "耳根",
    }
    synthetic = {
        item["source_label"]: item for item in candidates
        if item.get("source_label_provenance")
        == portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
    }
    assert set(synthetic) == {
        "面色苍白的女子",
        "白白净净身子较胖的少年",
        "绿袍修士一",
        "绿袍修士二",
    }
    assert synthetic["绿袍修士一"]["identity_group"] != (
        synthetic["绿袍修士二"]["identity_group"]
    )
    assert "两个男子中的一人" in (
        synthetic["绿袍修士一"]["source_quote"]
    )
    assert "另一个绿袍修士" in (
        synthetic["绿袍修士二"]["source_quote"]
    )


def test_attempt14_call_63221_old_functional_bible_name_fails_once(
    monkeypatch,
) -> None:
    calls = 0
    downstream: list[str] = []

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "耳根",
                "identity_kind": "functional",
                "functional_identity_key": "F9",
                "kind": "mentioned",
                "evidence": "耳根",
            }],
            messages=messages,
        ), ensure_ascii=False)

    async def forbidden_future(*_args, **_kwargs):
        downstream.append("future")
        raise AssertionError("old 63221 classification reached future")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        portraits,
        "resolve_future_identity_candidates",
        forbidden_future,
    )
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="functional 不得冒用已登记身份称谓：耳根",
    ):
        asyncio.run(portraits.discover_character_candidates(
            "作者耳根请读者收藏。",
            Bible(
                world=World(visual_style_canonical="国风"),
                characters=[Character(
                    name="耳根",
                    role="已登记稳定身份",
                    appearance_canonical="中年男子，素色长衫，面容沉静",
                )],
            ),
            1,
        ))

    assert calls == 1
    assert downstream == []


def test_attempt15_call_63222_rf10_mirror_succeeds_once(
    monkeypatch,
) -> None:
    """The captured three bad choices all have one explicit RF10 alternative."""
    source_text = "\n\n".join([
        "【第一章书生孟浩】第一章书生孟浩。",
        "王伯的木匠铺子赚钱，早知如此便和王老伯去学木匠手艺。",
        "孟浩还欠了周员外三两银子。",
        "王有材旁边探出一个仈jiu岁少年，这少年虎头虎脑。",
        "王有材身边另一个则是白白净净身子较胖。",
        "一个面色苍白的女子穿着一身银色长袍，站在那里。",
        "许师姐好手段，两个男子中的一人恭维那女子。",
        "许师姐已经到了凝气第七层，另一个绿袍修士提到掌教。",
        "作者耳根请读者收藏。",
    ])
    characters = [
        {
            "source_label": "王伯",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "mentioned",
            "evidence": "王伯的木匠铺子",
        },
        {
            "source_label": "王老伯",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "mentioned",
            "evidence": "王老伯去学木匠",
        },
        {
            "source_label": "周员外",
            "identity_kind": "functional",
            "functional_identity_key": "F2",
            "kind": "mentioned",
            "evidence": "周员外三两银子",
        },
        {
            "source_label": "八岁虎头虎脑少年",
            "identity_kind": "functional",
            "functional_identity_key": "F3",
            "kind": "onscreen",
            "evidence": "这少年虎头虎脑",
        },
        {
            "source_label": "白净较胖少年",
            "identity_kind": "functional",
            "functional_identity_key": "F4",
            "kind": "onscreen",
            "evidence": "白白净净身子较胖",
        },
        {
            "source_label": "银袍女子",
            "identity_kind": "functional",
            "functional_identity_key": "F5",
            "kind": "onscreen",
            "evidence": "穿着一身银色长袍",
        },
        {
            "source_label": "许师姐",
            "identity_kind": "functional",
            "functional_identity_key": "F5",
            "kind": "onscreen",
            "evidence": "许师姐好手段",
        },
        {
            "source_label": "绿袍男子甲",
            "identity_kind": "functional",
            "functional_identity_key": "F6",
            "kind": "onscreen",
            "evidence": "两个男子中的一人",
        },
        {
            "source_label": "绿袍男子乙",
            "identity_kind": "functional",
            "functional_identity_key": "F7",
            "kind": "onscreen",
            "evidence": "另一个绿袍修士",
        },
        {
            "source_label": "孟浩",
            "canonical_name": "孟浩",
            "identity_kind": "named",
            "kind": "onscreen",
            "evidence": "书生孟浩",
        },
        {
            "source_label": "王有材",
            "canonical_name": "王有材",
            "identity_kind": "named",
            "kind": "onscreen",
            "evidence": "王有材旁边",
        },
        {
            "source_label": "耳根",
            "canonical_name": "耳根",
            "identity_kind": "named",
            "kind": "mentioned",
            "evidence": "作者耳根",
        },
    ]
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        meta = kwargs["call_meta"]
        assert meta["contract_version"] == (
            portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION
        )
        assert meta["current_identity_version"] == (
            portraits.CURRENT_IDENTITY_DECISION_VERSION
        )
        assert meta["format_attempt"] == 0
        assert meta["semantic_attempt"] == 0
        assert kwargs["response_format"]["json_schema"]["name"] == (
            "screenplay_current_identity_discovery_v11"
        )
        return json.dumps(
            _identity_wire_for_call(
                kwargs,
                characters,
                messages=messages,
            ),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        source_text,
        Bible(
            world=World(visual_style_canonical="国风"),
            characters=[
                Character(
                    name=name,
                    role="已登记身份",
                    appearance_canonical="黑发长衫，五官清晰，体态稳定，服饰完整",
                )
                for name in ("孟浩", "王有材", "许清", "耳根")
            ],
        ),
        1,
    ))

    assert calls == 1
    assert len(candidates) == 12
    by_label = {item["source_label"]: item for item in candidates}
    assert by_label["王伯"]["identity_group"] == (
        by_label["王老伯"]["identity_group"]
    )
    assert by_label["许师姐"]["identity_kind"] == "functional"
    assert by_label["耳根"]["authority_id"] == "bible:耳根"
    assert by_label["孟浩"]["authority_id"] == "bible:孟浩"
    assert by_label["王有材"]["authority_id"] == "bible:王有材"


def test_current_identity_rf11_schema_stays_under_strict_property_limit() -> None:
    evidence_refs = [f"E{index:03d}" for index in range(1, 63)]
    local_schema = portraits._current_identity_schema(
        evidence_refs,
        known_decision_ids=[],
    )
    provider_schema = portraits._identity_strict_provider_schema(local_schema)
    allowed = {
        "$defs", "$ref", "additionalProperties", "enum", "items",
        "properties", "required", "type",
    }

    def inspect(node: object, *, schema_position: bool = True) -> int:
        if not isinstance(node, dict):
            return 0
        if schema_position:
            assert set(node) <= allowed
        property_count = 0
        for key, child in node.items():
            if key in {"properties", "$defs"}:
                assert isinstance(child, dict)
                if key == "properties":
                    property_count += len(child)
                property_count += sum(
                    inspect(value) for value in child.values()
                )
            elif key == "items":
                property_count += inspect(child)
        return property_count

    assert inspect(provider_schema) < 100
    assert provider_schema["required"] == ["k", "n", "f"]
    assert set(provider_schema["properties"]) == {"k", "n", "f"}
    assert provider_schema["additionalProperties"] is False
    assert provider_schema["$defs"]["CurrentNewNamedIdentityDecision"][
        "properties"
    ]["evidence_ref"]["enum"] == evidence_refs
    assert provider_schema["$defs"]["CurrentFunctionalIdentityDecision"][
        "properties"
    ]["evidence_ref"]["enum"] == evidence_refs
    empty_payload = {"k": [], "n": [], "f": []}
    assert len(json.dumps(empty_payload, separators=(",", ":"))) < 64
    assert provider_schema["$defs"]["CurrentKnownIdentityDecision"][
        "properties"
    ]["decision_id"]["enum"] == ["K:NONE"]


def test_current_identity_rf11_manual_alias_k_is_backend_projected() -> None:
    records = portraits._current_identity_evidence_records(
        "师尊走入殿中。陌生门卫守在门外。"
    )
    evidence_by_ref = {
        f"E{index:03d}": record
        for index, record in enumerate(records, start=1)
    }
    authorities = portraits.identity_authority_registry(
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        [{
            "source_label": "师尊",
            "canonical_name": "苍玄",
            "resolution": "reference_identity",
            "identity_group": "manual:master",
            "authority_id": "manual:cangxuan",
            "decision_provenance": "manual",
        }],
    )
    known = portraits._current_identity_known_decision_catalog(
        evidence_by_ref,
        authorities=authorities,
    )
    selected = next(iter(known.values()))
    payload = {"k": [], "n": [], "f": []}
    payload["k"].append({
        "decision_id": selected["decision_id"],
        "kind": "mentioned",
    })
    response = portraits.CurrentIdentityCandidateResponse.model_validate(
        payload
    )
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known,
        reserved_authority_labels={"师尊", "苍玄"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    assert errors == []
    assert projected[0]["source_label"] == "师尊"
    assert projected[0]["name"] == "苍玄"
    assert projected[0]["authority_id"] == "manual:cangxuan"


def test_current_identity_rf11_manual_alias_cannot_reach_card_materialization(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, project_id TEXT, "
        "episode_no INTEGER, screenplay_character_resolutions TEXT NOT NULL)"
    )
    manual = {
        "source_label": "师尊",
        "canonical_name": "苍玄",
        "resolution": "reference_identity",
        "identity_group": "manual:master",
        "authority_id": "manual:cangxuan",
        "decision_provenance": "manual",
    }
    conn.execute(
        "INSERT INTO episodes VALUES('e1','p1',1,?)",
        (json.dumps([manual], ensure_ascii=False),),
    )
    conn.commit()
    calls = 0
    card_calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        payload = _identity_wire_for_call(
            kwargs,
            [{
                "source_label": "师尊",
                "canonical_name": "苍玄",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "师尊走入殿中",
            }],
            messages=messages,
        )
        return json.dumps(payload, ensure_ascii=False)

    async def forbidden_card(*_args, **_kwargs):
        nonlocal card_calls
        card_calls += 1
        raise AssertionError("non-Bible K alias reached card materialization")

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "true")
    monkeypatch.setattr(
        portraits,
        "_future_chapter_context",
        lambda *_args, **_kwargs: ("", ""),
    )
    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(portraits, "ensure_character_card", forbidden_card)

    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="K authority 不可直接物化人物卡",
    ):
        asyncio.run(portraits.ensure_cards_for_text(
            "p1",
            1,
            "师尊走入殿中。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            generate_portraits=False,
        ))

    assert calls == 1
    assert card_calls == 0
    stored = json.loads(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='e1'"
    ).fetchone()[0])
    assert stored == [manual]


def test_current_identity_rf11_manual_alias_mentioned_persists_one_authority(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, project_id TEXT, "
        "episode_no INTEGER, screenplay_character_resolutions TEXT NOT NULL)"
    )
    manual = {
        "source_label": "师尊",
        "canonical_name": "苍玄",
        "resolution": "reference_identity",
        "identity_group": "manual:master",
        "authority_id": "manual:cangxuan",
        "decision_provenance": "manual",
    }
    conn.execute(
        "INSERT INTO episodes VALUES('e1','p1',1,?)",
        (json.dumps([manual], ensure_ascii=False),),
    )
    conn.commit()
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "师尊",
                "canonical_name": "苍玄",
                "identity_kind": "named",
                "kind": "mentioned",
                "evidence": "提到师尊",
            }],
            messages=messages,
        ), ensure_ascii=False)

    async def forbidden_card(*_args, **_kwargs):
        raise AssertionError("mentioned K alias must not materialize a card")

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "true")
    monkeypatch.setattr(
        portraits,
        "_future_chapter_context",
        lambda *_args, **_kwargs: ("", ""),
    )
    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(portraits, "ensure_character_card", forbidden_card)

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1",
        1,
        "正文提到师尊留下的戒律。",
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        generate_portraits=False,
    ))
    persisted = portraits.persist_screenplay_character_resolutions(
        conn,
        "e1",
        result["resolutions"],
    )
    registry = portraits.identity_authority_registry(
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        persisted,
    )

    assert calls == 1
    assert result["checked"] == 0
    assert {item["authority_id"] for item in persisted} == {
        "manual:cangxuan"
    }
    assert [item["authority_id"] for item in registry] == [
        "manual:cangxuan"
    ]


def test_structural_coverage_rejects_visible_non_bible_reference_before_call(
    monkeypatch,
) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("non-materializable reference reached provider")

    monkeypatch.setattr(model_gateway, "chat", forbidden)
    with pytest.raises(
        portraits.ContentGenerationError,
        match="structural coverage 可见人物只有不可物化",
    ):
        asyncio.run(portraits.audit_identity_coverage_from_structural_evidence(
            [{
                "source_label": "师尊",
                "name": "苍玄",
                "identity_kind": "named",
                "identity_group": "manual:master",
                "authority_id": "manual:cangxuan",
                "kind": "mentioned",
            }],
            structural_evidence=[{
                "identity_key": "师尊",
                "source_segment_ids": ["SRC0001"],
                "usage": "visible",
            }],
            source_text="师尊走入大殿。",
            bible=Bible(
                world=World(visual_style_canonical="国风"),
                characters=[],
            ),
            episode_no=1,
        ))


def test_bible_authority_alias_can_materialize_and_freeze_one_authority(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, project_id TEXT, "
        "episode_no INTEGER, screenplay_character_resolutions TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO episodes VALUES('e1','p1',1,'[]')")
    conn.commit()
    card_calls: list[str] = []

    async def fake_card(_project_id, name, _episode_no, **_kwargs):
        card_calls.append(name)
        return {
            "status": "added",
            "name": name,
            "has_portrait": False,
            "portrait_deferred": True,
        }

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(
        portraits,
        "_future_chapter_context",
        lambda *_args, **_kwargs: ("", ""),
    )
    monkeypatch.setattr(portraits, "ensure_character_card", fake_card)
    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1",
        1,
        "师尊走入大殿。",
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        generate_portraits=False,
        _precomputed_candidates=[{
            "source_label": "师尊",
            "name": "苍玄",
            "identity_kind": "named",
            "identity_group": "structural:master",
            "authority_id": "bible:苍玄",
            "kind": "onscreen",
        }],
    ))
    persisted = portraits.persist_screenplay_character_resolutions(
        conn,
        "e1",
        result["resolutions"],
    )
    registry = portraits.identity_authority_registry(
        Bible(
            world=World(visual_style_canonical="国风"),
            characters=[Character(
                name="苍玄",
                role="重要配角",
                appearance_canonical="白发老者，道袍简洁，神情威严",
            )],
        ),
        persisted,
    )

    assert result["errors"] == []
    assert card_calls == ["苍玄"]
    assert {item["authority_id"] for item in registry} == {"bible:苍玄"}


def test_materialized_bible_alias_k_never_upgrades_manual_authority(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, project_id TEXT, "
        "episode_no INTEGER, screenplay_character_resolutions TEXT NOT NULL)"
    )
    manual = {
        "source_label": "师尊",
        "canonical_name": "苍玄",
        "resolution": "reference_identity",
        "identity_group": "manual:master",
        "authority_id": "manual:cangxuan",
        "decision_provenance": "manual",
    }
    conn.execute(
        "INSERT INTO episodes VALUES('e1','p1',1,?)",
        (json.dumps([manual], ensure_ascii=False),),
    )
    conn.commit()
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "师尊",
                "canonical_name": "苍玄",
                "identity_kind": "named",
                "kind": "mentioned",
                "evidence": "师尊",
            }],
            messages=messages,
        ), ensure_ascii=False)

    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="苍玄",
            role="重要配角",
            appearance_canonical="白发老者，道袍简洁，神情威严",
        )],
    )
    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(
        portraits,
        "_future_chapter_context",
        lambda *_args, **_kwargs: ("", ""),
    )
    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1",
        1,
        "本章再次提到师尊的戒律。",
        bible,
        generate_portraits=False,
    ))
    assert calls == 1
    assert result["errors"] == [
        "named authority 不可直接物化人物卡：师尊->苍玄"
    ]
    assert result["candidates"][0]["authority_id"] == "manual:cangxuan"
    assert result["resolutions"] == []
    stored = json.loads(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='e1'"
    ).fetchone()[0])
    assert stored == [manual]


def test_materialization_compatibility_flag_cannot_override_non_bible_authority(
) -> None:
    assert portraits._named_candidate_materialization_compatible({
        "name": "苍玄",
        "authority_id": "manual:cangxuan",
        "identity_group": "manual:master",
        "materialization_compatible": True,
    }) is False


def test_future_known_decision_overwrites_stale_materialization_compatibility(
    monkeypatch,
) -> None:
    """Future K must retain the signed origin-group verdict, not an old F flag."""

    async def fake_chat(messages, **kwargs):
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "老者",
                "canonical_name": "苍玄",
                "identity_kind": "named",
                "kind": "onscreen",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [
            {
                "name": "苍玄",
                "source_label": "师尊",
                "identity_kind": "named",
                "identity_group": "manual:master",
                "authority_id": "bible:苍玄",
                "decision_provenance": "manual",
                "kind": "mentioned",
            },
            {
                "name": "老者",
                "source_label": "老者",
                "identity_kind": "functional",
                "identity_group": "current-1:F1",
                "kind": "onscreen",
                # This optimistic current-stage value must not survive a K
                # decision signed from an incompatible manual origin group.
                "materialization_compatible": True,
            },
        ],
        source_text="老者出现在山门前。",
        future_text="老者摘下面具，弟子认出他就是师尊。",
        bible=Bible(
            world=World(visual_style_canonical="国风"),
            characters=[],
        ),
        episode_no=1,
    ))

    elder = next(item for item in resolved if item["source_label"] == "老者")
    assert elder["name"] == "苍玄"
    assert elder["authority_id"] == "bible:苍玄"
    assert elder["materialization_compatible"] is False
    assert portraits._named_candidate_materialization_compatible(elder) is False


@pytest.mark.parametrize(
    ("mutation", "error_fragment"),
    [
        ("forged_k", "K decision 越界"),
        ("sentinel", "K decision 越界"),
        ("reserved_n", "必须选择 K decision"),
        ("cross_f", None),
    ],
)
def test_current_identity_rf11_custom_exact_gates(
    mutation: str,
    error_fragment: str | None,
) -> None:
    records = portraits._current_identity_evidence_records(
        "耳根站在山门。\n\n门卫守在殿前。"
    )
    evidence_by_ref = {
        f"E{index:03d}": record
        for index, record in enumerate(records, start=1)
    }
    authorities = portraits.identity_authority_registry(
        Bible(
            world=World(visual_style_canonical="国风"),
            characters=[Character(
                name="耳根",
                role="已登记身份",
                appearance_canonical="中年男子，素色长衫，五官清晰",
            )],
        ),
        [],
    )
    known = portraits._current_identity_known_decision_catalog(
        evidence_by_ref,
        authorities=authorities,
    )
    selected = next(iter(known.values()))
    payload = {"k": [], "n": [], "f": []}
    refs = list(evidence_by_ref)
    if mutation == "forged_k":
        payload["k"] = [{
            "decision_id": "K:forged",
            "kind": "mentioned",
        }]
    elif mutation == "sentinel":
        payload["k"] = [{
            "decision_id": "K:NONE",
            "kind": "mentioned",
        }]
    elif mutation == "reserved_n":
        payload["n"] = [{
            "evidence_ref": selected["evidence_ref"],
            "identity_label": "耳根",
            "name_kind": "personal_name",
            "kind": "onscreen",
        }]
    elif mutation == "cross_f":
        guard_ref = next(
            ref for ref, record in evidence_by_ref.items()
            if "门卫" in str(record["text"])
        )
        other_ref = next(ref for ref in refs if ref != guard_ref)
        payload["f"] = [{
            "evidence_ref": other_ref,
            "source_label": "门卫",
            "functional_identity_key": "F1",
            "kind": "onscreen",
        }]
    response = portraits.CurrentIdentityCandidateResponse.model_validate(
        payload
    )
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known,
        reserved_authority_labels={"耳根"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    if error_fragment is None:
        # A functional label that is not literal in its own owned evidence is a
        # legitimate synthetic observation (prompt rule 4), never a hard failure
        # just because the phrase happens to appear verbatim elsewhere.
        assert errors == []
        assert len(projected) == 1
        assert projected[0]["identity_kind"] == "functional"
        assert projected[0]["source_label"] == "门卫"
        assert (
            projected[0]["source_label_provenance"]
            == portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        )
        assert projected[0]["identity_group"].startswith("current-1:synthetic:")
    else:
        assert any(error_fragment in error for error in errors)


# ---------------------------------------------------------------------------
# 第35轮真实回归 ERR-20260824-bc3d14（EP10，李富贵）：模型在同一次响应里
# 既在 k 数组正确签发了 (source_label="李富贵", evidence_ref) 的合规决议，
# 又在 n 数组重复申报同一 (identity_label, evidence_ref) 当作「新」具名声明
# ——冗余回显，不是未经核验的具名注入。修前：append_candidate 的 n 循环从不
# 传 known_authority，凡命中 reserved_authority_labels 且逐字出现的 n 条目
# 一律被当成未授权申报硬拒，即便同一响应的 k 里已经有权威决议覆盖它。
# 修复：仅当 (source_label, evidence_ref) 复合键在 k 里有对应合规决议时才
# 静默丢弃该 n 条目；否则维持硬失败（防止模型夹带未经核验的具名声明）。
# ---------------------------------------------------------------------------

def test_current_identity_redundant_n_echo_dropped_when_k_covers_same_ref_ep10_regression() -> None:
    """用例 A（复现 EP10）：k 含李富贵@E001 的合规决议，n 含同 label 同 ref
    的重复条目——修前硬失败「current 已登记身份必须选择 K decision」，修后
    必须通过：n 条目被静默丢弃（不采信其中任何声明），身份以 K 决议为准，
    并在 K 决议对应的候选上留下可观测丢弃标记。"""
    evidence_by_ref = {"E001": {"text": "李富贵忽然开口说话。"}}
    decision_id = "K:E001:redundant-echo-fixture"
    known_decisions = {
        decision_id: {
            "evidence_ref": "E001",
            "source_label": "李富贵",
            "canonical_name": "李富贵",
            "authority_id": "bible:李富贵",
            "decision_type": "registered_authority",
            "materialization_compatible": True,
        },
    }
    payload = {
        "k": [{"decision_id": decision_id, "kind": "mentioned"}],
        "n": [{
            "evidence_ref": "E001",
            "identity_label": "李富贵",
            "name_kind": "personal_name",
            "kind": "mentioned",
        }],
        "f": [],
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known_decisions,
        reserved_authority_labels={"李富贵"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    # 只有 k 那条决议产出候选；n 的冗余回显被丢弃，不额外产出或覆盖任何字段。
    assert len(projected) == 1
    assert projected[0]["identity_kind"] == "named"
    assert projected[0]["name"] == "李富贵"
    assert projected[0]["source_label"] == "李富贵"
    assert projected[0]["_current_identity_redundant_n_echo_dropped"] is True


def test_current_identity_reserved_n_without_matching_k_still_hard_fails() -> None:
    """用例 B（闸门不许放松）：n 含 reserved label 且逐字出现在证据里，但
    本响应的 k 数组里根本没有任何决议——冗余回显豁免不适用，必须继续硬失败。
    这是防止模型偷偷注入未经核验具名声明的必要闸门，第35轮修复只处理"k 已
    签发"的冗余回显场景，不放松这条无 k 兜底的路径。"""
    evidence_by_ref = {"E001": {"text": "李富贵忽然开口说话。"}}
    payload = {
        "k": [],
        "n": [{
            "evidence_ref": "E001",
            "identity_label": "李富贵",
            "name_kind": "personal_name",
            "kind": "mentioned",
        }],
        "f": [],
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels={"李富贵"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("必须选择 K decision" in message for message in errors)


def test_current_identity_reserved_n_echo_different_evidence_ref_still_hard_fails() -> None:
    """用例 C（从严判断，见 RCA 追加结论）：n 命中的 reserved label 逐字出现
    在证据里，且本响应 k 数组里确实有同一 source_label 的合规决议——但那条
    K 决议锚定的是另一个 evidence_ref（E001），n 引用的是 E002。冗余回显
    豁免要求 (source_label, evidence_ref) 复合键完全一致：K 决议没有覆盖
    n 这条声明具体引用的证据，放行等于允许模型对着一条 K 未验证过的证据
    夹带具名声明，必须继续硬失败。"""
    evidence_by_ref = {
        "E001": {"text": "李富贵在院子里说话。"},
        "E002": {"text": "李富贵又在屋里喊了一声。"},
    }
    decision_id = "K:E001:redundant-echo-fixture"
    known_decisions = {
        decision_id: {
            "evidence_ref": "E001",
            "source_label": "李富贵",
            "canonical_name": "李富贵",
            "authority_id": "bible:李富贵",
            "decision_type": "registered_authority",
            "materialization_compatible": True,
        },
    }
    payload = {
        "k": [{"decision_id": decision_id, "kind": "mentioned"}],
        "n": [{
            "evidence_ref": "E002",
            "identity_label": "李富贵",
            "name_kind": "personal_name",
            "kind": "mentioned",
        }],
        "f": [],
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known_decisions,
        reserved_authority_labels={"李富贵"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("必须选择 K decision" in message for message in errors)


@pytest.mark.parametrize("failure", ["attempt15_rf9", "unknown_evidence"])
def test_current_identity_rf11_rejects_unbound_provider_output_once(
    monkeypatch,
    failure: str,
) -> None:
    calls = 0
    downstream: list[str] = []

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "attempt15_rf9":
            return json.dumps({
                "named": [{
                    "source_label": "许师姐",
                    "canonical_name": "许清",
                    "identity_kind": "named",
                    "kind": "onscreen",
                    "evidence_id": "CE:attempt15-wrong-alias",
                }],
                "functional": [{
                    "source_label": "耳根",
                    "identity_kind": "functional",
                    "functional_identity_key": "F1",
                    "kind": "mentioned",
                    "evidence_id": "CE:attempt15-reserved",
                }],
            }, ensure_ascii=False)
        payload = _identity_wire_for_call(
            kwargs,
            [{
                "source_label": "门卫",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence": "门卫",
            }],
            messages=messages,
        )
        assert len(payload["f"]) == 1
        payload["f"][0]["evidence_ref"] = "E999"
        return json.dumps(payload, ensure_ascii=False)

    async def forbidden_future(*_args, **_kwargs):
        downstream.append("future")
        raise AssertionError("invalid current wire reached future")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        portraits,
        "resolve_future_identity_candidates",
        forbidden_future,
    )
    expected_error = (
        model_gateway.StructuredFormatError
        if failure == "attempt15_rf9"
        else model_gateway.StructuredSemanticError
    )
    with pytest.raises(expected_error):
        asyncio.run(portraits.discover_character_candidates(
            "门卫守在山门。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))
    assert calls == 1
    assert downstream == []


def test_current_identity_rf11_unsupported_schema_is_one_call(
    monkeypatch,
) -> None:
    calls = 0
    original = hiagent.ProviderError(
        "strict response_format unsupported",
        retryable=False,
        failure_kind="response_format_unsupported",
    )

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        assert kwargs["call_meta"]["response_format_required"] is True
        assert kwargs["call_meta"]["disable_provider_retries"] is True
        assert kwargs["call_meta"][
            "disable_provider_candidate_fallback"
        ] is True
        assert kwargs["call_meta"]["disable_reasoning_fallback"] is True
        raise original

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(hiagent.ProviderError) as caught:
        asyncio.run(portraits.discover_character_candidates(
            "门卫守在山门。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))

    assert caught.value is original
    assert calls == 1


def test_current_identity_empty_owned_catalog_skips_provider(monkeypatch) -> None:
    async def forbidden(*_args, **_kwargs):
        raise AssertionError("empty current evidence catalog must skip provider")

    monkeypatch.setattr(model_gateway, "chat", forbidden)
    assert asyncio.run(portraits.discover_character_candidates(
        "",
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        1,
    )) == []


def test_current_identity_same_label_multiple_groups_backfills_end_to_end(
    monkeypatch,
) -> None:
    """第31轮 ERR-20260824-614276 方向变更（真实 EP5"老者"F3/F4）：这条测试
    曾经断言"同一裸标签、不同 F 键必须致命"，现在反过来——模型已经用不同
    functional_identity_key 结构性区分了两个人，只是没填 scope_qualifier，
    应当被确定性补足而不是拒绝重来。全链路（discover_character_candidates，
    不是单测 _project_current_identity_response）验证：RCA 过程中额外发现
    _discover_character_candidates_legacy 内部还有第三处按裸 source_label
    折叠的"resolved"聚合步骤（跨 collect() 批次做最终合并），一直没跟进
    round-20/round-31 的 (source_label, scope_qualifier) 复合键升级，会把
    已经成功补足限定语的两个人重新拍扁成一个——这条端到端测试正是为了
    盯住这第三处，不能只在单元测试层面验证 _project_current_identity_
    response 自己返回了两条就算数。"""
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        payload = _identity_wire_for_call(
            kwargs,
            [
                {
                    "source_label": "绿袍修士",
                    "identity_kind": "functional",
                    "functional_identity_key": "F1",
                    "kind": "onscreen",
                    "evidence": "绿袍修士",
                },
                {
                    "source_label": "绿袍修士",
                    "identity_kind": "functional",
                    "functional_identity_key": "F2",
                    "kind": "onscreen",
                    "evidence": "绿袍修士",
                },
            ],
            messages=messages,
        )
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.discover_character_candidates(
        "两名绿袍修士同时开口。",
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        1,
    ))
    assert calls == 1
    matches = sorted(
        (item for item in result if item["source_label"] == "绿袍修士"),
        key=lambda item: item["identity_group"],
    )
    assert len(matches) == 2
    assert [item["scope_qualifier"] for item in matches] == ["甲", "乙"]
    assert all(
        item.get("_current_identity_synthesized_qualifier") is True
        for item in matches
    )
    assert len({item["identity_group"] for item in matches}) == 2


def test_current_identity_named_requires_literal_selected_evidence(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "面色苍白的女子",
                "canonical_name": "面色苍白的女子",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "一个面色苍白",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="缺少逐字 owned evidence",
    ):
        asyncio.run(portraits.discover_character_candidates(
            "一个面色苍白，看不出年纪的女子走来。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))
    assert calls == 1


def test_current_identity_literal_label_isolated_as_synthetic_once(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        prompt = str(messages[0].get("content") or "")
        marker = (
            "backend-owned 当前身份证据目录。E ref 已绑定完整证据 receipt，"
            "禁止跨 E 搬运人物："
        )
        raw_catalog = prompt.split(marker, 1)[1].split("\n", 1)[1]
        raw_catalog = raw_catalog.split(
            "\n\n本批已登记身份 K 决议目录", 1
        )[0]
        catalog = json.loads(raw_catalog)
        unrelated_ref = next(
            str(item["evidence_ref"])
            for item in catalog
            if "银袍女子" in str(item["text"])
        )
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "门卫",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence_ref": unrelated_ref,
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    # "门卫" is not literal in the receipt it was bound to, so it is a legitimate
    # synthetic observation (prompt rule 4). It must be isolated as synthetic,
    # not hard-failed just because "门卫" appears verbatim in another owned
    # receipt. Only the single current provider round-trip is allowed; the
    # synthetic label must not trigger any future/authority provider call.
    candidates = asyncio.run(portraits.discover_character_candidates(
        "门卫守在山门。\n\n银袍女子站在殿前。",
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        1,
    ))

    assert calls == 1
    assert len(candidates) == 1
    assert candidates[0]["source_label"] == "门卫"
    assert candidates[0]["identity_kind"] == "functional"
    assert candidates[0]["source_label_provenance"] == (
        portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
    )
    assert candidates[0]["identity_group"].startswith("current-1:synthetic:")
    assert "银袍女子" in candidates[0]["source_quote"]


@pytest.mark.parametrize(
    ("source_text", "source_label", "evidence_hint", "bible", "resolutions"),
    [
        (
            "孟浩守在山门。",
            "孟浩",
            "孟浩守在山门",
            Bible(
                world=World(visual_style_canonical="国风"),
                characters=[Character(
                    name="孟浩",
                    role="主角",
                    appearance_canonical="黑发青年，青色长衫，目光坚定",
                )],
            ),
            [],
        ),
        (
            "银袍女子守在山门。",
            "孟浩",
            "银袍女子守在山门",
            Bible(
                world=World(visual_style_canonical="国风"),
                characters=[Character(
                    name="孟浩",
                    role="主角",
                    appearance_canonical="黑发青年，青色长衫，目光坚定",
                )],
            ),
            [],
        ),
        (
            "孟兄守在山门。",
            "孟兄",
            "孟兄守在山门",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            [{
                "source_label": "孟兄",
                "canonical_name": "孟浩",
                "resolution": "reference_identity",
                "identity_group": "reference:menghao",
                "authority_id": "bible:孟浩",
                "decision_provenance": "manual",
            }],
        ),
    ],
)
def test_current_functional_cannot_claim_reserved_authority_label_once(
    monkeypatch,
    source_text: str,
    source_label: str,
    evidence_hint: str,
    bible: Bible,
    resolutions: list[dict],
) -> None:
    calls = 0
    downstream: list[str] = []

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": source_label,
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence": evidence_hint,
            }],
            messages=messages,
        ), ensure_ascii=False)

    async def forbidden_future(*_args, **_kwargs):
        downstream.append("future")
        raise AssertionError("reserved functional result reached future")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        portraits,
        "resolve_future_identity_candidates",
        forbidden_future,
    )
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="functional 不得冒用已登记身份称谓",
    ):
        asyncio.run(portraits.discover_character_candidates(
            source_text,
            bible,
            1,
            existing_resolutions=resolutions,
        ))

    assert calls == 1
    assert downstream == []


def test_current_identity_cross_batch_literal_uses_global_catalog() -> None:
    records = portraits._current_identity_evidence_records(
        "门卫守在山门。\n\n银袍女子站在殿前。"
    )
    unrelated_record = next(
        item for item in records if "银袍女子" in str(item["text"])
    )
    response = portraits.CurrentIdentityCandidateResponse.model_validate({
        "k": [],
        "n": [],
        "f": [{
            "evidence_ref": "E001",
            "source_label": "门卫",
            "functional_identity_key": "F1",
            "kind": "onscreen",
        }],
    })

    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref={"E001": unrelated_record},
        known_decisions={},
        group_scope="current-2",
        existing_functional_routes=set(),
    )

    # The provider bound "门卫" to evidence that does not contain it verbatim.
    # That makes it a synthetic observation for this batch; the fact that "门卫"
    # appears literally in a different batch's receipt must not turn a legitimate
    # synthetic label into a hard failure. Synthetic identities are structurally
    # excluded from becoming authorities downstream, so no cross-batch conflict.
    assert errors == []
    assert len(projected) == 1
    assert projected[0]["source_label"] == "门卫"
    assert projected[0]["identity_kind"] == "functional"
    assert (
        projected[0]["source_label_provenance"]
        == portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
    )
    assert projected[0]["identity_group"].startswith("current-2:synthetic:")


@pytest.mark.parametrize("identity_kind", ["functional", "named"])
@pytest.mark.parametrize(
    ("first_kind", "second_kind"),
    [("mentioned", "onscreen"), ("onscreen", "mentioned")],
)
def test_current_identity_cross_batch_prior_reuse_keeps_onscreen_receipt(
    monkeypatch,
    identity_kind: str,
    first_kind: str,
    second_kind: str,
) -> None:
    source_text = "守卫在山门外被提及。\n\n守卫冲上前拦路。"
    records = portraits._current_identity_evidence_records(source_text)
    assert len(records) == 2
    monkeypatch.setattr(
        portraits,
        "_current_identity_evidence_batches",
        lambda *_args, **_kwargs: [[records[0]], [records[1]]],
    )
    calls: list[dict] = []

    async def fake_chat(messages, **kwargs):
        index = len(calls)
        calls.append(dict(kwargs["call_meta"]))
        item = {
            "source_label": "守卫",
            "identity_kind": identity_kind,
            "kind": first_kind if index == 0 else second_kind,
            "evidence": "守卫",
        }
        if identity_kind == "functional":
            item["functional_identity_key"] = "F1"
            item["reuse_prior"] = index == 1
        else:
            item["canonical_name"] = "守卫"
        return json.dumps(
            _identity_wire_for_call(kwargs, [item], messages=messages),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.extract_current_identity_candidates(
        source_text,
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        1,
    ))

    assert len(calls) == 2
    assert calls[1]["prior_decision_catalog_hash"] != calls[0][
        "prior_decision_catalog_hash"
    ]
    assert len(result) == 1
    assert result[0]["kind"] == "onscreen"
    assert result[0]["source_evidence_receipt"]["text"] == records[0]["text"]
    assert [
        receipt["evidence_id"]
        for receipt in result[0]["source_evidence_receipts"]
    ] == [record["evidence_id"] for record in records]
    assert result[0]["source_segment_ids"] == [
        record["source_segment_id"] for record in records
    ]
    assert result[0]["evidence"] in result[0]["source_quote"]


def test_current_identity_cross_batch_alias_explicitly_reuses_prior_group(
    monkeypatch,
) -> None:
    source_text = "王伯在铺子里忙碌。\n\n王老伯后来关上了铺门。"
    records = portraits._current_identity_evidence_records(source_text)
    assert len(records) == 2
    monkeypatch.setattr(
        portraits,
        "_current_identity_evidence_batches",
        lambda *_args, **_kwargs: [[records[0]], [records[1]]],
    )
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        item = {
            "source_label": "王伯" if calls == 1 else "王老伯",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "onscreen",
            "evidence": "王伯" if calls == 1 else "王老伯",
            "reuse_prior": calls == 2,
            "prior_source_label": "王伯",
        }
        return json.dumps(
            _identity_wire_for_call(kwargs, [item], messages=messages),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.extract_current_identity_candidates(
        source_text,
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        1,
    ))

    assert calls == 2
    assert {item["source_label"] for item in result} == {"王伯", "王老伯"}
    assert len({item["identity_group"] for item in result}) == 1
    assert len({
        item["source_evidence_receipt"]["evidence_id"] for item in result
    }) == 2


def test_current_identity_cross_batch_same_label_new_group_fails_once(
    monkeypatch,
) -> None:
    source_text = "守卫在山门外。\n\n守卫又走到殿前。"
    records = portraits._current_identity_evidence_records(source_text)
    monkeypatch.setattr(
        portraits,
        "_current_identity_evidence_batches",
        lambda *_args, **_kwargs: [[records[0]], [records[1]]],
    )
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "守卫",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence": "守卫",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="必须用P token显式复用",
    ):
        asyncio.run(portraits.extract_current_identity_candidates(
            source_text,
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))
    assert calls == 2


def test_current_identity_cross_batch_registered_alias_uses_only_k(
    monkeypatch,
) -> None:
    source_text = "本章提到师尊的戒律。\n\n弟子再次谈到师尊。"
    records = portraits._current_identity_evidence_records(source_text)
    monkeypatch.setattr(
        portraits,
        "_current_identity_evidence_batches",
        lambda *_args, **_kwargs: [[records[0]], [records[1]]],
    )
    prompts: list[str] = []

    async def fake_chat(messages, **kwargs):
        prompts.append(messages[0]["content"])
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "师尊",
                "canonical_name": "苍玄",
                "identity_kind": "named",
                "kind": "mentioned",
                "evidence": "师尊",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.extract_current_identity_candidates(
        source_text,
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        1,
        existing_resolutions=[{
            "source_label": "师尊",
            "canonical_name": "苍玄",
            "resolution": "reference_identity",
            "identity_group": "manual:master",
            "authority_id": "manual:cangxuan",
            "decision_provenance": "manual",
        }],
    ))

    assert len(prompts) == 2
    assert '"decision_type":"prior_named"' not in prompts[1]
    assert '"decision_type":"registered_authority"' in prompts[1]
    assert len(result) == 1
    assert result[0]["authority_id"] == "manual:cangxuan"
    assert result[0]["kind"] == "mentioned"


def test_current_synthetic_functional_never_enters_future_authority(
    monkeypatch,
) -> None:
    candidate = {
        "name": "绿袍修士一",
        "source_label": "绿袍修士一",
        "identity_kind": "functional",
        "identity_group": "current-1:synthetic:one",
        "kind": "onscreen",
        "source_label_provenance": (
            portraits.CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
        ),
    }

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("synthetic current label reached future provider")

    monkeypatch.setattr(model_gateway, "chat", forbidden)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [candidate],
        source_text="有两个穿绿袍的男子。",
        future_text="绿袍修士一后来自称赵某。",
        bible=Bible(
            world=World(visual_style_canonical="国风"),
            characters=[],
        ),
        episode_no=1,
    ))
    assert resolved == [candidate]


def test_future_context_prioritizes_late_known_name_cooccurrence() -> None:
    future_text = (
        ("小胖子继续砍柴，没有报出姓名。" * 120)
        + "小胖子拍着胸口说，我李富贵认你这个朋友。"
    )

    context = portraits._future_identity_context(
        future_text,
        ["小胖子"],
        known_names=["李富贵"],
        current_text="白净胖少年被带上山。",
    )

    assert "我李富贵" in context
    assert "人物谱真名：李富贵" in context


def test_future_canonical_cooccurrence_does_not_upgrade_alias_group(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="许清",
            role="重要配角",
            appearance_canonical="银袍女子，面色苍白，黑发冷眸",
        )],
    )
    calls = 0

    async def fake_chat(*_args, **kwargs):
        nonlocal calls
        calls += 1
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps(_identity_wire_for_call(kwargs, [
                {
                    "source_label": "会飞的女人",
                    "canonical_name": "",
                    "identity_kind": "functional",
                    "functional_identity_key": "F1",
                    "kind": "onscreen",
                    "evidence": "银袍女子将众人卷走",
                },
                {
                    "source_label": "许师姐",
                    "canonical_name": "",
                    "identity_kind": "functional",
                    "functional_identity_key": "F1",
                    "kind": "mentioned",
                    "evidence": "同一女子被称为许师姐",
                },
            ], messages=_args[0]), ensure_ascii=False)
        return json.dumps(_identity_wire_for_call(kwargs, [
            {
                "source_label": "会飞的女人",
                "identity_kind": "functional",
            },
            {
                "source_label": "许师姐",
                "identity_kind": "functional",
            },
        ]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "会飞的女人出现，绿袍修士称她为许师姐。",
        bible,
        1,
        future_text="许师姐转身，众人称她许清。",
        future_label="后续章节",
    ))

    assert calls == 2
    assert {
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    } == {
        ("会飞的女人", "会飞的女人", "functional"),
        ("许师姐", "许师姐", "functional"),
    }


def test_future_identity_projects_exact_who_tokens_without_substring_mutation() -> None:
    script = EpisodeScreenplay(
        episode_no=5,
        plot_spine=PlotSpine(
            episode_premise="阿宾与卢美的关系发生变化。",
            spine_beats=[
                PlotSpineBeat(
                    beat_id="S01",
                    who="美",
                    does="确认自己的决定",
                    turn="关系发生变化",
                ),
                PlotSpineBeat(
                    beat_id="S02",
                    who="阿宾、卢美",
                    does="继续交谈",
                    turn="双方达成共识",
                ),
                PlotSpineBeat(
                    beat_id="S03",
                    who="阿宾、小美",
                    does="保留另一个完整身份 token",
                    turn="身份不会被误改",
                ),
            ],
            must_keep_ending="卢美完成本集关系变化。",
        ),
    )
    resolutions = [{
        "source_label": "美",
        "canonical_name": "卢美",
        "resolution": "future_identity",
        "authority_id": "bible:卢美",
    }]

    for _ in range(3):
        portraits.apply_screenplay_character_resolutions(script, resolutions)

    assert [beat.who for beat in script.plot_spine.spine_beats] == [
        "卢美",
        "阿宾、卢美",
        "阿宾、小美",
    ]


def test_future_identity_repairs_legacy_expansion_and_blocks_it_before_publish() -> None:
    script = EpisodeScreenplay(
        episode_no=6,
        plot_spine=PlotSpine(
            spine_beats=[
                PlotSpineBeat(
                    beat_id="S01",
                    who="阿宾、卢卢美、何何钰慧",
                    does="进入下一场事件",
                    turn="局势发生变化",
                ),
            ],
        ),
    )
    resolutions = [
        {
            "source_label": "美",
            "canonical_name": "卢美",
            "resolution": "future_identity",
            "authority_id": "bible:卢美",
        },
        {
            "source_label": "钰慧",
            "canonical_name": "何钰慧",
            "resolution": "future_identity",
            "authority_id": "bible:何钰慧",
        },
    ]

    errors = portraits.screenplay_character_resolution_errors(
        script,
        resolutions,
    )
    assert any("plot_spine.spine_beats[0].who[卢卢美]" in error for error in errors)
    assert any("plot_spine.spine_beats[0].who[何何钰慧]" in error for error in errors)

    portraits.apply_screenplay_character_resolutions(script, resolutions)

    assert script.plot_spine.spine_beats[0].who == "阿宾、卢美、何钰慧"
    assert portraits.screenplay_character_resolution_errors(script, resolutions) == []
    assert portraits.screenplay_unknown_identity_errors(
        script,
        Bible(
            world=World(visual_style_canonical="写实"),
            characters=[
                Character(name="阿宾", role="主角", appearance_canonical="青年"),
                Character(name="卢美", role="配角", appearance_canonical="女性"),
                Character(name="何钰慧", role="配角", appearance_canonical="女性"),
            ],
        ),
        resolutions,
    ) == []


def test_structural_audit_keeps_unregistered_descriptor_functional(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name="孟浩",
                role="主角",
                appearance_canonical="黑发书生，蓝色长衫，手持葫芦",
            ),
            Character(
                name="李富贵",
                role="重要配角",
                appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
            ),
        ],
    )

    phases: list[str] = []

    async def fake_chat(messages, **kwargs):
        phases.append(kwargs["call_meta"]["discovery_phase"])
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps(_identity_wire_for_call(kwargs, [{
                "source_label": "孟浩",
                "canonical_name": "孟浩",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence": "当前主角",
            }], messages=messages), ensure_ascii=False)
        assert "白白净净身较胖" in messages[0]["content"]
        assert "SRC0001" in messages[0]["content"]
        assert "我李富贵" not in messages[0]["content"]
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "白白净净身较胖",
                "identity_kind": "functional",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "孟浩身边的李富贵长得白白净净身较胖。",
        bible,
        1,
        future_text="小胖子跟随孟浩。后来小胖子说：我李富贵认你这个朋友。",
        future_label="后续章节",
        structural_evidence=[{
            "identity_key": "白白净净身较胖",
            "source_segment_ids": ["SRC0001"],
            "usage": "visible",
        }],
    ))

    assert any(
        item["source_label"] == "白白净净身较胖"
        and item["name"] == "白白净净身较胖"
        and item["identity_kind"] == "functional"
        for item in candidates
    )
    assert phases == ["current", "coverage"]


def test_identity_discovery_does_not_run_fixed_coverage_without_structural_evidence(
    monkeypatch,
) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[],
    )
    phases: list[str] = []

    async def fake_chat(_messages, **kwargs):
        phases.append(kwargs["call_meta"]["discovery_phase"])
        return json.dumps(_identity_wire_for_call(kwargs, []), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    assert asyncio.run(portraits.discover_character_candidates(
        "孟浩身边另一个则是白白净净身较胖。",
        bible,
        1,
    )) == []
    assert phases == ["current"]


def test_stable_unique_title_is_accepted_as_named_identity(monkeypatch) -> None:
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="国风"),
    )

    async def fake_chat(*_args, **kwargs):
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps(_identity_wire_for_call(kwargs, [{
                "source_label": "靠山老祖",
                "canonical_name": "",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "mentioned",
                "evidence": "本集提到建立宗门的老祖",
            }]), ensure_ascii=False)
        return json.dumps(_identity_wire_for_call(kwargs, [{
            "source_label": "靠山老祖",
            "canonical_name": "靠山老祖",
            "identity_kind": "named",
            "kind": "mentioned",
            "evidence": "跨章节唯一指向建立宗门的同一位老祖",
            "future_evidence": "靠山老祖定下门规",
        }], messages=_args[0]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "靠山老祖建立宗门，靠山老祖后来失踪。",
        bible,
        2,
        future_text="靠山老祖定下门规，靠山老祖的画像仍在宗门。",
        future_label="后续章节",
    ))

    assert candidates[0]["name"] == "靠山老祖"
    assert candidates[0]["identity_kind"] == "named"


def test_future_functional_relation_label_is_not_promoted_by_text_presence(
    monkeypatch,
) -> None:
    bible = Bible(
        characters=[],
        world=World(visual_style_canonical="都市漫画"),
    )

    phases: list[str] = []

    async def fake_chat(*_args, **kwargs):
        phases.append(kwargs["call_meta"]["discovery_phase"])
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps(_identity_wire_for_call(kwargs, [{
                "source_label": "她男朋友",
                "canonical_name": "",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence": "她男朋友帮忙拎行李",
            }]), ensure_ascii=False)
        return json.dumps(_identity_wire_for_call(kwargs, [{
            "source_label": "她男朋友",
            "canonical_name": "",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "onscreen",
            "evidence": "她男朋友帮忙拎行李",
            "future_evidence": "",
        }]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "她男朋友帮忙拎行李。",
        bible,
        3,
        future_text="后来她男朋友又来了一次，仍未交代姓名。",
        future_label="后续章节",
    ))

    assert [
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    ] == [("她男朋友", "她男朋友", "functional")]
    assert phases == ["current", "future_identity"]


def test_future_functional_does_not_bind_existing_bible_by_cooccurrence(
    monkeypatch,
) -> None:
    bible = Bible(
        characters=[Character(
            name="李富贵",
            role="重要配角",
            appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
        )],
        world=World(visual_style_canonical="国风"),
    )

    async def fake_chat(*_args, **kwargs):
        if kwargs["call_meta"]["discovery_phase"] == "current":
            return json.dumps(_identity_wire_for_call(kwargs, [{
                "source_label": "小胖子",
                "canonical_name": "",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "onscreen",
                "evidence": "小胖子跟随孟浩",
            }]), ensure_ascii=False)
        return json.dumps(_identity_wire_for_call(kwargs, [{
            "source_label": "小胖子",
            "identity_kind": "functional",
            "functional_identity_key": "F1",
            "kind": "onscreen",
            "evidence": "小胖子跟随孟浩",
            "future_evidence": "小胖子拍着胸口说，我李富贵认你这个朋友。",
        }]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "小胖子跟随孟浩。",
        bible,
        2,
        future_text="小胖子拍着胸口说，我李富贵认你这个朋友。",
        future_label="后续章节",
    ))

    assert [
        (item["source_label"], item["name"], item["identity_kind"])
        for item in candidates
    ] == [("小胖子", "小胖子", "functional")]


def test_character_resolutions_persist_and_future_identity_upgrades_route_fallback() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO episodes(id) VALUES('e1')")
    first = portraits.persist_screenplay_character_resolutions(conn, "e1", [{
        "source_label": "青衣人",
        "canonical_name": "路人甲",
        "resolution": "functional_extra",
    }])
    upgraded = portraits.persist_screenplay_character_resolutions(conn, "e1", [{
        "source_label": "青衣人",
        "canonical_name": "丁力",
        "resolution": "future_identity",
    }])

    assert first[0]["canonical_name"] == "路人甲"
    assert upgraded[0]["canonical_name"] == "丁力"
    assert portraits.load_screenplay_character_resolutions(conn, "e1") == upgraded


def test_persist_is_stable_when_semantic_identity_set_is_unchanged() -> None:
    """A re-discovery that reproduces the same semantic decisions must not
    rewrite the stored rows (which would churn screenplay_authority_fingerprint
    and strand the retry grant on a superseded revision)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, "
        "screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO episodes(id) VALUES('e1')")
    portraits.persist_screenplay_character_resolutions(conn, "e1", [{
        "source_label": "门卫",
        "canonical_name": "门卫",
        "resolution": "functional_identity",
        "reason": "首版理由文本",
        "evidence": "首版证据文本",
    }])
    stored_before = str(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='e1'"
    ).fetchone()[0])

    # Same semantic decision, but the model re-authored the free-text fields.
    portraits.persist_screenplay_character_resolutions(conn, "e1", [{
        "source_label": "门卫",
        "canonical_name": "门卫",
        "resolution": "functional_identity",
        "reason": "重跑理由文本完全不同",
        "evidence": "重跑证据文本完全不同",
    }])
    stored_after = str(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='e1'"
    ).fetchone()[0])

    # Stored bytes are unchanged -> authority fingerprint stays stable.
    assert stored_after == stored_before

    # A genuine semantic change (new resolution) still persists.
    changed = portraits.persist_screenplay_character_resolutions(conn, "e1", [{
        "source_label": "青衣人",
        "canonical_name": "丁力",
        "resolution": "future_identity",
    }])
    assert any(item["source_label"] == "青衣人" for item in changed)


def test_discovery_persistence_retires_only_legacy_future_identity_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]')"
    )
    conn.execute("INSERT INTO episodes(id) VALUES('e1')")
    portraits.persist_screenplay_character_resolutions(conn, "e1", [
        {
            "source_label": "旧称谓",
            "canonical_name": "旧猜测",
            "resolution": "future_identity",
        },
        {
            "source_label": "门卫",
            "canonical_name": "门卫",
            "resolution": "functional_identity",
        },
    ])

    current = portraits.persist_screenplay_character_resolutions(
        conn,
        "e1",
        [{
            "source_label": "新称谓",
            "canonical_name": "已证实名",
            "resolution": "future_identity",
            "decision_contract_version": portraits.FUTURE_IDENTITY_DECISION_VERSION,
        }],
        retire_legacy_future_identity=True,
    )

    assert {
        (item["source_label"], item["resolution"])
        for item in current
    } == {
        ("门卫", "functional_identity"),
        ("新称谓", "future_identity"),
    }


def test_character_resolution_merge_preserves_distinct_scoped_authorities() -> None:
    merged = portraits.merge_screenplay_character_resolutions([], [
        {
            "source_label": "穿着绿色长袍的男",
            "canonical_name": "绿袍修士甲",
            "resolution": "functional_identity",
            "authority_id": "functional:green-a",
            "source_instance_key": "functional:green-a",
        },
        {
            "source_label": "穿着绿色长袍的男",
            "canonical_name": "绿袍修士乙",
            "resolution": "functional_identity",
            "authority_id": "functional:green-b",
            "source_instance_key": "functional:green-b",
        },
    ])

    assert [item["authority_id"] for item in merged] == [
        "functional:green-a",
        "functional:green-b",
    ]


def test_character_importance_window_remains_twenty_chapters() -> None:
    conn = _make_conn()
    _seed_project(conn, "美杜莎短暂现身。")
    conn.execute(
        "INSERT INTO chapters(project_id,idx,content) VALUES('p1',50,?)",
        ("美杜莎在二十章窗口边界再次登场。",),
    )
    fragments, label = portraits._forward_fragments(conn, "p1", "美杜莎", 21)

    assert "二十章窗口边界" in fragments
    assert "+20 章" in label


def test_unresolved_descriptive_people_keep_source_labels(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "绿袍男子与大汉守在门前。")
    _patch_settings(monkeypatch, conn)

    async def fake_chat(*_args, **_kwargs):
        return json.dumps(_identity_wire_for_call(_kwargs, [
                {
                    "source_label": "绿袍男子", "canonical_name": "",
                    "identity_kind": "functional", "kind": "onscreen", "evidence": "绿袍男子守门",
                },
                {
                    "source_label": "大汉", "canonical_name": "",
                    "identity_kind": "functional", "kind": "onscreen", "evidence": "大汉守门",
                },
            ]), ensure_ascii=False)

    async def forbidden_ensure(*_args, **_kwargs):
        raise AssertionError("过渡称谓不得建人物卡")

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    monkeypatch.setattr(portraits, "ensure_character_card", forbidden_ensure)
    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    ))

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 21, "绿袍男子与大汉守在门前。", bible,
        generate_portraits=False,
    ))

    assert [
        (
            item["source_label"],
            item["canonical_name"],
            item["resolution"],
        )
        for item in result["resolutions"]
    ] == [
        ("绿袍男子", "绿袍男子", "functional_identity"),
        ("大汉", "大汉", "functional_identity"),
    ]
    assert result["checked"] == 0


def test_mentioned_named_identity_gets_authority_without_character_card(
    monkeypatch,
) -> None:
    conn = _make_conn()
    _seed_project(conn, "卷首落款是靠山老祖。")
    _patch_settings(monkeypatch, conn)

    async def forbidden_ensure(*_args, **_kwargs):
        raise AssertionError("仅内容归属身份不得创建人物卡")

    monkeypatch.setattr(portraits, "ensure_character_card", forbidden_ensure)
    bible = Bible.model_validate(json.loads(
        conn.execute(
            "SELECT bible_json FROM projects WHERE id='p1'"
        ).fetchone()["bible_json"]
    ))
    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1",
        21,
        "卷首落款是靠山老祖。",
        bible,
        generate_portraits=False,
        _precomputed_candidates=[{
            "name": "靠山老祖",
            "source_label": "靠山老祖",
            "identity_kind": "named",
            "identity_group": "current-1:靠山老祖",
            "kind": "mentioned",
            "evidence": "卷首落款明确归属",
        }],
    ))

    assert result["checked"] == 0
    assert result["added"] == []
    assert result["resolutions"][0]["resolution"] == "reference_identity"
    # A mentioned named reference is a named-family decision: it must resolve to
    # the canonical named-authority namespace so the persisted authority_id
    # agrees with the ``named:`` identity_group the discovery projector assigns
    # to the same label.  Minting a ``functional:`` authority for a named group
    # is the self-contradictory row the structural-coverage gate rejects as
    # "identity group 缺少唯一权威".
    assert result["resolutions"][0]["authority_id"] == "bible:靠山老祖"


def test_confirmed_real_name_is_not_downgraded_to_route_extra(monkeypatch) -> None:
    conn = _make_conn()
    _seed_project(conn, "青衣人拦路，后被认出是丁力。")
    _patch_settings(monkeypatch, conn)

    async def fake_candidates(*_args, **_kwargs):
        return [{
            "name": "丁力",
            "source_label": "青衣人",
            "identity_kind": "named",
            "kind": "onscreen",
            "evidence": "青衣人拦路",
            "future_evidence": "被认出是丁力",
        }]

    async def incomplete_card(*_args, **kwargs):
        assert kwargs["require_identity_card"] is True
        return {"status": "skipped_minor", "name": "丁力", "reason": "戏份少"}

    monkeypatch.setattr(portraits, "discover_character_candidates", fake_candidates)
    monkeypatch.setattr(portraits, "ensure_character_card", incomplete_card)
    bible = Bible.model_validate(json.loads(
        conn.execute("SELECT bible_json FROM projects WHERE id='p1'").fetchone()["bible_json"]
    ))

    result = asyncio.run(portraits.ensure_cards_for_text(
        "p1", 21, "青衣人拦路。", bible, generate_portraits=False,
    ))

    assert result["resolutions"] == []
    assert result["errors"] == ["丁力：真名已确认，但人物卡未完成：戏份少"]


def test_baseline_audit_uses_model_to_classify_arbitrary_descriptive_identity(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )

    async def fake_chat(messages, **_kwargs):
        return json.dumps(_identity_wire_for_call(_kwargs, [{
            "source_label": "紫甲女子",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "紫甲女子拦路",
            "future_evidence": "",
        }], messages=messages), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    draft = EpisodeScreenplay(
        episode_no=21,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="触发拦路冲突",
            characters=["萧炎", "紫甲女子"],
            summary="紫甲女子在山门前拦住萧炎，双方的冲突随即升级。",
        )],
    ).model_dump_json()

    candidates = asyncio.run(portraits.discover_character_candidates(
        "萧炎来到山门。", bible, 21, draft_text=draft,
    ))

    assert [(item["source_label"], item["identity_kind"]) for item in candidates] == [
        ("紫甲女子", "functional"),
    ]
    assert candidates[0]["source_label_provenance"] == (
        portraits.CURRENT_IDENTITY_LITERAL_PROVENANCE
    )
    assert candidates[0]["source_evidence_receipt"]["origin"] == (
        "draft_identity_projection"
    )
    assert candidates[0]["source_evidence_receipt"]["source_segment_id"] == (
        "DRF0002"
    )
    assert "紫甲女子" in candidates[0]["source_quote"]


def test_baseline_audit_sends_typed_identity_projection_only(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    prompts: list[str] = []

    async def fake_chat(messages, **_kwargs):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        assert _kwargs["call_meta"]["discovery_phase"] == "current"
        assert "SOURCE_BODY_MARKER" not in prompt
        assert "SCRIPT_ACTION_MARKER" not in prompt
        assert "紫甲女子" in prompt
        return json.dumps(_identity_wire_for_call(_kwargs, [{
            "source_label": "紫甲女子",
            "canonical_name": "",
            "identity_kind": "functional",
            "kind": "onscreen",
            "evidence": "类型合同中的场次人物",
            "future_evidence": "",
        }], messages=messages), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    draft = EpisodeScreenplay(
        episode_no=21,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="SCRIPT_ACTION_MARKER",
            characters=["萧炎", "紫甲女子"],
            summary="SCRIPT_ACTION_MARKER",
        )],
        full_script_text="【场1】日 / 山门\nSCRIPT_ACTION_MARKER",
    ).model_dump_json()

    candidates = asyncio.run(portraits.discover_character_candidates(
        "SOURCE_BODY_MARKER", bible, 21, draft_text=draft,
    ))

    assert len(prompts) == 1
    assert [(item["source_label"], item["identity_kind"]) for item in candidates] == [
        ("紫甲女子", "functional"),
    ]


def test_draft_identity_projection_keeps_structured_annotated_speaker() -> None:
    script = EpisodeScreenplay(
        episode_no=5,
        full_script_text=(
            "【场1】夜 / 室内\n"
            "路人乙（小晶的声音）：我在信里说明经过。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="路人乙（小晶的声音）",
                line="我在信里说明经过。",
                source_text="我在信里说明经过。",
            )],
        )],
    )

    projection = json.loads(
        portraits._draft_identity_projection(script.model_dump_json())
    )
    values = [item["value"] for item in projection["identity_mentions"]]

    assert "路人乙（小晶的声音）" in values
    assert "路人乙" not in values


def test_identity_annotation_normalization_requires_authoritative_base() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="小晶",
            role="配角",
            appearance_canonical="黑色长发，浅色衬衫，神情克制",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=5,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】夜 / 室内",
            story_function="读信",
            characters=["小晶（画外音）", "井下回声（画外）"],
            summary="小晶的信件内容被读出。",
        )],
        full_script_text=(
            "【场1】夜 / 室内\n"
            "小晶（画外音）：这是信的内容。\n"
            "路人乙（小晶的声音）：这是错误的说话人标签。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[
                KeyDialogueTurn(
                    speaker="小晶（画外音）",
                    line="这是信的内容。",
                    source_text="这是信的内容。",
                ),
                KeyDialogueTurn(
                    speaker="路人乙（小晶的声音）",
                    line="这是错误的说话人标签。",
                    source_text="这是错误的说话人标签。",
                ),
            ],
        )],
        narrative_plan=NarrativeContinuityPlan(
            scope_id="episode-5",
            identity_contracts=[NarrativeIdentityContract(
                identity_id="voice-well",
                display_name="井下回声",
                kind="画外声源",
                visual_policy="offscreen_only",
                asset_requirement="forbidden",
                voice_ids=["井下回声"],
                evidence=IdentityContractEvidence(
                    proposition_ids=["P1"],
                    rationale="来源只定义声音，不定义可见实体",
                ),
            )],
        ),
        voice_bible=[
            VoiceCanonical(
                speaker_id="小晶",
                voice_canonical="克制的年轻声音",
            ),
            VoiceCanonical(
                speaker_id="井下回声",
                voice_canonical="遥远的回声",
                role_type="offscreen_speaker",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_identity_annotations(script, bible)

    assert changes == [{
        "source_label": "小晶（画外音）",
        "canonical_name": "小晶",
        "resolution": "authority_annotation",
    }]
    assert script.scene_outline[0].characters == ["小晶", "井下回声（画外）"]
    assert script.dialogue_chains[0].turns[0].speaker == "小晶"
    assert script.dialogue_chains[0].turns[1].speaker == "路人乙（小晶的声音）"
    assert "小晶：这是信的内容。" in script.full_script_text
    assert "路人乙（小晶的声音）" in script.full_script_text


def test_existing_bible_name_that_looks_generic_keeps_its_canonical_identity(monkeypatch) -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="少年", role="主角",
            appearance_canonical="十六岁黑发少年，蓝色长衫，身形清瘦，目光坚定，衣着朴素整洁",
        )],
    )

    async def fake_chat(*_args, **_kwargs):
        return json.dumps(_identity_wire_for_call(_kwargs, [{
            "source_label": "少年", "canonical_name": "少年",
            "identity_kind": "named", "kind": "onscreen", "evidence": "少年转身迎战",
        }], messages=_args[0]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "少年转身迎战。", bible, 21,
    ))

    assert candidates[0]["name"] == "少年"
    assert candidates[0]["identity_kind"] == "named"


def test_screenplay_resolution_is_applied_before_publish_and_keeps_source_evidence() -> None:
    script = EpisodeScreenplay(
        episode_no=21,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="绿袍男子拦路并触发冲突",
            characters=["萧炎", "绿袍男子"],
            summary="绿袍男子站到萧炎面前，厉声阻止他继续前行。",
            source_basis="原文写绿袍男子拦在山门前。",
        )],
        full_script_text="【场1】日 / 山门\n绿袍男子拦住萧炎。\n绿袍男子：止步！\n萧炎：让开。",
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="绿袍男子",
                line="止步！",
                source_text="绿袍男子厉声道：止步！",
            )],
        )],
        information_ledger=[InformationItem(
            info_id="I1", content="绿袍男子拦住萧炎", speaker_id="绿袍男子",
        )],
        voice_bible=[VoiceCanonical(
            speaker_id="绿袍男子", voice_canonical="低沉粗粝",
        )],
    )
    resolutions = [{
        "source_label": "绿袍男子",
        "canonical_name": "路人甲",
        "resolution": "functional_extra",
    }]

    assert portraits.screenplay_character_resolution_errors(script, resolutions)
    changes = portraits.apply_screenplay_character_resolutions(script, resolutions)

    assert changes == [{
        "source_label": "绿袍男子", "canonical_name": "路人甲",
        "resolution": "functional_extra",
    }]
    assert script.scene_outline[0].characters == ["萧炎", "路人甲"]
    assert "路人甲拦住萧炎" in script.full_script_text
    assert "路人甲：止步！" in script.full_script_text
    assert script.dialogue_chains[0].turns[0].speaker == "路人甲"
    assert script.dialogue_chains[0].turns[0].source_text == "绿袍男子厉声道：止步！"
    assert script.scene_outline[0].source_basis == "原文写绿袍男子拦在山门前。"
    assert script.voice_bible[0].speaker_id == "路人甲"
    assert script.voice_bible[0].role_type == "functional_character"
    assert portraits.screenplay_character_resolution_errors(script, resolutions) == []


def test_resolution_does_not_turn_non_dialogue_prefix_into_speaker() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】夜 / 歌厅\n"
            "并行画面：王申和同事在歌厅唱歌。\n"
            "王申：我先回去了。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="王申",
                line="我先回去了。",
                source_text="我先回去了。",
            )],
        )],
    )

    portraits.apply_screenplay_character_resolutions(script, [{
        "source_label": "并行画面",
        "canonical_name": "路人11",
        "resolution": "functional_extra",
    }])

    assert "并行画面：王申和同事在歌厅唱歌。" in script.full_script_text
    assert "路人11：" not in script.full_script_text


def test_dialogue_normalization_demotes_unowned_colon_line_to_action() -> None:
    from app.validators import normalize_screenplay_dialogue_chains

    script = EpisodeScreenplay(
        episode_no=1,
        full_script_text=(
            "【场1】夜 / 歌厅\n"
            "路人11：王申和同事在歌厅唱歌。\n"
            "王申：我先回去了。"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[KeyDialogueTurn(
                speaker="王申",
                line="我先回去了。",
                source_text="我先回去了。",
            )],
        )],
    )

    normalize_screenplay_dialogue_chains(script)

    assert "路人11，王申和同事在歌厅唱歌。" in script.full_script_text
    assert "王申：我先回去了。" in script.full_script_text


def test_identity_gate_uses_shared_speaker_parser_and_allows_narrator() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎", role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山门",
            story_function="交代山门对峙",
            characters=["萧炎"],
            summary="山门骤然安静，萧炎站到众人面前准备迎战。",
        )],
        full_script_text="【场1】日 / 山门\n旁白：山门骤然安静。\n萧炎：我来应战。",
        information_ledger=[InformationItem(
            info_id="I1", content="山门骤然安静", delivery_owner="narration", speaker_id="旁白",
        )],
        voice_bible=[VoiceCanonical(
            speaker_id="旁白", voice_canonical="沉稳克制", role_type="narrator",
        )],
    )

    assert portraits.screenplay_unknown_identity_errors(script, bible) == []

    script.full_script_text += "\n青衣人：此路不通。"
    errors = portraits.screenplay_unknown_identity_errors(script, bible)
    assert len(errors) == 1
    assert "青衣人" in errors[0]


def test_voice_alias_is_normalized_only_from_unambiguous_ledger_identity() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[
            Character(
                name="孟浩",
                role="主角",
                appearance_canonical="黑发书生，青色长衫，身形清瘦，背着旧书箱",
            ),
            Character(
                name="王有材",
                role="重要配角",
                appearance_canonical="圆脸少年，粗布短衣，身形敦实，神态慌张",
            ),
        ],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(scope_id="episode-1"),
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】日 / 山顶",
            story_function="孟浩决定离开山顶寻找出路",
            characters=["孟浩", "王有材"],
            summary="孟浩听见王有材求救，转身寻找声音来源。",
        )],
        full_script_text=(
            "【场1】日 / 山顶\n"
            "孟浩：又落榜了。\n"
            "王有材：救命！"
        ),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            turns=[
                KeyDialogueTurn(
                    speaker="孟浩",
                    line="又落榜了。",
                    source_text="又落榜了。",
                ),
                KeyDialogueTurn(
                    speaker="王有材",
                    line="救命！",
                    source_text="救命！",
                ),
            ],
        )],
        information_ledger=[
            InformationItem(
                info_id="I1",
                content="孟浩连续三年科举落榜",
                speaker_id="V-MH",
            ),
            InformationItem(
                info_id="I2",
                content="孟浩听见王有材在山崖下求救",
                exact_text="救命！",
                speaker_id="V-WYC",
            ),
            InformationItem(
                info_id="I3",
                content="山崖下同时传来孟浩与王有材的声音",
                speaker_id="V-AMBIGUOUS",
            ),
        ],
        voice_bible=[
            VoiceCanonical(
                speaker_id="V-MH",
                voice_canonical="清瘦书生的年轻嗓音",
            ),
            VoiceCanonical(
                speaker_id="V-WYC",
                voice_canonical="慌张的少年嗓音",
            ),
            VoiceCanonical(
                speaker_id="V-AMBIGUOUS",
                voice_canonical="少年嗓音",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_voice_ids(script, bible)

    assert changes == [{
        "source_label": "V-MH",
        "canonical_name": "孟浩",
        "resolution": "voice_alias_from_ledger",
    }, {
        "source_label": "V-WYC",
        "canonical_name": "王有材",
        "resolution": "voice_alias_from_ledger",
    }, {
        "source_label": "V-AMBIGUOUS",
        "canonical_name": "",
        "resolution": "non_voice_carrier_removed",
    }]
    assert script.voice_bible[0].speaker_id == "孟浩"
    assert script.information_ledger[0].speaker_id == "孟浩"
    assert script.voice_bible[1].speaker_id == "王有材"
    assert script.information_ledger[1].speaker_id == "王有材"
    assert len(script.voice_bible) == 2
    assert script.information_ledger[2].speaker_id is None


def test_voice_normalization_removes_only_unreferenced_unbound_entries() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="孟浩",
            role="主角",
            appearance_canonical="清瘦书生，青色长衫，目光坚定",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(scope_id="episode-1"),
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="门外来客",
            turns=[KeyDialogueTurn(
                speaker="门外来客",
                line="请开门。",
                source_text="请开门。",
            )],
        )],
        voice_bible=[
            VoiceCanonical(
                speaker_id="门外来客",
                voice_canonical="门外传来的低沉人声",
            ),
            VoiceCanonical(
                speaker_id="未引用声源",
                voice_canonical="短促的非语言声响",
                role_type="sound_effect",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_voice_ids(script, bible)

    assert [voice.speaker_id for voice in script.voice_bible] == ["门外来客"]
    assert changes == [{
        "source_label": "未引用声源",
        "canonical_name": "",
        "resolution": "unreferenced_voice_removed",
    }]


def test_voice_normalization_projects_non_voice_delivery_out_of_speaker_fields() -> None:
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="孟浩",
            role="主角",
            appearance_canonical="清瘦书生，青色长衫，目光坚定",
        )],
    )
    script = EpisodeScreenplay(
        episode_no=1,
        narrative_plan=NarrativeContinuityPlan(scope_id="episode-1"),
        full_script_text="未绑定声源：当——\n门外来客：请开门。",
        key_lines=["未绑定声源：当——", "门外来客：请开门。"],
        information_ledger=[InformationItem(
            info_id="I1",
            content="门外传来一声钟响。",
            delivery_owner="ambient_sound",
            speaker_id="未绑定声源",
            exact_text="当——",
        )],
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="门外动静",
            turns=[
                KeyDialogueTurn(
                    speaker="未绑定声源",
                    line="当——",
                    source_text="当——",
                ),
                KeyDialogueTurn(
                    speaker="门外来客",
                    line="请开门。",
                    source_text="请开门。",
                ),
            ],
        )],
        voice_bible=[
            VoiceCanonical(
                speaker_id="未绑定声源",
                voice_canonical="短促的非语言声响",
                role_type="sound_effect",
            ),
            VoiceCanonical(
                speaker_id="门外来客",
                voice_canonical="门外传来的低沉人声",
            ),
        ],
    )

    changes = portraits.normalize_screenplay_voice_ids(script, bible)

    assert script.information_ledger[0].speaker_id is None
    assert [turn.speaker for turn in script.dialogue_chains[0].turns] == ["门外来客"]
    assert script.key_lines == ["门外来客：请开门。"]
    assert script.full_script_text == "【当——】\n门外来客：请开门。"
    assert [voice.speaker_id for voice in script.voice_bible] == ["门外来客"]
    assert changes == [{
        "source_label": "未绑定声源",
        "canonical_name": "",
        "resolution": "non_voice_carrier_removed",
    }]


def test_source_identity_contexts_cover_complete_long_source() -> None:
    source = "甲" * 19 + "\n\n" + "乙" * 17

    chunks = portraits._source_identity_contexts(source, budget=10)

    assert len(chunks) == 4
    assert "".join(chunks) == source.replace("\n", "")


def test_future_identity_keeps_current_display_label() -> None:
    script = EpisodeScreenplay(
        episode_no=1,
        scene_outline=[ScriptScene(
            scene_no=1,
            scene_heading="【场1】夜 / 山门",
            story_function="神秘来客阻路",
            characters=["青衣人"],
            summary="青衣人挡在门前，没有公开姓名。",
            source_basis="原文只称青衣人。",
        )],
        full_script_text="【场1】夜 / 山门\n青衣人挡在门前。\n青衣人：止步。",
        dialogue_chains=[KeyDialogueChain(
            chain_id="DC1",
            topic="阻路",
            turns=[KeyDialogueTurn(
                speaker="青衣人",
                line="止步。",
                source_text="止步。",
            )],
        )],
    )

    portraits.apply_screenplay_character_resolutions(script, [{
        "source_label": "青衣人",
        "canonical_name": "丁力",
        "resolution": "future_identity",
    }])

    assert script.scene_outline[0].characters == ["丁力"]
    assert script.dialogue_chains[0].turns[0].speaker == "丁力"
    assert "青衣人挡在门前" in script.full_script_text
    assert "丁力：止步" in script.full_script_text
    assert "丁力" not in script.scene_outline[0].summary


@pytest.mark.skip(
    reason="休眠待 P1 清理：断言重型剧本流水线内的全章人物发现自动建人物谱行为；"
    "screenplay 契约 6.0.0 起 _screenplay_task 改为轻量 episode_prep_pack 流程"
    "（app/production/prep_pack.py），角色/场景改为对 character_portraits/"
    "scene_references 的确定性解析，不再跑全章人物发现模型调用。"
)
def test_late_episode_screenplay_auto_adds_character_and_defers_portrait_generation(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "late-episode-character.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="萧炎",
            role="主角",
            appearance_canonical="黑发少年，玄色劲装，目光坚定，身形修长，腰佩火纹玉佩",
        )],
    )
    conn.execute(
        "INSERT INTO projects(id,name,status,bible_json,bible_version,bible_status,created_at) "
        "VALUES('p1','斗破苍穹','planned',?,1,'ready',1)",
        (bible.model_dump_json(),),
    )
    conn.execute(
        "INSERT INTO chapters(project_id,idx,title,content,char_count) VALUES(?,?,?,?,?)",
        ("p1", 1926, "第一千六百二十二章 双帝之战",
         "魂天帝踏着血云现身。魂天帝与萧炎在中州上空连续交锋。" * 8, 240),
    )
    conn.execute(
        """INSERT INTO episodes(
            id,project_id,episode_no,title,hook,cliffhanger,synopsis,source_chapters,
            target_duration_s,screenplay_status,status,created_at
        ) VALUES('e1926','p1',1926,'双帝之战','','','魂天帝现身','[1926]',50,'running','planned',1)"""
    )
    conn.commit()

    async def fake_candidates(source_text, current_bible, episode_no, *, draft_text="", **_kwargs):
        assert episode_no == 1926
        assert "魂天帝" in source_text
        return [{"name": "魂天帝", "kind": "onscreen", "evidence": "魂天帝踏着血云现身"}]

    async def fake_assess(*_args, **_kwargs):
        return {
            "subject_kind": "person",
            "important": True,
            "reason": "本章核心反派并反复出场",
            "role": "反派",
            "appearance_canonical": "中年男性，黑色长发披肩，暗红帝袍覆身，血色双瞳冷漠，周身缠绕血云",
            "personality": "冷酷",
            "speech_style": "低沉威压",
            "relationships": [{"to": "萧炎", "relation": "决战对手"}],
        }

    async def portrait_failure(*_args, **_kwargs):
        raise AssertionError("剧本阶段不应调用定妆图 Provider")

    generated_with: list[set[str]] = []

    async def fake_generate(ep_data, source_text, current_bible, prev_ending=""):
        names = {character.name for character in current_bible.characters}
        generated_with.append(names)
        assert "魂天帝" in names
        scenes = [
            ScriptScene(
                scene_no=index,
                scene_heading=f"【场{index}】日 / 中州天际",
                story_function="推进双帝决战并交接下一场冲突",
                characters=["萧炎", "魂天帝"],
                summary="萧炎与魂天帝在中州天际正面交锋，帝境力量持续碰撞。",
                conflict="双方争夺天地存亡的最终胜负",
                turn="帝境交锋进一步升级",
                source_basis="保留魂天帝现身并与萧炎连续交锋的原文事件",
            )
            for index in range(1, 4)
        ]
        return EpisodeScreenplay(
            episode_no=ep_data["episode_no"],
            title="双帝之战",
            scene_outline=scenes,
            full_script_text="【场1】魂天帝：今日便结束一切。\n萧炎：那就一战。",
        )

    async def fake_production(*, episode_id, episode, source_text, bible, **_kwargs):
        # 生产链仍应看到 preflight 已追加的 source-backed 角色
        script = await fake_generate(episode, source_text, bible)
        conn.execute(
            "UPDATE episodes SET screenplay_json=?, screenplay_status='ready', "
            "screenplay_error=NULL, screenplay_updated_at=? WHERE id=?",
            (script.model_dump_json(), db.now(), episode_id),
        )
        conn.commit()
        return script

    monkeypatch.setattr(portraits, "discover_character_candidates", fake_candidates)
    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)
    monkeypatch.setattr(portraits, "_generate_fresh_portrait", portrait_failure)
    monkeypatch.setattr(
        "app.production.screenplay_repair.run_screenplay_production",
        fake_production,
    )

    result = asyncio.run(api._screenplay_task("e1926"))

    project = conn.execute(
        "SELECT bible_json,bible_version FROM projects WHERE id='p1'"
    ).fetchone()
    names = {
        item["name"] for item in json.loads(project["bible_json"])["characters"]
    }
    episode = conn.execute(
        "SELECT screenplay_status,screenplay_json,screenplay_error FROM episodes WHERE id='e1926'"
    ).fetchone()
    assert result is not None
    assert result.title == "双帝之战"
    assert names == {"萧炎", "魂天帝"}
    assert project["bible_version"] == 2
    assert generated_with == [{"萧炎", "魂天帝"}]
    assert episode["screenplay_status"] == "ready"
    assert episode["screenplay_json"] is not None
    assert episode["screenplay_error"] is None
    queue = json.loads(conn.execute(
        "SELECT bible_auto_changes_json FROM projects WHERE id='p1'"
    ).fetchone()["bible_auto_changes_json"])
    assert queue[0]["character"] == "魂天帝"
    assert queue[0]["status"] == "auto_applied_asset_pending"


def test_future_identity_accepts_new_name_with_owned_verbatim_evidence(
    monkeypatch,
) -> None:
    """真名逐字在后续窗口、但模型证据措辞不精确时，程序应确定性重定位证据并保留解析。"""
    future = (
        "绿袍男子恭敬地说道，许师姐好手段。"
        "许师姐已经到了凝气第七层，被掌教赐了风幡。"
        "另一个绿袍修士感慨，孟浩看着许师姐消失在山峦间。"
    ) * 2

    async def fake_structured(messages, **kwargs):
        return portraits.FutureIdentityCandidateResponse.model_validate(
            _future_identity_wire([{
                "source_label": "会飞的女人",
                "canonical_name": "许师姐",
                "identity_kind": "named",
                "future_evidence": "许师姐已经到了凝气第七层",
            }], provider_schema=kwargs["response_format"]["json_schema"][
                "schema"
            ])
        )

    monkeypatch.setattr(portraits.model_gateway, "chat_structured", fake_structured)

    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "source_label": "会飞的女人",
            "identity_kind": "functional",
            "kind": "onscreen",
            "identity_group": "grp-fly",
        }],
        source_text="一个会飞的女人出现在众人面前。",
        future_text=future,
        bible=Bible(characters=[], world=World(visual_style_canonical="测试")),
        episode_no=2,
    ))

    fly = next(item for item in resolved if item["source_label"] == "会飞的女人")
    assert fly["name"] == "许师姐"
    assert fly["identity_kind"] == "named"
    assert fly["authority_id"] == "bible:许师姐"
    # 新真名证据必须由provider逐字拥有，程序不再静默重写。
    assert "许师姐" in fly["future_evidence"]
    assert fly["future_evidence"] in future


@pytest.mark.parametrize("known_authority", [False, True])
def test_future_identity_persists_bounded_evidence_with_authority_anchor(
    monkeypatch,
    known_authority: bool,
) -> None:
    future = (
        "“黑衣人"
        + "缓慢向前走。" * 35
        + "最后摘下面具说道我叫丁力。”"
    )
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "黑衣人",
                "canonical_name": "丁力",
                "identity_kind": (
                    "functional" if known_authority else "named"
                ),
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    bible_characters = [
        Character(
            name="丁力",
            role="重要配角",
            appearance_canonical="黑衣男子，短发利落，身形健硕",
        )
    ] if known_authority else []
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "黑衣人",
            "source_label": "黑衣人",
            "identity_kind": "functional",
            "identity_group": "current-1:F1",
            "kind": "onscreen",
        }],
        source_text="黑衣人站在门口。",
        future_text=future,
        bible=Bible(
            world=World(visual_style_canonical="国风"),
            characters=bible_characters,
        ),
        episode_no=1,
    ))

    assert calls == 1
    if known_authority:
        assert resolved[0]["identity_kind"] == "functional"
        assert resolved[0].get("future_evidence", "") == ""
        assert not resolved[0].get("authority_id")
    else:
        evidence = resolved[0]["future_evidence"]
        assert 0 < len(evidence) <= 120
        assert "丁力" in evidence
        assert evidence in future
        assert resolved[0]["authority_id"] == "bible:丁力"


def test_future_identity_catalog_reserves_middle_label_reveal_window(
    monkeypatch,
) -> None:
    reveal = "黑衣人摘下面具说我叫丁力"
    future = "“" + "甲" * 1000 + reveal + "乙" * 1000 + "”"
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        prompt = messages[0]["content"]
        raw_catalog = prompt.split("后续证据目录", 1)[1]
        raw_catalog = raw_catalog.split("\n", 1)[1]
        raw_catalog = raw_catalog.split("\n可选决议目录", 1)[0]
        evidence_catalog = json.loads(raw_catalog)
        assert all(len(item["text"]) <= 120 for item in evidence_catalog)
        assert any(reveal in item["text"] for item in evidence_catalog)
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "黑衣人",
                "canonical_name": "丁力",
                "identity_kind": "named",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "黑衣人",
            "source_label": "黑衣人",
            "identity_kind": "functional",
            "identity_group": "current-1:F1",
            "kind": "onscreen",
        }],
        source_text="黑衣人站在门口。",
        future_text=future,
        bible=Bible(
            world=World(visual_style_canonical="国风"),
            characters=[],
        ),
        episode_no=1,
    ))

    assert calls == 1
    assert resolved[0]["name"] == "丁力"
    assert "丁力" in resolved[0]["future_evidence"]
    assert resolved[0]["future_evidence"] in future


def test_future_identity_catalog_overlaps_plain_segment_boundary(
    monkeypatch,
) -> None:
    prefix = "黑衣人" + "甲" * (119 - len("黑衣人"))
    future = prefix + "丁力" + "乙" * 150
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        prompt = messages[0]["content"]
        raw_catalog = prompt.split("后续证据目录", 1)[1]
        raw_catalog = raw_catalog.split("\n", 1)[1]
        raw_catalog = raw_catalog.split("\n可选决议目录", 1)[0]
        evidence_catalog = json.loads(raw_catalog)
        assert len(evidence_catalog) <= 6
        assert any("丁力" in item["text"] for item in evidence_catalog)
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "黑衣人",
                "canonical_name": "丁力",
                "identity_kind": "named",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "黑衣人",
            "source_label": "黑衣人",
            "identity_kind": "functional",
            "identity_group": "current-1:F1",
            "kind": "onscreen",
        }],
        source_text="黑衣人站在门口。",
        future_text=future,
        bible=Bible(
            world=World(visual_style_canonical="国风"),
            characters=[],
        ),
        episode_no=1,
    ))

    assert calls == 1
    assert resolved[0]["name"] == "丁力"
    assert "丁力" in resolved[0]["future_evidence"]
    assert resolved[0]["future_evidence"] in future


def test_future_identity_catalog_caps_distinct_windows_per_group(
    monkeypatch,
) -> None:
    future = "".join(
        "黑衣人" + f"{index:04d}" + chr(0x4E00 + index) * 150
        for index in range(12)
    )

    async def fake_chat(messages, **kwargs):
        prompt = messages[0]["content"]
        raw_catalog = prompt.split("后续证据目录", 1)[1]
        raw_catalog = raw_catalog.split("\n", 1)[1]
        raw_catalog = raw_catalog.split("\n可选决议目录", 1)[0]
        evidence_catalog = json.loads(raw_catalog)
        assert len(evidence_catalog) == 6
        assert len({item["text"] for item in evidence_catalog}) == 6
        return json.dumps(
            _identity_wire_for_call(
                kwargs,
                [{
                    "source_label": "黑衣人",
                    "identity_kind": "functional",
                }],
                messages=messages,
            ),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "黑衣人",
            "source_label": "黑衣人",
            "identity_kind": "functional",
            "identity_group": "current-1:F1",
            "kind": "onscreen",
        }],
        source_text="黑衣人站在门口。",
        future_text=future,
        bible=Bible(
            world=World(visual_style_canonical="国风"),
            characters=[],
        ),
        episode_no=1,
    ))

    assert resolved[0]["identity_kind"] == "functional"


def test_future_identity_rejects_name_absent_from_window(monkeypatch) -> None:
    """真名不在后续窗口时（模型臆测），即便声称 named 也不得取得解析，防捏造约束不放松。"""
    future = "绿袍男子恭敬地说道，许师姐好手段。许师姐已经到了凝气第七层。" * 2

    async def fake_structured(messages, **kwargs):
        return json.dumps(_identity_wire_for_call(kwargs, [{
            "source_label": "会飞的女人",
            "canonical_name": "许清",  # 窗口里根本没有“许清”
            "identity_kind": "named",
            "future_evidence": "绿袍男子称该会飞的女子为许清",
        }]), ensure_ascii=False)

    monkeypatch.setattr(portraits.model_gateway, "chat", fake_structured)

    with pytest.raises(model_gateway.StructuredSemanticError):
        asyncio.run(portraits.resolve_future_identity_candidates(
            [{
                "source_label": "会飞的女人",
                "identity_kind": "functional",
                "kind": "onscreen",
                "identity_group": "grp-fly",
            }],
            source_text="一个会飞的女人出现在众人面前。",
            future_text=future,
            bible=Bible(characters=[], world=World(visual_style_canonical="测试")),
            episode_no=2,
        ))


def _rf11_literal_candidate(source_text: str, source_label: str) -> dict:
    receipts = [
        record
        for record in portraits._current_identity_evidence_records(source_text)
        if source_label in str(record.get("text") or "")
    ]
    assert receipts
    receipts = sorted(
        receipts,
        key=portraits._current_identity_receipt_sort_key,
    )
    primary = receipts[0]
    return {
        "name": source_label,
        "source_label": source_label,
        "identity_kind": "functional",
        "identity_group": "current-1:F1",
        "authority_id": "",
        "kind": "onscreen",
        "evidence": source_label,
        "future_evidence": "",
        "source_label_provenance": (
            portraits.CURRENT_IDENTITY_LITERAL_PROVENANCE
        ),
        "source_evidence_receipt": dict(primary),
        "source_evidence_receipts": [dict(value) for value in receipts],
        "source_segment_id": primary["source_segment_id"],
        "source_segment_ids": [
            value["source_segment_id"] for value in receipts
        ],
        "source_quote": primary["text"],
    }


def test_attempt16_rf11_global_wire_aggregates_occurrence_receipts_once(
    monkeypatch,
) -> None:
    paragraphs = [
        f"孟浩在第{index:02d}处查看山路。" for index in range(1, 51)
    ] + [
        f"守卫{index:02d}在第{index:02d}道门前值守。"
        for index in range(1, 13)
    ]
    source_text = "\n\n".join(paragraphs)
    assert len(portraits._current_identity_evidence_records(source_text)) == 62
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        meta = kwargs["call_meta"]
        assert meta["contract_version"] == (
            portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION
        )
        assert meta["current_identity_version"] == (
            portraits.CURRENT_IDENTITY_DECISION_VERSION
        )
        assert meta["reuse_successful_operation"] is False
        assert meta["disable_provider_retries"] is True
        assert meta["disable_provider_candidate_fallback"] is True
        assert meta["disable_reasoning_fallback"] is True
        assert kwargs["response_format"]["json_schema"]["name"] == (
            "screenplay_current_identity_discovery_v11"
        )
        prompt = str(messages[0]["content"])
        assert "decisions 必须精确包含目录中全部 E" not in prompt
        assert "root 只输出一次 k/n/f" in prompt
        schema = kwargs["response_format"]["json_schema"]["schema"]
        refs = schema["$defs"]["CurrentFunctionalIdentityDecision"][
            "properties"
        ]["evidence_ref"]["enum"]
        assert len(refs) == 62
        characters = [
            {
                "source_label": "孟浩",
                "canonical_name": "孟浩",
                "identity_kind": "named",
                "kind": "onscreen" if index == 49 else "mentioned",
                "evidence_ref": refs[index],
            }
            for index in range(50)
        ]
        characters.extend([
            {
                "source_label": f"守卫{index:02d}",
                "identity_kind": "functional",
                "functional_identity_key": f"F{index}",
                "kind": "onscreen",
                "evidence_ref": refs[49 + index],
            }
            for index in range(1, 13)
        ])
        return json.dumps(
            _identity_wire_for_call(kwargs, characters, messages=messages),
            ensure_ascii=False,
        )

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.discover_character_candidates(
        source_text,
        Bible(
            world=World(visual_style_canonical="国风"),
            characters=[Character(
                name="孟浩",
                role="主角",
                appearance_canonical="黑发长衫，五官清晰，体态稳定",
            )],
        ),
        1,
    ))

    assert calls == 1
    assert len(result) == 13
    meng = next(item for item in result if item["source_label"] == "孟浩")
    assert meng["kind"] == "onscreen"
    assert len(meng["source_evidence_receipts"]) == 50
    assert len(meng["source_segment_ids"]) == 50
    assert meng["source_evidence_receipt"] == meng[
        "source_evidence_receipts"
    ][0]
    assert meng["evidence"] in meng["source_quote"]


def test_attempt16_old_rf10_wire_fails_once_before_downstream(monkeypatch) -> None:
    calls = 0
    downstream: list[str] = []

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return json.dumps({
            "decisions": {"E001": {"k": [], "n": [], "f": []}},
        })

    async def forbidden_future(*_args, **_kwargs):
        downstream.append("future")
        raise AssertionError("old RF10 wire reached downstream")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        portraits,
        "resolve_future_identity_candidates",
        forbidden_future,
    )
    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(portraits.discover_character_candidates(
            "守卫站在山门。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))

    assert calls == 1
    assert downstream == []


@pytest.mark.parametrize(
    "mutation",
    [
        "tamper", "non_dict", "duplicate", "out_of_order",
        "ids_mismatch", "primary_mismatch", "wrong_label",
        "ids_integer", "ids_mapping", "ids_whitespace", "ids_duplicate",
    ],
)
def test_current_identity_receipt_v2_bundle_is_fail_closed(
    mutation: str,
) -> None:
    source_text = "守卫守在山门。\n\n守卫走到殿前。"
    candidate = _rf11_literal_candidate(source_text, "守卫")
    if mutation == "tamper":
        candidate["source_evidence_receipts"][1]["text"] += "伪造"
    elif mutation == "non_dict":
        candidate["source_evidence_receipts"].append("bad")
    elif mutation == "duplicate":
        first = dict(candidate["source_evidence_receipts"][0])
        candidate["source_evidence_receipts"] = [first, dict(first)]
        candidate["source_segment_ids"] = [first["source_segment_id"]]
    elif mutation == "out_of_order":
        candidate["source_evidence_receipts"].reverse()
        candidate["source_evidence_receipt"] = dict(
            candidate["source_evidence_receipts"][0]
        )
        candidate["source_segment_id"] = candidate[
            "source_evidence_receipt"
        ]["source_segment_id"]
        candidate["source_segment_ids"].reverse()
    elif mutation == "ids_mismatch":
        candidate["source_segment_ids"] = ["SRC9999"]
    elif mutation == "primary_mismatch":
        candidate["source_evidence_receipt"] = dict(
            candidate["source_evidence_receipts"][1]
        )
        candidate["source_segment_id"] = candidate[
            "source_evidence_receipt"
        ]["source_segment_id"]
    elif mutation == "wrong_label":
        candidate["source_label"] = "银袍女子"
    elif mutation == "ids_integer":
        candidate["source_segment_ids"] = 1
    elif mutation == "ids_mapping":
        candidate["source_segment_ids"] = {"SRC0001": 1}
    elif mutation == "ids_whitespace":
        candidate["source_segment_ids"][0] = (
            " " + candidate["source_segment_ids"][0]
        )
    elif mutation == "ids_duplicate":
        candidate["source_segment_ids"].append(
            candidate["source_segment_ids"][0]
        )

    with pytest.raises(
        portraits.ContentGenerationError,
        match="receipt v2 无效",
    ):
        portraits._attach_candidate_source_evidence(
            [candidate],
            source_text,
        )
    assert candidate["source_evidence_receipts"] == []
    assert candidate["source_segment_ids"] == []


def test_current_identity_receipt_v2_resolution_and_hash_keep_all_evidence() -> None:
    source_text = "守卫守在山门。\n\n守卫走到殿前。\n\n守卫敲了敲门。"
    candidate = _rf11_literal_candidate(source_text, "守卫")
    candidate["identity_scope_fingerprint"] = (
        portraits.screenplay_identity_scope_fingerprint(1, source_text)
    )
    resolution = portraits._identity_resolution(
        candidate,
        "守卫",
        "functional_identity",
    )

    assert resolution["source_evidence_receipts"] == candidate[
        "source_evidence_receipts"
    ]
    assert resolution["source_segment_ids"] == candidate["source_segment_ids"]
    assert resolution["source_segment_id"] == candidate["source_segment_id"]
    assert portraits.screenplay_identity_resolution_is_current_for_source(
        resolution,
        episode_no=1,
        source_text=source_text,
    )

    changed = dict(candidate)
    changed["source_evidence_receipts"] = [
        dict(candidate["source_evidence_receipts"][0]),
        dict(candidate["source_evidence_receipts"][2]),
    ]
    changed["source_segment_ids"] = [
        value["source_segment_id"]
        for value in changed["source_evidence_receipts"]
    ]
    assert portraits._structural_identity_candidate_semantic_hash(
        [candidate]
    ) != portraits._structural_identity_candidate_semantic_hash([changed])


def test_persist_replaces_invalid_current_bundle_with_valid_v2() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, "
        "screenplay_character_resolutions TEXT NOT NULL)"
    )
    source_text = "守卫守在山门。\n\n守卫走到殿前。"
    candidate = _rf11_literal_candidate(source_text, "守卫")
    candidate["identity_scope_fingerprint"] = (
        portraits.screenplay_identity_scope_fingerprint(1, source_text)
    )
    good = portraits._identity_resolution(
        candidate,
        "守卫",
        "functional_identity",
    )
    bad = dict(good)
    bad.pop("source_evidence_receipt")
    bad.pop("source_evidence_receipts")
    bad["source_segment_id"] = ""
    bad["source_segment_ids"] = []
    conn.execute(
        "INSERT INTO episodes VALUES(?,?)",
        ("ep1", json.dumps([bad], ensure_ascii=False)),
    )
    conn.commit()

    persisted = portraits.persist_screenplay_character_resolutions(
        conn,
        "ep1",
        [good],
    )

    assert persisted[0]["source_evidence_receipts"] == good[
        "source_evidence_receipts"
    ]
    stored = json.loads(conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='ep1'"
    ).fetchone()[0])
    assert stored[0]["source_evidence_receipts"] == good[
        "source_evidence_receipts"
    ]


def test_persist_keeps_stored_bytes_for_two_valid_receipt_subsets() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, "
        "screenplay_character_resolutions TEXT NOT NULL)"
    )
    source_text = "守卫守在山门。\n\n守卫走到殿前。\n\n守卫敲了敲门。"
    base = _rf11_literal_candidate(source_text, "守卫")
    scope = portraits.screenplay_identity_scope_fingerprint(1, source_text)

    def resolution_for(indexes: tuple[int, int]) -> dict:
        candidate = dict(base)
        receipts = [
            dict(base["source_evidence_receipts"][index]) for index in indexes
        ]
        candidate.update({
            "source_evidence_receipt": dict(receipts[0]),
            "source_evidence_receipts": receipts,
            "source_segment_id": receipts[0]["source_segment_id"],
            "source_segment_ids": [
                value["source_segment_id"] for value in receipts
            ],
            "source_quote": receipts[0]["text"],
            "identity_scope_fingerprint": scope,
        })
        return portraits._identity_resolution(
            candidate,
            "守卫",
            "functional_identity",
        )

    first = resolution_for((0, 1))
    second = resolution_for((0, 2))
    original_json = json.dumps([first], ensure_ascii=False)
    conn.execute("INSERT INTO episodes VALUES(?,?)", ("ep1", original_json))
    conn.commit()

    persisted = portraits.persist_screenplay_character_resolutions(
        conn,
        "ep1",
        [second],
    )
    stored_json = conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='ep1'"
    ).fetchone()[0]

    assert stored_json == original_json
    assert persisted[0]["source_evidence_receipts"] == first[
        "source_evidence_receipts"
    ]


def test_persist_repairs_invalid_adjudication_receipt_and_keeps_valid_bytes(
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE episodes(id TEXT PRIMARY KEY, "
        "screenplay_character_resolutions TEXT NOT NULL)"
    )
    source_text = "守卫守在山门。\n\n守卫走到殿前。"
    source_hash = portraits.evidence_repository.content_hash(source_text)
    scope = portraits.screenplay_identity_scope_fingerprint(1, source_text)

    def adjudicated(source_id: str | list[str]) -> dict:
        source_ids = [source_id] if isinstance(source_id, str) else source_id
        receipt_payload = {
            "version": "screenplay-ir-identity-adjudicator.v2",
            "source_hash": source_hash,
            "source_segment_ids": source_ids,
        }
        return {
            "source_label": "守卫",
            "canonical_name": "守卫",
            "resolution": "functional_identity",
            "identity_group": "functional:guard",
            "authority_id": "functional:guard",
            "source_instance_key": "functional:guard",
            "identity_scope_fingerprint": scope,
            "decision_provenance": (
                portraits.AUTOMATIC_IDENTITY_DECISION_PROVENANCE
            ),
            "decision_contract_version": (
                portraits.FUTURE_IDENTITY_DECISION_VERSION
            ),
            "structural_identity_policy_version": (
                portraits.STRUCTURAL_IDENTITY_COVERAGE_VERSION
            ),
            "source_label_provenance": (
                portraits.IDENTITY_ADJUDICATION_SOURCE_PROVENANCE
            ),
            "decision_source": "screenplay-ir-identity-adjudicator.v2",
            "evidence_source_ids": source_ids,
            "source_segment_ids": source_ids,
            "identity_adjudication_receipt": {
                **receipt_payload,
                "hash": portraits.evidence_repository.content_hash(
                    receipt_payload
                ),
            },
        }

    first = adjudicated("SRC0001")
    for invalid_source_ids in (["SRC9999"], ["SRC0002", "SRC0001"]):
        invalid = adjudicated(invalid_source_ids)
        assert portraits._identity_adjudication_receipt_is_valid(
            invalid,
            source_text=None,
        )
        assert not portraits.screenplay_identity_resolution_is_current_for_source(
            invalid,
            episode_no=1,
            source_text=source_text,
        )
    bad = json.loads(json.dumps(first, ensure_ascii=False))
    bad["identity_adjudication_receipt"]["hash"] = "bad"
    conn.execute(
        "INSERT INTO episodes VALUES(?,?)",
        ("ep1", json.dumps([bad], ensure_ascii=False)),
    )
    conn.commit()

    repaired = portraits.persist_screenplay_character_resolutions(
        conn, "ep1", [first]
    )
    assert repaired[0]["identity_adjudication_receipt"] == first[
        "identity_adjudication_receipt"
    ]
    assert portraits.load_screenplay_character_resolutions_for_source(
        conn,
        "ep1",
        episode_no=1,
        source_text=source_text,
    ) == repaired

    stored_json = conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='ep1'"
    ).fetchone()[0]
    second = adjudicated("SRC0002")
    persisted = portraits.persist_screenplay_character_resolutions(
        conn, "ep1", [second]
    )
    assert conn.execute(
        "SELECT screenplay_character_resolutions FROM episodes WHERE id='ep1'"
    ).fetchone()[0] == stored_json
    assert persisted[0]["identity_adjudication_receipt"] == first[
        "identity_adjudication_receipt"
    ]


def test_legacy_generic_cache_rejects_tampered_typed_v2_bundle(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE artifacts(id TEXT PRIMARY KEY, scope_type TEXT, "
        "scope_id TEXT, type TEXT, status TEXT, content_json TEXT, "
        "content_hash TEXT, created_at REAL)"
    )
    source_text = "守卫守在山门。\n\n守卫走到殿前。"
    bible = Bible(world=World(visual_style_canonical="国风"), characters=[])
    bad = _rf11_literal_candidate(source_text, "守卫")
    bad["source_evidence_receipts"][1]["text"] += "伪造"
    discovery_input = {
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": (
            portraits._current_identity_evidence_catalog_hash(source_text)
        ),
        "mode": "legacy",
        "episode_no": 1,
        "source_text": source_text,
        "draft_text": "",
        "future_text": "",
        "future_label": "",
        "bible": bible.model_dump(mode="json"),
        "existing_resolutions": [],
        "structural_evidence": [],
    }
    cached_content = {
        "contract_version": portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION,
        "current_identity_version": portraits.CURRENT_IDENTITY_DECISION_VERSION,
        "current_evidence_catalog_hash": discovery_input[
            "current_evidence_catalog_hash"
        ],
        "input_hash": portraits.evidence_repository.content_hash(
            discovery_input
        ),
        "mode": "legacy",
        "candidates": [bad],
    }
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
        (
            "bad-cache", "episode", "ep1",
            "screenplay_identity_discovery", "validated",
            json.dumps(cached_content, ensure_ascii=False),
            portraits.evidence_repository.content_hash(cached_content),
            1.0,
        ),
    )
    conn.commit()
    calls = 0

    async def fresh_legacy(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return [{
            "name": "守卫",
            "source_label": "守卫",
            "identity_kind": "functional",
            "identity_group": "legacy:F1",
            "kind": "onscreen",
        }]

    monkeypatch.setattr(portraits, "get_conn", lambda: conn)
    monkeypatch.setattr(portraits, "get_setting", lambda *_args: "false")
    monkeypatch.setattr(
        portraits,
        "_discover_character_candidates_legacy",
        fresh_legacy,
    )
    monkeypatch.setattr(
        portraits.evidence_repository,
        "create_artifact",
        lambda *_args, **_kwargs: {"id": "fresh"},
    )

    result = asyncio.run(portraits.discover_character_candidates(
        source_text,
        bible,
        1,
        scope_id="ep1",
    ))

    assert calls == 1
    assert result[0]["identity_group"] == "legacy:F1"
    assert result[0].get("source_evidence_receipts") is None


def test_current_identity_named_rebinds_wrong_evidence_ref(monkeypatch) -> None:
    """模型挑错 E 下标不该炸掉整集：称谓逐字出现在本批另一条 owned 证据里就改绑。"""
    calls = 0
    source_text = (
        "孟浩抬头望向山巅，久久没有说话。\n\n"
        "“许师姐好手段。”绿袍男子带着恭维说道。"
    )

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "许师姐",
                "canonical_name": "许师姐",
                "identity_kind": "named",
                "kind": "mentioned",
                # 第一段里没有“许师姐”，模型却选了 E001。
                "evidence_ref": "E001",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(portraits.extract_current_identity_candidates(
        source_text,
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        1,
    ))

    assert calls == 1
    named = [item for item in result if item["source_label"] == "许师姐"]
    assert len(named) == 1
    assert named[0]["identity_kind"] == "named"
    assert "许师姐" in named[0]["source_quote"]
    assert named[0]["source_segment_id"] == "SRC0002"


def test_current_identity_named_without_any_owned_evidence_still_fails(
    monkeypatch,
) -> None:
    """改绑只认原文逐字存在的称谓；正文里根本没有的名字仍必须硬失败。"""
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "许师姐",
                "canonical_name": "许师姐",
                "identity_kind": "named",
                "kind": "onscreen",
                "evidence_ref": "E001",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="缺少逐字 owned evidence",
    ):
        asyncio.run(portraits.extract_current_identity_candidates(
            "孟浩抬头望向山巅，久久没有说话。\n\n绿袍男子带着恭维说道。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))
    assert calls == 1


# --- ERR-20260823-66c63c / ERR-20260823-71551e --------------------------------
# EP5 (proj_3ac0b627fa46, source chapter idx=5) production run: the model
# repeated the exact same generic label "男子" twice with the exact same
# functional_identity_key "F3" -- its own declared "this is one person"
# signal per prompt rule 6 ("不同 source_label 若明确是同一人必须共用同一
# ID"; the same source_label repeating the same ID is a stronger version of
# that same declaration).  But it cited two different evidence refs (E021
# "...男女修士...", E038 "...许姓女子...") -- neither literally contains
# "男子".  The chapter's only literal "男子" is one paragraph later, at E039
# ("...银袍，神色淡然的男子...").  Because both citations missed, both
# entries fell into the non-literal synthetic branch, which isolates
# identity_group per (source_label, evidence_id) -- so the one declared
# person split into two different identity_group values and hard-failed the
# whole episode with the exact production error text:
#   source_label 重复：男子；current 同一 source_label 对应多个
#   identity_group：男子
# chapter_text below is chapters.content for (proj_3ac0b627fa46, idx=5)
# verbatim; the f payload below is response_json.choices[0].message.content
# from provider_calls id=7252 (operation_id
# screenplay.identity.current.v6:5:1:000a67cec...), unmodified.

_EP5_CHAPTER5_TEXT = """第五章此子不错

“竟然是上官师叔亲自来发丹，而且许师姐与陈师兄是内门弟子，以往很少看到，这次居然也都来了，莫非这次有单独丹药发放不成？”

“应该是这样，你们看外宗的韩宗师兄出现了，他是外宗第二人，修为凝气五层，若能到了凝气七层，就可自动成为内门弟子，可惜没有看到王腾飞师兄。”

“以王腾飞师兄的资质，根本就不会在意这些丹药，他当年加入靠山宗，可是引起了掌门长老等人不小的轰动，若非是王师兄不愿坏了宗门规矩，要凭自己本事进入内宗，如今早就是第三位内宗弟子了。”

“嘿嘿，这次有好戏看了，每次单独发放的丹药都有十二个时辰的封印期，每个时辰都会散发丹光给人指引来抢夺，在这期间更是无法服用，就算带着丹药逃走，估计也没有本事藏十二个时辰。”

孟浩听着身边同门的议论，尽管是第一次参与这种事，但也知晓，每一次外宗的发丹，将是引起争夺的关键，这半个月来他看到了不少抢夺之事，死亡也有发生。

尤其是这次似乎有单独丹药发放，想来争夺必定是更为激烈。

孟浩沉默，暗道自己只有凝气一层，这种所谓的单独丹药，应该不会落在自己身上，只是看着四周一片带着贪婪之意的面孔，孟浩对弱肉强食这四个字，有了更深的了解。

“安静！”平台上金袍老者淡淡开口，他声音不大，可传出时却如滚滚雷霆轰隆隆的降临天地，震的下方所有修士一个个心神震动，双耳嗡鸣，孟浩这里更是如此，好半晌才恢复过来。

“老夫上官修，今日放丹，余者每人一粒凝灵丹，半块灵石。”上官修右手一挥，立刻一百多粒丹药与灵石瞬间四散，竟没有丝毫差错的落入每一个人的面前，孟浩望着漂浮在身前的丹药与灵石，阵阵药香让人陶醉，这是孟浩第一次看到丹药，也是他第一次看到灵石。

这灵石只有指甲盖大小，晶莹剔透，让人看一眼就会忍不住沉迷进去。

他心跳立刻加速，这丹药与灵石必定还是价值千金，孟浩毫不迟疑一把拿入手中，正要将丹药吞下时却发现四周之人没有一个如此，内心一动，再看手中丹药，那上面有一层光芒，隐隐有一道古怪的印记。

“还有这粒……旱灵丹。”孟浩正观察手中丹药时，高台上的上官修声音继续传出，在他的手中，赫然出现了一枚紫色的丹药。

这丹药一出，瞬间整个广场都掀起了一片药香，孟浩闻一口，顿时觉得体内的灵气竟多了一丝，心知这丹药绝非寻常。

“居然是……旱灵丹！”

“这……这是对凝气五层以上修士极为珍贵的丹药，估计宗门内也没多少，居然拿出了一颗！”

“此丹一出，这次外宗争夺会死伤多少人啊。”众人嗡鸣，看向上官修手中丹药的目光，顿时凝聚了无数的贪婪与渴望，尤其是那些修为即将突破的弟子，更是呼吸都急促起来。

“今日本无此丹，可听闻本月有弟子晋升外宗，老夫很是欣喜，若能日月如此，靠山宗辉煌指日可待，这丹药便送此人以作勉励。”上官修微微一笑，目光扫过人群，落在了孟浩身上。

孟浩内心咯噔一声，他听到老者前半句话就觉得不妙，可还没等他反应过来，上官修右手一挥，顿时其手中的紫色丹药刹那出现在了孟浩的面前，由不得他拒绝般，直接落在了他的手中。

在这一刹，孟浩进入靠山宗前所未有的，瞬间成为了万众瞩目，他四周所有的目光齐齐凝聚而来。

那些目光里的贪婪与凶残，仿佛要将孟浩生生撕裂，就连上官修身边的男女修士，也都在此时看向了孟浩，那女子看到孟浩后一怔，但很快就恢复了冰冷。

“哈哈，居然是一个凝气一层的弟子获得丹药，这一次应该好争夺了不少，此人如今已是公敌。”

“此人完了，上次单独丹药发放，我记得那人是凝气二层，就因为迟疑了一下，被没抢到丹药的赵武刚师兄泄愤生生拽入公开区内砍了脑袋。”

阵阵议论之声回荡，许多凝气二三层的弟子，尽管知晓危险，但也忍不住贪婪起来，毕竟这一次丹药首获之人，修为实在是弱到了极致，使得他们仿佛也有了抢夺的资格。

孟浩全身冷汗已经泌出，他想立刻扔了丹药，可却发现这丹药如粘在了手上，无法扔下，四周虎视眈眈的目光，让孟浩刹那间仿佛感受到了死亡的阴影，甚至他看到有不少人带着凶狠之意，正快步向自己这里走来。

“师弟，这丹药一会你扔给我，否则的话，我要你好看。”

“你敢不给我，明年的今天，是你的忌日。”阵阵声音如冷冽之风回荡孟浩四周。

与此同时，在这靠山宗四周的山峰上，有两个老者盘膝坐在山顶，正笑眯眯的看着山下外宗广场的一幕幕。

“上官师侄太不讲究了，把这丹药给了这刚入门的小娃，完了，估计我们靠山宗又要少了一个弟子。”

“这一次的争夺没意思，我赌这小娃一会广场禁制消散后会将立刻扔丹。”

随着二老的彼此谈论，下方广场的九根柱子颜色瞬间黯淡下来，看其样子，也就是十多息后，就会完全失去光芒，到了那时，此地广场的禁制也会立刻消失。

孟浩心脏快跳，不用别人去说，他也能明白当这九根柱子光芒消失后，等待自己将是一场疯狂，甚至直接扔了丹药都极有可能引起一些人不喜，迁怒自己。

“这……这怎么给我了。”孟浩全身冷汗，心神瞬间千转，不扔必死，扔了日后恐怕也一样会被迁怒，孟浩几乎用出了三年读书的全部脑筋，要看光柱光芒将暗，彩霞石台上的上官修挥袖要离去，在这危急关头孟浩脑海刹那灵机一闪，猛地迈出一步，大声喊出。

“弟子有话要说。”

“弟子能来到靠山宗，能感受到伟大的靠山宗磅礴的仙家气息，全因一场造化，弟子非常感谢给我造化的那个人。”

“弟子日夜期盼可以再次看到她，要当面感谢她，直至今日弟子终于看到了。”孟浩越说越快，话语传出时让平台上的上官修一愣，不再离去而是向孟浩看来。

“此人就是许师姐，许师姐，师弟对你感激万分，无以为报，特将此丹送给你，只有这样才可以报答师姐再造之恩。”孟浩说着，立刻抬起右手，将那粘在手上的丹药高高举起。

上官修怔了一下，显然没想到孟浩居然要说的是这些话语，神色有些古怪，嘴角渐渐露出微笑，其旁穿着银袍的许姓女子自然也是一愣，她就算是再冷漠，如今也是神色有了变化，她虽说修为已是凝气七层，这旱灵丹对她用处不大，但就算身为内门弟子，因旱灵丹极为稀少，她要获得也并非容易，自忖此丹若是与其他几种丹药重新熔炼，可炼出一炉对自己帮助甚大的五灵丹，故而此刻也不由得怦然心动。

就算是那一样银袍，神色淡然的男子，此刻也不由得多看了孟浩几眼。

在这一刹那，四周之人瞬间安静下来，那些快步走向孟浩的修士也下意识的脚步一顿，一个个神色古怪，看向孟浩的目光带着错愣。

“还能这样……”

“居然当着这么多人的面，如此赤露露的送出丹药，而且竟是给内门弟子，这……这谁还敢继续抢，这是和内门弟子抢丹药。”

“这个方法简单，可怎么当年我就没想到，该死的，该死的！”

“他奶奶的，老子当年也没想到这个方法，害的我重伤躺了三个多月。”

短暂的安静之后，立刻哗然四起，所有看向孟浩的目光，瞬间蕴含了无数情绪，不说古往今来，但此地修士多年这还是第一次遇到这种处理丹药的方式，不由得使众人看向孟浩时，在这一刹那深深的记住了孟浩。

与此同时，九根柱子的光芒彻底散去，可在孟浩手中的丹药，却是于这一瞬，竟无人去抢，这一幕在靠山宗的发丹之日，极其罕见。

许姓女子神色很快恢复如常，毫不迟疑的右手抬起向下一抓，顿时孟浩高举的丹药立刻飞出直奔这女子，被她一把拿在手中。眼看丹药被取走，孟浩暗叹，可也知道此物对目前的自己而言是祸根，此时四周的人群，一个个纷纷暗自叹气，有心对孟浩迁怒，可想到那许师姐，一个个顿时犹豫中打消了念头。

许姓女子迟疑了一下，觉得以自己内门弟子的身份，白拿一个刚入门的外宗弟子的好处，有些过意不去。

“我早年于外宗被赐了一座南峰洞府，借你居住。”许姓女子沉默片刻，从储物袋内取出一枚白色的玉简，抛出落在孟浩的面前，被孟浩接住。

“许师姐的洞府……这家伙也太走运了，那洞府据说灵气极为充足，整个宗门也没多少。”

“许师姐说是借，实际上这明显是给，只不过借之一字便可打消太多人的念头，看来这小子的送丹，起到了绝效。”

“该死的，我当年怎么没想到。”

与此同时，在这外宗旁的山峰上，那之前打赌的两个老者中穿着灰色长袍的高大老者，双眼猛地明亮，带着强烈的赞赏，哈哈大笑起来。

“这小家伙有意思，刚入宗门就知道找靠山，莫非这是本能不成，好好好，这才是领悟了我靠山宗的真意，此子不错，非常不错。”

孟浩很聪明吧，周一到了，诸位道友看在孟浩的聪明份上，推荐票，会员点击，还请给我！！第一周能不能封榜，就看这一天了！

加上这第三更，今天一共更新1万1千字，相当于四更，不求月票，只求免费的推荐票！！！很多道友已经注册会员，可却没有到500经验值，这个很简单，书评区有专门的帖子告诉大伙如何10分钟获得500经验值，随后就可以获得一张推荐票，全程10分钟左右，就可以来支持耳根，还请大家去看看这个帖子，耳根谢谢你们，谢谢，谢谢！至于龙套，书评区有专门的龙套楼，我保证会在结束前，争取让所有里面的角色出场，需要龙套的道友记得要去登记一下。"""


def test_ep5_declared_repeat_label_rebinds_to_unique_literal_evidence() -> None:
    """Real EP5 chapter text + the real 「男子」-bearing f entries must merge, not hard-fail."""
    records = portraits._current_identity_evidence_records(_EP5_CHAPTER5_TEXT)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    assert "男子" in evidence_by_ref["E039"]["text"]
    assert "男子" not in evidence_by_ref["E021"]["text"]
    assert "男子" not in evidence_by_ref["E038"]["text"]

    # Verbatim f array from provider_calls id=7252 (real production response).
    payload = {"k": [], "n": [], "f": [
        {"evidence_ref": "E002", "functional_identity_key": "F1", "kind": "mentioned", "source_label": "上官师叔"},
        {"evidence_ref": "E002", "functional_identity_key": "F2", "kind": "mentioned", "source_label": "许师姐"},
        {"evidence_ref": "E002", "functional_identity_key": "F3", "kind": "mentioned", "source_label": "陈师兄"},
        {"evidence_ref": "E003", "functional_identity_key": "F4", "kind": "mentioned", "source_label": "韩宗师兄"},
        {"evidence_ref": "E003", "functional_identity_key": "F5", "kind": "mentioned", "source_label": "王腾飞师兄"},
        {"evidence_ref": "E009", "functional_identity_key": "F6", "kind": "mentioned", "source_label": "金袍老者"},
        {"evidence_ref": "E021", "functional_identity_key": "F2", "kind": "mentioned", "source_label": "女子"},
        {"evidence_ref": "E021", "functional_identity_key": "F3", "kind": "mentioned", "source_label": "男子"},
        {"evidence_ref": "E038", "functional_identity_key": "F2", "kind": "mentioned", "source_label": "许姓女子"},
        {"evidence_ref": "E038", "functional_identity_key": "F3", "kind": "mentioned", "source_label": "男子"},
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    assert errors == []
    by_label = {item["source_label"]: item for item in projected}
    assert set(by_label) == {
        "上官师叔", "许师姐", "陈师兄", "韩宗师兄", "王腾飞师兄",
        "金袍老者", "女子", "男子", "许姓女子",
    }
    merged = by_label["男子"]
    assert merged["identity_kind"] == "functional"
    assert merged["identity_group"] == "current-1:F3"
    assert merged["source_label_provenance"] == (
        portraits.CURRENT_IDENTITY_LITERAL_PROVENANCE
    )
    assert merged["source_segment_id"] == "SRC0039"
    assert "银袍" in merged["source_quote"] and "男子" in merged["source_quote"]
    # Both mis-citations (E021 and E038) rebind to the one real literal home
    # (E039), so they collapse onto that single receipt rather than staying
    # split across the two evidence ids the model actually cited.
    assert len(merged["source_evidence_receipts"]) == 1
    assert merged["source_evidence_receipts"][0]["source_segment_id"] == (
        "SRC0039"
    )


def test_two_distinct_people_same_label_different_key_backfills_disambiguating_qualifier() -> None:
    """第31轮 ERR-20260824-614276 方向变更（真实 EP5 回归："老者"F3/F4）：
    这条测试曾经是"不同 F 键必须继续致命"的反向护栏，现在反过来——模型用
    两个不同的 functional_identity_key（F1/F2）正确各自引用了各自的逐字
    证据，这正是模型自己已经做出的"这是两个人"的结构性判断，字符串标签
    的欠额（都没填 scope_qualifier）现在被确定性补足，不再拒绝重来。
    按各自证据的首现顺序（E001 在前）用 _identity_disambiguating_suffix
    盖上甲/乙，标记 synthesized（观测用）。跟马脸青年案①②分支完全不冲突
    ——那两支处理的是"同一个复合键内部"的雷同/矛盾判定，这里处理的是
    "复合键本身该不该被拆开"。"""
    text = "一个男子站在桥头，神色警惕。\n\n远处又有一个男子骑马而来，衣着截然不同。"
    records = portraits._current_identity_evidence_records(text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    payload = {"k": [], "n": [], "f": [
        {"evidence_ref": "E001", "source_label": "男子", "functional_identity_key": "F1", "kind": "onscreen"},
        {"evidence_ref": "E002", "source_label": "男子", "functional_identity_key": "F2", "kind": "onscreen"},
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    matches = sorted(
        (item for item in projected if item["source_label"] == "男子"),
        key=lambda item: item["_current_response_group_key"],
    )
    assert len(matches) == 2
    assert [item["scope_qualifier"] for item in matches] == ["甲", "乙"]
    assert all(
        item["_current_identity_synthesized_qualifier"] is True for item in matches
    )
    # 复合键随即互不相同，两条各自独立成组，identity_group 允许不同（不再
    # 触发"多个 identity_group"这条既有护栏——那是给"同一个人却报了两个
    # identity_group"设计的，两个不同的人本来就该有两个 identity_group）。
    assert len({item["identity_group"] for item in matches}) == 2


# ---------------------------------------------------------------------------
# 复合键在，模型没用：申报字段雷同的重复归一 vs 真分歧升级反馈（真实第27轮
# EP3 回归 ERR-20260824-079190，provider_calls.id=9134）。取证：模型两次
# 声明"马脸青年"，source_label/scope_qualifier/kind/functional_identity_key
# 逐字段完全相同（已按 prompt 规则6声明"同一 functional_identity_key=
# 同一人"）——其中一次引用的证据段（E014："惨叫传出屋舍外……"）本身不含
# "马脸青年"字面，而这个词在全批 84 段里出现在 6 处不同段落，自动改写要求
# 全批唯一命中才敢做，6 处不算唯一，不触发，这次遂被判成 synthetic；另一次
# （E062："那仿佛日夜都盘膝坐在大石上的马脸青年……"）本身逐字含"马脸青年"，
# 判成 literal。identity_group/source_label_provenance 这两个后端为"这次
# 具体出现"单独推导的字段因此不一致，触发旧版"source_label 重复"致命——
# 但模型自己的申报内容从未分歧，这是后端证据判定的偶然结果，不是模型的
# 自相矛盾。
# ---------------------------------------------------------------------------

def test_current_identity_declared_duplicate_with_literal_anchor_normalizes() -> None:
    """红灯 a：两条"马脸青年"申报字段逐字段相等（真实第27轮 EP3 回归最小
    复现），其中一条的证据段本身不含字面、该词在全批里另有两处不相关的
    独立出现（自动改写因此不触发），另一条证据段本身逐字含"马脸青年"——
    申报内容逐字段相等 + 至少一条有逐字锚定，必须确定性归一为一条，
    errors==[]，且归一计数（_current_identity_normalized_duplicate 标记）
    体现为 1。"""
    text = (
        "惨叫传出屋舍外，四周杂役纷纷侧目。\n\n"
        "那马脸青年缓缓睁开双眼，看了一眼四周。\n\n"
        "清晨时分，马脸青年的声音再度响起，语气冷漠。"
    )
    records = portraits._current_identity_evidence_records(text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    payload = {"k": [], "n": [], "f": [
        {
            "evidence_ref": "E001", "source_label": "马脸青年",
            "functional_identity_key": "F4", "kind": "onscreen",
            "scope_qualifier": "北区杂役处负责管理杂役的马脸修士",
        },
        {
            "evidence_ref": "E002", "source_label": "马脸青年",
            "functional_identity_key": "F4", "kind": "onscreen",
            "scope_qualifier": "北区杂役处负责管理杂役的马脸修士",
        },
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    matches = [item for item in projected if item["source_label"] == "马脸青年"]
    assert len(matches) == 1
    assert matches[0]["_current_identity_normalized_duplicate"] is True
    normalized_duplicate_declarations = sum(
        1 for item in projected
        if item.get("_current_identity_normalized_duplicate")
    )
    assert normalized_duplicate_declarations == 1


def test_current_identity_declared_conflict_stays_fatal_with_side_by_side_diff() -> None:
    """红灯 b（第31轮 ERR-20260824-614276 收口：同 F 键内部字段分歧，
    "马脸青年案②分支不动"）：两条"马脸青年"共用**同一个**
    functional_identity_key（F4——模型自己申报"这是同一个人"），kind 却
    自相矛盾（onscreen vs mentioned），申报字段本身就不一致，真的没法
    确定是不是同一个人——这跟"老者"F3/F4 案不是同一个形状：那边是不同
    F 键（模型已经区分出两个人，缺的只是限定语），这边是同一个 F 键却
    自相矛盾（模型对"是不是同一个人"这件事本身前后矛盾），必须维持致命，
    且错误信息并排列出两条各自的申报内容，并给出确定性的修复指令（多人
    -> 各自 scope_qualifier；同一人 -> 合并共用同一个 ID），不能只甩一个
    错误码。（这条测试在第31轮之前用的是不同 F 键 F4/F5 的夹具——那个
    形状现在被第31轮的确定性补足机制正确地救回来了，不再致命，见
    test_two_distinct_people_same_label_different_key_backfills_
    disambiguating_qualifier；本测试改用真正的"同 F 键"夹具，继续守住
    真矛盾这条线。）
    """
    # E001（逐字含"马脸青年"，本条引用，kind=onscreen）/E002（不含
    # "马脸青年"，本条引用，kind=mentioned）/E003（逐字含"马脸青年"，本批
    # 未引用——存在这一段是为了让"马脸青年"在全批里有两处逐字出现，全批
    # 唯一命中改绑因此不触发，E002 保持 synthetic，不会被悄悄救回成逐字）。
    text = (
        "那马脸青年缓缓睁开双眼，看了一眼四周。\n\n"
        "屋外传来一阵脚步声，气氛顿时紧张起来。\n\n"
        "又一位马脸青年策马而来，神情冷漠。"
    )
    records = portraits._current_identity_evidence_records(text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    payload = {"k": [], "n": [], "f": [
        {
            "evidence_ref": "E001", "source_label": "马脸青年",
            "functional_identity_key": "F4", "kind": "onscreen",
        },
        {
            "evidence_ref": "E002", "source_label": "马脸青年",
            "functional_identity_key": "F4", "kind": "mentioned",
        },
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("source_label 重复" in error for error in errors)
    conflict_messages = [
        error for error in errors if "冲突内容并排对比" in error
    ]
    assert conflict_messages, "必须并排列出冲突内容，不能只甩错误码"
    diff_message = conflict_messages[0]
    assert '"functional_identity_key":"F4"' in diff_message
    assert diff_message.count('"functional_identity_key":"F4"') == 2
    assert '"kind":"onscreen"' in diff_message
    assert '"kind":"mentioned"' in diff_message
    assert "scope_qualifier" in diff_message
    assert "合并为一条" in diff_message


def test_current_identity_conflict_diff_survives_generic_semantic_repair_prompt(
    monkeypatch,
) -> None:
    """红灯 b（断言 prompt 构造）：并排冲突内容一旦流入
    model_gateway.chat_structured 已有的通用语义重试修复提示词机制
    （"只修复以下业务问题...当前候选..."），重试请求的 prompt 里必须
    完整带着这份并排对比，模型不会只收到一个孤零零的错误码。current
    identity 这条具体调用本身被 model_gateway 的 strict_identity_
    substage 校验显式禁止重试（"forbids structured retries"，见
    app/harness/model_gateway.py，本轮未改动这条既有护栏，保留其余
    ~44条既有规则不变的既定纪律）——这里用一个普通（非 identity-strict）
    stage_key 复现同一个通用修复提示词构造路径，验证"并排冲突内容一旦
    有机会进入任何重试 prompt，必定完整保留"这件事本身是成立的。
    """
    from pydantic import BaseModel

    class _Payload(BaseModel):
        value: int

    prompts: list[str] = []
    conflict_diff = (
        'source_label 重复：马脸青年；冲突内容并排对比：'
        '[{"kind":"onscreen","functional_identity_key":"F4"},'
        '{"kind":"mentioned","functional_identity_key":"F5"}]；'
        '若这几条指的是不同的人，请为每一条各自的 scope_qualifier 填写'
        '能互相区分的限定语；若指的是同一个人，请合并为一条并共用同一个'
        ' functional_identity_key（f 分支）或同一个 decision_id（k 分支）'
    )

    call_count = 0

    async def fake_chat(messages, **_kwargs):
        nonlocal call_count
        call_count += 1
        prompts.append(str(messages[0]["content"]))
        if call_count == 1:
            return json.dumps({"value": 1})
        return json.dumps({"value": 2})

    def flaky_validate(value: "_Payload") -> list[str]:
        return [] if value.value == 2 else [conflict_diff]

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    result = asyncio.run(model_gateway.chat_structured(
        [{"role": "user", "content": "resolve current identity"}],
        model_type=_Payload,
        validate=flaky_validate,
        operation_id="test.current-identity-conflict-diff:v1",
        max_tokens=128,
        format_retry_limit=0,
        semantic_retry_limit=1,
    ))

    assert result.value == 2
    assert call_count == 2
    assert conflict_diff in prompts[1], "冲突内容必须完整保留在重试 prompt 里"


# ---------------------------------------------------------------------------
# 真实第18轮 EP10 回归 ERR-20260824-b16bb4：「师弟」是关系称呼语，同一字符串
# 在本批合法指向不同人（不像"男子"这种通用描述，rule 4 要求模型改用更具体
# 的描述区分）——旧的裸 source_label 唯一键假设对关系称谓天然不成立。结构性
# 方案 a：唯一性判定键改为 (source_label, scope_qualifier) 复合键，模型按
# prompt 规则8申报区分限定语，不需要模型或校验代码认识"师弟"这个具体词形。
# ---------------------------------------------------------------------------

def test_relational_label_with_distinct_scope_qualifiers_resolves_as_two_people() -> None:
    """红灯（协调方点名，ERR-20260824-b16bb4 最小复现）：'师弟'在本批分别
    指两个不同的人，模型给了不同的 functional_identity_key，并按规则8各自
    申报了区分限定语（scope_qualifier）——修复后两个身份组各自正确成立，
    不再被硬拒；同一批次未申报限定语的既有场景（scope_qualifier 默认空串）
    完全不受影响，见上一条 reverse guard。"""
    text = "藏经阁前，一名师弟正在打扫。\n\n演武场上，另一名师弟正在练剑。"
    records = portraits._current_identity_evidence_records(text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    payload = {"k": [], "n": [], "f": [
        {
            "evidence_ref": "E001", "source_label": "师弟",
            "functional_identity_key": "F1", "kind": "onscreen",
            "scope_qualifier": "藏经阁前打扫的师弟",
        },
        {
            "evidence_ref": "E002", "source_label": "师弟",
            "functional_identity_key": "F2", "kind": "onscreen",
            "scope_qualifier": "演武场上练剑的师弟",
        },
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    assert len(projected) == 2
    groups = {item["identity_group"] for item in projected}
    assert len(groups) == 2
    labels = {item["source_label"] for item in projected}
    assert labels == {"师弟"}


def test_scope_qualifier_allows_natural_punctuation_but_rejects_runaway_length() -> None:
    """红灯（真实第19轮 EP1 回归）：scope_qualifier 之前照抄了 source_label
    的分隔符禁令，但 scope_qualifier 只作为 by_label 分组的 Python 元组键
    第二个元素，从没被拼进任何身份列表字符串——分隔符禁令在这里是误套
    （跟当年 source_label max_length=16 误伤自然语言值同一类错误）。真实值
    "县城木匠铺王伯，王有材的父亲"这类带逗号顿号的自然限定语必须放行；
    超长（>64字符）的整段抄录级失控值仍必须拒绝——这条约束只保留防御性
    长度上限，不检查标点。"""
    natural_qualifier = "县城木匠铺王伯，王有材的父亲"
    decision = portraits.CurrentFunctionalIdentityDecision.model_validate({
        "evidence_ref": "E001", "source_label": "王伯",
        "functional_identity_key": "F1", "kind": "onscreen",
        "scope_qualifier": natural_qualifier,
    })
    assert decision.scope_qualifier == natural_qualifier

    runaway_qualifier = "王" * 65
    with pytest.raises(ValidationError):
        portraits.CurrentFunctionalIdentityDecision.model_validate({
            "evidence_ref": "E001", "source_label": "王伯",
            "functional_identity_key": "F1", "kind": "onscreen",
            "scope_qualifier": runaway_qualifier,
        })


def test_cross_batch_projection_does_not_conflict_on_distinct_scope_qualifiers() -> None:
    """红灯（真实第20轮 EP4 回归 ERR-20260824-407c9b）：长章节按证据切成多批
    分别调用模型，两个不同的"外宗弟子"（不同 scope_qualifier、不同
    identity_group）如果恰好落在不同批次——_project_current_identity_
    response 单批内已经用 (source_label, scope_qualifier) 复合键正确放行，
    但 _current_identity_projection_errors 这道独立的跨批一致性检查之前
    还在按裸 source_label 分组，会把上游已经合法区分开的两个人重新判成
    "同一 source_label 冲突"，硬拦整集。修复后两者互不冲突；裸 source_label
    仍然相同这件事本身不构成矛盾信号。"""
    candidates = [
        {
            "source_label": "外宗弟子", "scope_qualifier": "甲：负责搬运丹药的那位",
            "identity_group": "current-1:F1", "identity_kind": "functional",
            "name": "",
        },
        {
            "source_label": "外宗弟子", "scope_qualifier": "乙：把守宗门大门的那位",
            "identity_group": "current-2:F1", "identity_kind": "functional",
            "name": "",
        },
    ]
    errors = portraits._current_identity_projection_errors(candidates)
    assert errors == []


def test_cross_batch_projection_still_rejects_genuine_conflict() -> None:
    """反向护栏：同一 (source_label, scope_qualifier) 复合键在不同批次里被
    判成不同 identity_group/name，仍然是真矛盾，必须继续拦截——本次修复
    只放宽了"裸 label 相同、qualifier 不同"的假冲突，不豁免真冲突。"""
    candidates = [
        {
            "source_label": "外宗弟子", "scope_qualifier": "甲：负责搬运丹药的那位",
            "identity_group": "current-1:F1", "identity_kind": "functional",
            "name": "",
        },
        {
            "source_label": "外宗弟子", "scope_qualifier": "甲：负责搬运丹药的那位",
            "identity_group": "current-2:F9", "identity_kind": "functional",
            "name": "",
        },
    ]
    errors = portraits._current_identity_projection_errors(candidates)
    assert any("外宗弟子" in message for message in errors)


def test_route_name_collision_gets_deterministic_disambiguating_suffix() -> None:
    """红灯（真实第20轮 EP4 回归，manifest/群演衔接处）：两个不同的
    identity_group 都退回同一个裸功能性标签（"外宗弟子"）当 route_name 时，
    第二个及以后的申领者必须带确定性后缀区分，不能让 route_name 字符串
    本身变得完全相同——下游 functional_extras 按这个字符串聚合 event_ids，
    字符串相同就会把两个不同的人的出场事件悄悄合并成一条群演记录。"""
    assert portraits._identity_disambiguating_suffix(1) == "甲"
    assert portraits._identity_disambiguating_suffix(2) == "乙"
    assert portraits._identity_disambiguating_suffix(3) == "丙"
    # 十天干用尽后退化为阿拉伯数字，确定性且互不相同，不会自己再撞车。
    assert portraits._identity_disambiguating_suffix(11) == "11"
    assert len({portraits._identity_disambiguating_suffix(i) for i in range(1, 20)}) == 19


def test_structured_semantic_error_classifies_as_quality_gate_not_system() -> None:
    """协调方顺带指令，ERR-20260824-b16bb4：identity.current 校验失败时抛出
    的 app.harness.model_gateway.StructuredSemanticError 之前没有被
    app.errors.classify 特殊处理，落到通用 SYS 兜底——用户看到"系统内部
    错误"，而实际失败的是我们自己的业务校验（供应商调用本身是成功的）。
    应与 PrepPackGateError 同待遇，归 quality_gate 类展示真实原因。"""
    exc = model_gateway.StructuredSemanticError("source_label 重复：师弟")
    assert app_errors.classify(exc) == ("quality_gate", "QA")


def test_declared_repeat_label_with_ambiguous_literal_home_still_hard_fails() -> None:
    """Reverse guard: the repair only fires for an UNAMBIGUOUS literal anchor.

    Both entries declare the same functional_identity_key (claiming one
    person), but the label is genuinely ambiguous in this batch -- it occurs
    literally in two different places, and neither cited E contains it. The
    fix must not silently guess which one is meant; this stays a hard
    failure exactly like before the EP5 fix.
    """
    text = "一个男子站在桥头。\n\n远处又有一个男子骑马而来。\n\n孟浩打了个哈欠。"
    records = portraits._current_identity_evidence_records(text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    payload = {"k": [], "n": [], "f": [
        {"evidence_ref": "E003", "source_label": "男子", "functional_identity_key": "F3", "kind": "mentioned"},
        {"evidence_ref": "E003", "source_label": "男子", "functional_identity_key": "F3", "kind": "mentioned"},
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert any("source_label 重复" in error for error in errors)


# --- ERR-20260821-f3e065 -----------------------------------------------------
# run_d05fc5539edf asked for a strict json_schema response_format and the
# provider returned a body that derailed mid-object into `{"decision_id": "f" :
# [`, so no JSON object ever decoded.  Under the strict identity contract that
# single corrupt sample failed the whole episode.  A response that was never
# delivered carries no identity judgement to preserve, so it -- and only it --
# is resampled once under a fresh operation id.


class _IdentityResampleShape(BaseModel):
    name: str


def test_undelivered_identity_answer_is_resampled_once(monkeypatch) -> None:
    operation_ids: list[str] = []
    attempts: list[int] = []

    async def fake_structured(_messages, **kwargs):
        operation_ids.append(kwargs["operation_id"])
        attempts.append(kwargs["call_meta"]["resample_attempt"])
        if len(operation_ids) == 1:
            error = model_gateway.StructuredFormatError("derailed mid-object")
            error.unparseable = True
            raise error
        return "recovered"

    monkeypatch.setattr(model_gateway, "chat_structured", fake_structured)

    result = asyncio.run(portraits._identity_structured_with_resample(
        [{"role": "user", "content": "prompt"}],
        model_type=_IdentityResampleShape,
        validate=lambda _value: [],
        max_tokens=256,
        operation_id_for_attempt=lambda attempt: f"op:{attempt}",
        call_meta={"stage_key": "screenplay_character_discovery"},
    ))

    assert result == "recovered"
    assert attempts == [0, 1]
    # A distinct operation id keeps cost/idempotency accounting exact.
    assert operation_ids == ["op:0", "op:1"]
    assert len(operation_ids) == (
        portraits.IDENTITY_UNUSABLE_RESPONSE_RESAMPLES + 1
    )


def test_authored_but_schema_invalid_identity_answer_is_never_resampled(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_structured(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        error = model_gateway.StructuredFormatError("wrong shape")
        error.unparseable = False
        raise error

    monkeypatch.setattr(model_gateway, "chat_structured", fake_structured)

    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(portraits._identity_structured_with_resample(
            [{"role": "user", "content": "prompt"}],
            model_type=_IdentityResampleShape,
            validate=lambda _value: [],
            max_tokens=256,
            operation_id_for_attempt=lambda attempt: f"op:{attempt}",
            call_meta={"stage_key": "screenplay_character_discovery"},
        ))

    assert calls == 1


def test_semantically_invalid_identity_answer_is_never_resampled(
    monkeypatch,
) -> None:
    calls = 0

    async def fake_structured(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        raise model_gateway.StructuredSemanticError("current named 缺少逐字 owned evidence")

    monkeypatch.setattr(model_gateway, "chat_structured", fake_structured)

    with pytest.raises(model_gateway.StructuredSemanticError):
        asyncio.run(portraits._identity_structured_with_resample(
            [{"role": "user", "content": "prompt"}],
            model_type=_IdentityResampleShape,
            validate=lambda _value: [],
            max_tokens=256,
            operation_id_for_attempt=lambda attempt: f"op:{attempt}",
            call_meta={"stage_key": "screenplay_character_discovery"},
        ))

    assert calls == 1


def test_two_undelivered_identity_answers_still_fail(monkeypatch) -> None:
    calls = 0

    async def fake_structured(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        error = model_gateway.StructuredFormatError("derailed again")
        error.unparseable = True
        raise error

    monkeypatch.setattr(model_gateway, "chat_structured", fake_structured)

    with pytest.raises(model_gateway.StructuredFormatError):
        asyncio.run(portraits._identity_structured_with_resample(
            [{"role": "user", "content": "prompt"}],
            model_type=_IdentityResampleShape,
            validate=lambda _value: [],
            max_tokens=256,
            operation_id_for_attempt=lambda attempt: f"op:{attempt}",
            call_meta={"stage_key": "screenplay_character_discovery"},
        ))

    assert calls == portraits.IDENTITY_UNUSABLE_RESPONSE_RESAMPLES + 1


def test_gateway_tags_corrupt_and_schema_invalid_responses_differently(
    monkeypatch,
) -> None:
    class _Shape(BaseModel):
        name: str

    async def corrupt(*_args, **_kwargs):
        return '{"name" "missing colon"}'

    async def wrong_shape(*_args, **_kwargs):
        return '{"unrelated": 1}'

    monkeypatch.setattr(model_gateway, "chat", corrupt)
    with pytest.raises(model_gateway.StructuredFormatError) as corrupt_error:
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "p"}],
            model_type=_Shape,
            validate=lambda _value: [],
            operation_id="op-corrupt",
            max_tokens=256,
            format_retry_limit=0,
            semantic_retry_limit=0,
        ))
    assert corrupt_error.value.unparseable is True

    monkeypatch.setattr(model_gateway, "chat", wrong_shape)
    with pytest.raises(model_gateway.StructuredFormatError) as shape_error:
        asyncio.run(model_gateway.chat_structured(
            [{"role": "user", "content": "p"}],
            model_type=_Shape,
            validate=lambda _value: [],
            operation_id="op-shape",
            max_tokens=256,
            format_retry_limit=0,
            semantic_retry_limit=0,
        ))
    assert shape_error.value.unparseable is False


def _future_identity_catalog(prompt: str, marker: str) -> list[dict]:
    """Return one backend-owned catalog exactly as the provider receives it."""
    body = prompt.split(marker, 1)[1]
    body = body[body.index("["):]
    depth = 0
    for index, char in enumerate(body):
        depth += (char == "[") - (char == "]")
        if depth == 0:
            return json.loads(body[: index + 1])
    raise AssertionError(f"未找到闭合的目录：{marker}")


_REVEAL_FUTURE_TEXT = (
    "“好人啊，孟浩，你走了这么久，依旧每天回来偷偷帮我砍柴，"
    "孟浩，你是我李富贵这一辈子的好朋友。”小胖子感慨连连。\n\n"
    "孟浩在不远处的丛林内听到这些话，愣在那里。"
)
_REVEAL_CANDIDATES = [{
    "name": "小胖子",
    "source_label": "小胖子",
    "identity_kind": "functional",
    "identity_group": "current-1:F3",
    "kind": "onscreen",
}]


def _reveal_bible() -> Bible:
    return Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="李富贵",
            role="重要配角",
            appearance_canonical="圆脸胖少年，粗麻长衫，门牙醒目",
        )],
    )


def _resolve_reveal() -> list[dict]:
    return asyncio.run(portraits.resolve_future_identity_candidates(
        [dict(item) for item in _REVEAL_CANDIDATES],
        source_text="小胖子在通铺上打呼噜。",
        future_text=_REVEAL_FUTURE_TEXT,
        bible=_reveal_bible(),
        episode_no=2,
        future_label="第 3-12 章",
    ))


def test_future_identity_offers_k_for_canonical_name_in_group_evidence(
    monkeypatch,
) -> None:
    """A Bible authority without aliases must still be selectable as known.

    Production regression: every Bible-seeded authority starts with an empty
    alias list, so requiring a *non-canonical* registered alias meant no K
    decision was ever minted.  The one correct answer was unrepresentable and
    the episode died on the NEW rule instead.
    """
    seen: dict[str, list] = {}

    async def known_wire(messages, **kwargs):
        prompt = str(messages[0]["content"])
        seen["decisions"] = _future_identity_catalog(prompt, "可选决议目录")
        enum = kwargs["response_format"]["json_schema"]["schema"][
            "properties"]["decisions"]["properties"]["G001"]["enum"]
        seen["enum"] = list(enum)
        return json.dumps({
            "decisions": {
                "G001": next(v for v in enum if v.startswith("K:")),
            },
            "revealed_names": {"G001": ""},
            "revealed_name_kinds": {"G001": ""},
            "reveal_evidence_ids": {"G001": ""},
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", known_wire)
    resolved = _resolve_reveal()

    known = [
        decision for decision in seen["decisions"]
        if decision["resolution_kind"] == "known_named"
    ]
    assert [decision["authority_id"] for decision in known] == ["bible:李富贵"]
    assert known[0]["proof_kind"] == "canonical_name"
    assert known[0]["proof_anchors"] == ["李富贵"]
    assert resolved[0]["name"] == "李富贵"
    assert resolved[0]["authority_id"] == "bible:李富贵"
    assert resolved[0]["identity_kind"] == "named"


def test_future_identity_new_naming_existing_authority_becomes_that_k(
    monkeypatch,
) -> None:
    """NEW carrying an existing canonical name is that authority's K decision.

    Every fact the K decision requires -- this group, this authority, this
    backend-owned evidence span, the anchor verbatim inside it -- is already
    present, so the answer is canonicalised onto the backend token instead of
    failing the episode.
    """
    calls = 0

    async def new_wire(messages, **kwargs):
        nonlocal calls
        calls += 1
        prompt = str(messages[0]["content"])
        evidence = _future_identity_catalog(prompt, "后续证据目录")
        anchored = next(
            item for item in evidence if "李富贵" in item["text"]
        )
        enum = kwargs["response_format"]["json_schema"]["schema"][
            "properties"]["decisions"]["properties"]["G001"]["enum"]
        return json.dumps({
            "decisions": {
                "G001": next(v for v in enum if v.startswith("N:")),
            },
            "revealed_names": {"G001": "李富贵"},
            "revealed_name_kinds": {"G001": "personal_name"},
            "reveal_evidence_ids": {"G001": anchored["evidence_id"]},
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", new_wire)
    resolved = _resolve_reveal()

    assert calls == 1
    assert resolved[0]["name"] == "李富贵"
    assert resolved[0]["authority_id"] == "bible:李富贵"
    assert resolved[0]["identity_kind"] == "named"
    assert "李富贵" in str(resolved[0]["future_evidence"])


def test_future_identity_new_name_without_backend_decision_stays_closed(
    monkeypatch,
) -> None:
    """No backend K decision, no rewrite: the NEW rule still fails closed."""

    async def unanchored_wire(_messages, **kwargs):
        enum = kwargs["response_format"]["json_schema"]["schema"][
            "properties"]["decisions"]["properties"]["G001"]["enum"]
        evidence_enum = kwargs["response_format"]["json_schema"]["schema"][
            "properties"]["reveal_evidence_ids"]["properties"]["G001"]["enum"]
        return json.dumps({
            "decisions": {
                "G001": next(v for v in enum if v.startswith("N:")),
            },
            "revealed_names": {"G001": "李富贵"},
            "revealed_name_kinds": {"G001": "personal_name"},
            "reveal_evidence_ids": {
                "G001": next(v for v in evidence_enum if v),
            },
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", unanchored_wire)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="NEW 不得重新签发已有 authority",
    ):
        asyncio.run(portraits.resolve_future_identity_candidates(
            [dict(item) for item in _REVEAL_CANDIDATES],
            source_text="小胖子在通铺上打呼噜。",
            future_text="小胖子第二天照常去砍柴，没有人叫破他的名字。",
            bible=_reveal_bible(),
            episode_no=2,
        ))


# ---------------------------------------------------------------------------
# NEW 重签已有 authority 的归一规则（真实第26轮 EP5 回归 ERR-20260824-88ece5）：
# 门禁立意（防重复铸造身份）没错，错在对"多报"的响应形态——模型把"引用
# 已有身份"误说成 NEW（G001/G002 两例），是因为这个 group 自己被后端按
# proof_anchors 预筛出的那批 evidence 里恰好没有这个 authority 的 K 选项，
# 不是模型编造。按门禁不对称教义（缺失致命、冗余归一、矛盾致命）：
#   a) authority_ids 唯一命中 + 真名整体确实逐字出现在这个 group 能看到的
#      完整 future_text 里（不要求命中后端预筛的那个更窄子集）= 冗余，
#      归一为对既有身份的引用，不再要求重新逐字锚定（见
#      REISSUE_KNOWN_RESOLUTION_KIND）；
#   b) authority_ids 命中多个不同的 authority_id（同一个真名字符串被项目
#      内不同的身份记录分别持有）= 矛盾，无法确定性判断该并入哪一个，
#      维持致命；
#   c) 全新 authority（authority_ids 命中零个）且缺逐字真名锚 = 缺失，
#      逐字真名锚要求原样保留（既有测试保持，见本文件其它 NEW 缺锚测试，
#      未受本轮改动影响）。
# ---------------------------------------------------------------------------

def test_future_identity_new_reissue_normalizes_when_authority_grounded_elsewhere_in_future_text(
    monkeypatch,
) -> None:
    """红灯 a：模型选了 N: token 重签"李富贵"（已有 authority），引用的
    evidence_id 恰好不是后端按 proof_anchors 预筛出的那一条（模型引用了
    "小胖子"在后续章节里另一次出场的证据），但"李富贵"这个真名整体确实
    逐字出现在这个 group 能看到的完整 future_text 里——必须归一为对既有
    身份的引用（resolution_kind=reissue_known），不再要求这次也命中窄口径
    的逐字锚点，且计数体现为该条候选自带的 resolution_kind 标记
    （normalized_new_reissues 观测的枚举依据）。"""
    future_text = (
        "小胖子在山下练功，忽然想起故人往事。"
        "他低声自语：好人啊，孟浩，你走了这么久，依旧每天回来偷偷帮我砍柴，"
        "孟浩，你是我李富贵这一辈子的好朋友。\n\n"
        "又过了三日，小胖子仍旧每天在村口张望，盼着孟浩归来，却始终没有等到。"
    )
    candidates = [{
        "name": "小胖子",
        "source_label": "小胖子",
        "identity_kind": "functional",
        "identity_group": "current-1:F3",
        "kind": "onscreen",
    }]

    async def fake_chat(_messages, **kwargs):
        schema = kwargs["response_format"]["json_schema"]["schema"]
        enum = schema["properties"]["decisions"]["properties"]["G001"]["enum"]
        evidence_enum = schema["properties"]["reveal_evidence_ids"][
            "properties"]["G001"]["enum"]
        # 取最后一个非空 evidence_id——对应"又过了三日"那一段，不含"李富贵"，
        # 不是后端按 proof_anchors 锚定给"李富贵"的那一条。
        chosen = [value for value in evidence_enum if value][-1]
        return json.dumps({
            "decisions": {"G001": next(v for v in enum if v.startswith("N:"))},
            "revealed_names": {"G001": "李富贵"},
            "revealed_name_kinds": {"G001": "personal_name"},
            "reveal_evidence_ids": {"G001": chosen},
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        candidates,
        source_text="小胖子练功。",
        future_text=future_text,
        bible=_reveal_bible(),
        episode_no=2,
        future_label="后续章节",
    ))

    assert resolved[0]["name"] == "李富贵"
    assert resolved[0]["authority_id"] == "bible:李富贵"
    assert resolved[0]["identity_kind"] == "named"
    assert resolved[0]["resolution_kind"] == portraits.REISSUE_KNOWN_RESOLUTION_KIND
    normalized_new_reissues = sum(
        1 for item in resolved
        if item.get("resolution_kind") == portraits.REISSUE_KNOWN_RESOLUTION_KIND
    )
    assert normalized_new_reissues == 1


def test_future_identity_new_reissue_stays_fatal_on_ambiguous_authority_conflict(
    monkeypatch,
) -> None:
    """红灯 b：同一个真名字符串"李富贵"被项目内两个不同的 authority_id
    分别持有（Bible 里的"bible:李富贵"，以及本集另一条显式带
    authority_id 的具名候选"manual:duplicate-lifugui"）——authority_ids
    命中多个，无法确定性判断该并入哪一个，必须维持致命，不得被归一分支
    误吞。"""
    future_text = (
        "小胖子低声念叨：李富贵，你可千万要平安归来。\n\n"
        "远处又有个陌生的声音也喊了一声李富贵，众人一时分不清是谁在说话。"
    )
    candidates = [
        {
            "name": "小胖子",
            "source_label": "小胖子",
            "identity_kind": "functional",
            "identity_group": "current-1:F3",
            "kind": "onscreen",
        },
        {
            "name": "李富贵",
            "source_label": "另一位李富贵",
            "identity_kind": "named",
            "identity_group": "current-1:named-dup",
            "kind": "mentioned",
            "authority_id": "manual:duplicate-lifugui",
        },
    ]

    async def fake_chat(_messages, **kwargs):
        schema = kwargs["response_format"]["json_schema"]["schema"]
        enum = schema["properties"]["decisions"]["properties"]["G001"]["enum"]
        evidence_enum = schema["properties"]["reveal_evidence_ids"][
            "properties"]["G001"]["enum"]
        return json.dumps({
            "decisions": {"G001": next(v for v in enum if v.startswith("N:"))},
            "revealed_names": {"G001": "李富贵"},
            "revealed_name_kinds": {"G001": "personal_name"},
            "reveal_evidence_ids": {
                "G001": next(v for v in evidence_enum if v),
            },
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="NEW 不得重新签发已有 authority",
    ):
        asyncio.run(portraits.resolve_future_identity_candidates(
            candidates,
            source_text="小胖子练功。",
            future_text=future_text,
            bible=_reveal_bible(),
            episode_no=2,
            future_label="后续章节",
        ))


def test_registered_authority_without_literal_anchor_routes_to_functional(
    monkeypatch,
) -> None:
    """A Bible name the episode never writes must route to f, not to n.

    Production regression: the episode only ever says the appellation, so no K
    decision can be minted for that authority, while rule 2 said a registered
    identity may only enter through k.  The provider resolved the deadlock by
    writing the canonical name into n, which fails closed twice over and ends
    the episode.  The routing for that case is now stated explicitly.
    """
    prompts: list[str] = []

    async def fake_chat(messages, **kwargs):
        prompt = str(messages[0].get("content") or "")
        prompts.append(prompt)
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "许师姐",
                "identity_kind": "functional",
                "functional_identity_key": "F1",
                "kind": "mentioned",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    bible = Bible(
        world=World(visual_style_canonical="国风"),
        characters=[Character(
            name="许清",
            role="重要配角",
            appearance_canonical="银袍女子，黑发高髻，肤色惨白",
        )],
    )
    candidates = asyncio.run(portraits.discover_character_candidates(
        "许师姐与陈师兄是内门弟子，以往很少看到。",
        bible,
        5,
    ))

    prompt = prompts[0]
    # The authority is offered for recognition …
    assert "许清" in prompt.split("本批 backend-owned", 1)[0]
    # … but no K decision can be minted, because nothing anchors it verbatim.
    catalog = _future_identity_catalog(prompt, "本批已登记身份 K 决议目录")
    assert catalog == []
    # So the prompt must say where such an authority goes instead.
    assert "本批 K 目录没有为「当前人物谱已有角色」中的某人签发 decision_id" in prompt
    assert "只能把你实际读到的" in prompt

    assert [
        (item["source_label"], item["identity_kind"]) for item in candidates
    ] == [("许师姐", "functional")]


def test_registered_name_recognised_from_context_is_dropped_not_fatal(
    monkeypatch,
) -> None:
    """Recognising a registered person is not the same as reading their name.

    The episode only ever writes 「许师姐」, so no K decision can exist for
    「许清」 and the name is nowhere verbatim in the owned evidence.  The claim
    cannot become a new authority and carries no appellation to keep, so it is
    dropped -- the structural coverage audit still recovers the person from the
    wording the source actually uses.  Ending the whole episode over one such
    claim was the production failure.
    """
    calls = 0

    async def fake_chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "许清",
                "identity_kind": "named",
                "kind": "onscreen",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "许师姐与陈师兄是内门弟子，以往很少看到。",
        Bible(
            world=World(visual_style_canonical="国风"),
            characters=[Character(
                name="许清",
                role="重要配角",
                appearance_canonical="银袍女子，黑发高髻，肤色惨白",
            )],
        ),
        5,
    ))

    assert calls == 1
    # No duplicate authority is minted for the recognised person.
    assert not [
        item for item in candidates
        if str(item.get("name") or "") == "许清"
        and item.get("identity_kind") == "named"
    ]


def test_honorific_new_name_becomes_functional_not_a_new_authority(
    monkeypatch,
) -> None:
    """真名 > 尊称 > 代称: an honorific never mints a character card.

    Production: 「许师姐」 was issued as a brand-new authority ``bible:许师姐``
    beside the already-registered 「许清」, and the scene identity registry then
    failed closed because one appellation pointed at two canonical identities.
    """

    async def fake_chat(messages, **kwargs):
        return json.dumps(_identity_wire_for_call(
            kwargs,
            [{
                "source_label": "许师姐",
                "identity_kind": "named",
                "name_kind": "honorific",
                "kind": "onscreen",
            }],
            messages=messages,
        ), ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    candidates = asyncio.run(portraits.discover_character_candidates(
        "许师姐与陈师兄是内门弟子，以往很少看到。",
        Bible(world=World(visual_style_canonical="国风"), characters=[]),
        5,
    ))

    honorific = [
        item for item in candidates
        if str(item.get("source_label") or "") == "许师姐"
    ]
    assert honorific, candidates
    assert honorific[0]["identity_kind"] == "functional"
    assert not honorific[0].get("authority_id")


def test_identity_transport_stall_is_resampled_under_a_fresh_operation(
    monkeypatch,
) -> None:
    """A 0-character stall delivered no judgement, so it is not a re-roll.

    Production: one identity call stalled for 303s with received_chars=0 and
    ended the episode.  Nothing was authored, so there was no answer to
    preserve -- the same reasoning the blueprint shard path already applies.
    """
    operations: list[str] = []

    async def stalling_then_valid(_messages, **kwargs):
        operations.append(str(kwargs["call_meta"]["operation_id"]))
        if len(operations) == 1:
            raise hiagent.ProviderError(
                "流式调用read阶段超时（303131ms）",
                failure_kind="request_outcome_unknown",
                received_chars=0,
            )
        return json.dumps({"value": "ok"}, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", stalling_then_valid)

    class _Shape(BaseModel):
        value: str

    result = asyncio.run(portraits._identity_structured_with_resample(
        [{"role": "user", "content": "p"}],
        model_type=_Shape,
        validate=lambda _value: [],
        max_tokens=4096,
        operation_id_for_attempt=lambda attempt: (
            "op-identity" if not attempt else f"op-identity:resample:{attempt}"
        ),
        call_meta={"stage_key": "screenplay_character_discovery"},
        format_retry_limit=0,
        semantic_retry_limit=0,
    ))

    assert result.value == "ok"
    assert operations == ["op-identity", "op-identity:resample:1"]


def test_identity_partial_delivery_provider_error_still_fails_closed(
    monkeypatch,
) -> None:
    """Bytes on the wire mean an answer existed; it is never re-rolled."""
    calls = 0

    async def partially_delivered(_messages, **_kwargs):
        nonlocal calls
        calls += 1
        raise hiagent.ProviderError(
            "流式调用read阶段超时",
            failure_kind="request_outcome_unknown",
            received_chars=1200,
        )

    monkeypatch.setattr(model_gateway, "chat", partially_delivered)

    class _Shape(BaseModel):
        value: str

    with pytest.raises(hiagent.ProviderError):
        asyncio.run(portraits._identity_structured_with_resample(
            [{"role": "user", "content": "p"}],
            model_type=_Shape,
            validate=lambda _value: [],
            max_tokens=4096,
            operation_id_for_attempt=lambda attempt: f"op-partial-{attempt}",
            call_meta={"stage_key": "screenplay_character_discovery"},
            format_retry_limit=0,
            semantic_retry_limit=0,
        ))

    assert calls == 1


def test_future_honorific_reveal_is_demoted_to_functional(monkeypatch) -> None:
    """真名 > 尊称 > 代称 on the future wire, deterministically.

    Production EP1: the lookahead window only ever wrote 「许师姐」, the model
    issued it as a NEW authority, and 「许师姐」/「许清」 became two character
    cards for one person.  An honorific must resolve the group as a functional
    identity instead -- still a standalone identity, still claimable by the
    real name through a K decision once the source finally writes it.
    """
    seen: dict[str, object] = {}

    async def honorific_wire(messages, **kwargs):
        prompt = str(messages[0]["content"])
        evidence = _future_identity_catalog(prompt, "后续证据目录")
        anchored = next(item for item in evidence if "许师姐" in item["text"])
        schema = kwargs["response_format"]["json_schema"]["schema"]
        enum = schema["properties"]["decisions"]["properties"]["G001"]["enum"]
        seen["kinds_enum"] = list(
            schema["properties"]["revealed_name_kinds"]["properties"]["G001"]["enum"]
        )
        return json.dumps({
            "decisions": {
                "G001": next(v for v in enum if v.startswith("N:")),
            },
            "revealed_names": {"G001": "许师姐"},
            "reveal_evidence_ids": {"G001": anchored["evidence_id"]},
            "revealed_name_kinds": {"G001": "honorific"},
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", honorific_wire)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "银袍女子",
            "source_label": "银袍女子",
            "identity_kind": "functional",
            "identity_group": "current-1:F1",
            "kind": "onscreen",
        }],
        source_text="一个身穿银袍的女子站在广场上。",
        future_text="“许师姐好手段。”众人低声议论，那银袍女子并未回头。",
        bible=Bible(world=World(visual_style_canonical="国风"), characters=[]),
        episode_no=1,
        future_label="第 2-11 章",
    ))

    assert "honorific" in seen["kinds_enum"]
    assert resolved[0]["identity_kind"] == "functional"
    assert not resolved[0].get("authority_id")
    assert resolved[0]["name"] == "银袍女子"


def test_future_personal_name_reveal_still_mints_the_authority(
    monkeypatch,
) -> None:
    """A real name keeps working exactly as before."""

    async def personal_wire(messages, **kwargs):
        prompt = str(messages[0]["content"])
        evidence = _future_identity_catalog(prompt, "后续证据目录")
        anchored = next(item for item in evidence if "陈三" in item["text"])
        schema = kwargs["response_format"]["json_schema"]["schema"]
        enum = schema["properties"]["decisions"]["properties"]["G001"]["enum"]
        return json.dumps({
            "decisions": {
                "G001": next(v for v in enum if v.startswith("N:")),
            },
            "revealed_names": {"G001": "陈三"},
            "reveal_evidence_ids": {"G001": anchored["evidence_id"]},
            "revealed_name_kinds": {"G001": "personal_name"},
        }, ensure_ascii=False)

    monkeypatch.setattr(model_gateway, "chat", personal_wire)
    resolved = asyncio.run(portraits.resolve_future_identity_candidates(
        [{
            "name": "青衫少年",
            "source_label": "青衫少年",
            "identity_kind": "functional",
            "identity_group": "current-1:F1",
            "kind": "onscreen",
        }],
        source_text="一个青衫少年走进院子。",
        future_text="那青衫少年抬起头：“我叫陈三。”",
        bible=Bible(world=World(visual_style_canonical="国风"), characters=[]),
        episode_no=1,
        future_label="第 2-11 章",
    ))

    assert resolved[0]["identity_kind"] == "named"
    assert resolved[0]["name"] == "陈三"
    assert resolved[0]["authority_id"] == "bible:陈三"


def test_current_identity_repairs_zero_padding_slip_in_evidence_ref() -> None:
    """`E01` for a catalog holding `E001` is formatting, not a new decision.

    Production EP1: the provider ignored the closed enum and sent `E01`, and
    the whole episode failed on `current F evidence_ref 越界`.  Re-padding
    selects an existing backend-owned receipt, and only when exactly one
    matches.
    """
    records = portraits._current_identity_evidence_records(
        "绿袍修士站在山门。\n\n许师姐收起风幡。"
    )
    evidence_by_ref = {
        f"E{index:03d}": record
        for index, record in enumerate(records, start=1)
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate({
        "k": [],
        "n": [],
        "f": [{
            "evidence_ref": "E01",
            "source_label": "绿袍修士",
            "functional_identity_key": "F1",
            "kind": "onscreen",
        }],
    })

    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    assert errors == []
    assert [item["source_label"] for item in projected] == ["绿袍修士"]


def test_current_identity_ambiguous_ref_still_fails_closed() -> None:
    """Only an unambiguous re-padding is accepted."""
    evidence_by_ref = {
        "E001": {"text": "绿袍修士站在山门。", "evidence_id": "e1"},
        "EE01": {"text": "许师姐收起风幡。", "evidence_id": "e2"},
    }
    response = portraits.CurrentIdentityCandidateResponse.model_validate({
        "k": [],
        "n": [],
        "f": [{
            "evidence_ref": "E9",
            "source_label": "绿袍修士",
            "functional_identity_key": "F1",
            "kind": "onscreen",
        }],
    })

    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    assert any("evidence_ref 越界" in error for error in errors)


def _non_person_verdict(subject_kind: str, name: str) -> dict:
    """A card the model considers worth anchoring, for a thing that is not a person."""
    return {
        "subject_kind": subject_kind,
        "important": True,
        "reason": f"{name}是独立出场单元，需单独建卡保证一致性",
        "role": "重要配角",
        "appearance_canonical": (
            "青灰色石砌山门，飞檐上悬着铜铃，门楣刻着朱红宗名，常年云雾缭绕"
        ),
        "personality": "",
        "speech_style": "",
        "relationships": [],
    }


@pytest.mark.parametrize(
    ("subject_kind", "name"),
    [
        ("organization", "靠山宗"),
        ("object", "凝气卷"),
        ("place", "大青山"),
        ("other", "凝气三层"),
    ],
)
def test_non_person_never_enters_the_character_bible(
    monkeypatch, subject_kind: str, name: str,
) -> None:
    """人物谱只登记人。

    Production: 「靠山宗」 was carded with the model's own reason saying it is
    「独立的组织类出场单元」, and 「凝气卷」 with 「靠山宗发放的修行典籍」.  Both
    stated plainly that they are not people.  Nothing asked.
    """
    conn = _make_conn()
    _seed_project(conn, f"{name}矗立在山谷之中。" * 6)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*_args, **_kwargs):
        return _non_person_verdict(subject_kind, name)

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)

    result = asyncio.run(portraits.ensure_character_card("p1", name, 21))

    assert result["status"] == "skipped_not_person"
    assert result["subject_kind"] == subject_kind
    bible = json.loads(
        conn.execute(
            "SELECT bible_json FROM projects WHERE id='p1'"
        ).fetchone()["bible_json"]
    )
    assert name not in {item["name"] for item in bible["characters"]}


def test_confirmed_real_name_cannot_bypass_the_person_gate(monkeypatch) -> None:
    """身份消歧确认的是"稳定专名"，不是"这是一个人"。

    ``require_identity_card`` forces a complete card and skips the importance
    vote, which is exactly the branch a non-person used to slip through.
    """
    conn = _make_conn()
    _seed_project(conn, "靠山宗矗立在山谷之中。" * 6)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*_args, **_kwargs):
        return _non_person_verdict("organization", "靠山宗")

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)

    result = asyncio.run(portraits.ensure_character_card(
        "p1", "靠山宗", 21, require_identity_card=True,
    ))

    assert result["status"] == "skipped_not_person"
    bible = json.loads(
        conn.execute(
            "SELECT bible_json FROM projects WHERE id='p1'"
        ).fetchone()["bible_json"]
    )
    assert "靠山宗" not in {item["name"] for item in bible["characters"]}


def test_card_role_must_come_from_the_declared_enum() -> None:
    """role 是闭合枚举，不是自由文本。

    「靠山宗」's persisted card carried role="重要场景载体", a value the contract
    never allowed; only a non-empty check stood between it and the bible.
    """
    assert portraits.CHARACTER_CARD_ROLES == ("主角", "重要配角", "反派")
    assert "重要场景载体" not in portraits.CHARACTER_CARD_ROLES
    assert portraits.CHARACTER_SUBJECT_PERSON in portraits.CHARACTER_SUBJECT_KINDS


def test_author_pen_name_is_recorded_as_not_a_character(monkeypatch) -> None:
    """卡层拒绝建卡后，该判定必须持久化，否则 coverage 仍会索要人物卡。

    Production EP3: 「耳根」 is the author's pen name in the chapter's closing
    note.  The card layer correctly declined it, and structural coverage then
    failed the whole episode with 「结构人物 coverage 的 named card 尚未物化：耳根」.
    ``ensure_cards_for_text`` copies its candidate dicts and coverage reads
    persisted artifacts, so an in-memory demotion alone would not reach it.
    """
    conn = _make_conn()
    _seed_project(conn, "耳根在这里恭喜柚子，求推荐票。" * 6)
    _patch_settings(monkeypatch, conn)

    async def fake_assess(*_args, **_kwargs):
        return {
            "subject_kind": "other",
            "important": False,
            "reason": "作者笔名，出现在章末互动旁白，并非故事角色",
            "role": "重要配角",
            "appearance_canonical": "",
            "personality": "",
            "speech_style": "",
            "relationships": [],
        }

    monkeypatch.setattr(portraits, "assess_new_character", fake_assess)

    result = asyncio.run(portraits.ensure_character_card(
        "p1", "耳根", 21, require_identity_card=True,
    ))

    assert result["status"] == "skipped_not_person"
    assert portraits.get_setting(
        portraits._non_character_skip_key("p1", "耳根")
    ) == "1"
    bible = json.loads(
        conn.execute(
            "SELECT bible_json FROM projects WHERE id='p1'"
        ).fetchone()["bible_json"]
    )
    assert "耳根" not in {item["name"] for item in bible["characters"]}


def test_redundant_evidence_ref_on_a_k_decision_is_dropped() -> None:
    """K 决议的 token 已经绑定证据，回声字段不该让整集格式失败。

    Production EP4: ``k.0.evidence_ref Extra inputs are not permitted`` —
    the model echoed a field the backend already owns, and the strict
    ``extra="forbid"`` shape turned that into a whole-episode failure.
    """
    payload = {
        "k": [{
            "decision_id": "K:E002:abc",
            "kind": "onscreen",
            "evidence_ref": "E002",
        }],
        "n": [],
        "f": [],
    }

    normalized = portraits._normalize_current_identity_payload(payload)

    assert normalized["k"] == [{"decision_id": "K:E002:abc", "kind": "onscreen"}]
    portraits.CurrentIdentityCandidateResponse.model_validate(normalized)


def test_unknown_fields_on_n_and_f_still_fail_closed() -> None:
    """只清理后端本就拥有的字段；N/F 携带模型创作内容，不得替它猜。"""
    payload = {
        "k": [],
        "n": [{
            "evidence_ref": "E001",
            "identity_label": "陈三",
            "name_kind": "personal_name",
            "kind": "onscreen",
            "unexpected": "x",
        }],
        "f": [],
    }

    normalized = portraits._normalize_current_identity_payload(payload)

    assert normalized["n"][0]["unexpected"] == "x"
    with pytest.raises(ValidationError):
        portraits.CurrentIdentityCandidateResponse.model_validate(normalized)


def test_identity_discovery_failure_gets_dedicated_no_retry_budget_hint() -> None:
    """身份判定 format_retry_limit=semantic_retry_limit=0（fail-closed，见
    app/portraits.py 身份判定调用点与 _current_identity_projection_errors 附近
    注释），所以它触发的 StageError 必须走专用报错分类，而不是共享的
    "generation" 分类——共享分类的提示会建议用户"调整「修复重试上限」"，
    但那个设置对这条 fail-closed 路径完全无效，会误导用户。

    app/domain/screenplay_ops.py 的 ``_screenplay_character_discovery`` 用
    ``[IDENTITY_DISCOVERY_FIXED_RETRY_BUDGET]`` 标记这类失败；这里只验证
    分类结果与专用提示文案，不驱动完整的身份判定流程。
    """
    from app import errors as app_errors

    exc = stages.StageError(
        "新人物发现",
        [
            "人物身份模型暂未完成本集预检，请在剧本阶段重试（ERR-x）"
            "[IDENTITY_DISCOVERY_FIXED_RETRY_BUDGET]"
        ],
    )

    assert app_errors.classify(exc) == (
        "generation_identity_fixed_budget",
        "GEN-IDENTITY-BUDGET",
    )
    hint = app_errors.CATEGORIES["generation_identity_fixed_budget"]["hint"]
    assert "修复重试上限" in hint and "无效" in hint

    # 未命中标记的普通 StageError 必须继续走既有的共享 "generation" 分类，
    # 不能被这条新分支误伤。
    generic_exc = stages.StageError("新人物发现", ["普通失败，无特殊标记"])
    assert app_errors.classify(generic_exc) == ("generation", "GEN")


# ---------------------------------------------------------------------------
# 第31轮 EP5 真实回归 ERR-20260824-614276："老者"F3/F4，空限定语双条。
# ---------------------------------------------------------------------------

def test_ep5_two_elders_same_label_different_functional_key_backfills_qualifiers() -> None:
    """红灯 a（第31轮 ERR-20260824-614276 真实最小复现）：第 5 章确实有两位
    "老者"，模型用不同 functional_identity_key（F3/F4）明确申报了两个人，
    区分的事实判断已经做出，只是没填 scope_qualifier——必须确定性补足为
    甲/乙，两个身份组独立成立，不得拒绝重来。"""
    text = (
        "石台上端坐着一位老者，白发苍苍，神情威严。\n\n"
        "台阶另一侧，又有一位老者缓缓踱步，面容清癯，气息迥异于前者。"
    )
    records = portraits._current_identity_evidence_records(text)
    evidence_by_ref = {
        f"E{index:03d}": record for index, record in enumerate(records, start=1)
    }
    payload = {"k": [], "n": [], "f": [
        {
            "evidence_ref": "E001", "source_label": "老者",
            "functional_identity_key": "F3", "kind": "onscreen",
        },
        {
            "evidence_ref": "E002", "source_label": "老者",
            "functional_identity_key": "F4", "kind": "onscreen",
        },
    ]}
    response = portraits.CurrentIdentityCandidateResponse.model_validate(payload)
    projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions={},
        reserved_authority_labels=set(),
        group_scope="current-1",
        existing_functional_routes=set(),
    )
    assert errors == []
    matches = sorted(
        (item for item in projected if item["source_label"] == "老者"),
        key=lambda item: item["_current_response_group_key"],
    )
    assert len(matches) == 2
    assert [item["scope_qualifier"] for item in matches] == ["甲", "乙"]
    assert all(
        item["_current_identity_synthesized_qualifier"] is True for item in matches
    )
    assert len({item["identity_group"] for item in matches}) == 2


def test_ep5_disambiguation_key_falls_back_to_authority_id_for_named_branch() -> None:
    """_current_identity_disambiguation_key 单测：k 分支（已登记具名身份）
    append_candidate 时 _current_response_group_key 恒为空串（见该调用点
    上方注释），必须回退用 authority_id 作为区分键，不能让所有 k 分支候选
    都退化成同一个空字符串键（那样反而会把两个不同的已登记角色错误地
    当成"需要拆分补足限定语"的同一组处理）。"""
    key_with_functional = portraits._current_identity_disambiguation_key({
        "_current_response_group_key": "F3", "authority_id": "",
    })
    assert key_with_functional == "F3"
    key_named_a = portraits._current_identity_disambiguation_key({
        "_current_response_group_key": "", "authority_id": "bible:萧炎",
    })
    key_named_b = portraits._current_identity_disambiguation_key({
        "_current_response_group_key": "", "authority_id": "bible:药老",
    })
    assert key_named_a == "bible:萧炎"
    assert key_named_b == "bible:药老"
    assert key_named_a != key_named_b
