import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app import api, db, hiagent, portraits, screenplay_scene_shards, stages
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
        else:
            decisions[group_key] = next(
                option for option in options if str(option).startswith("F:")
            )
            revealed_names[group_key] = ""
            reveal_evidence_ids[group_key] = ""
    return {
        "decisions": decisions,
        "revealed_names": revealed_names,
        "reveal_evidence_ids": reveal_evidence_ids,
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
    ]["maxLength"] == 16

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
        (
            '{"characters":[{"source_label" "未知求救者"}]}',
            model_gateway.StructuredFormatError,
        ),
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
        "status TEXT, content_json TEXT, created_at REAL)"
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
    conn.execute(
        "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
        (
            "ordinary-current",
            "episode",
            "ep-attempt10-ordinary-cache",
            "screenplay_identity_discovery",
            "validated",
            json.dumps({
                "contract_version": (
                    portraits.IDENTITY_DISCOVERY_CONTRACT_VERSION
                ),
                "current_identity_version": (
                    portraits.CURRENT_IDENTITY_DECISION_VERSION
                ),
                "current_evidence_catalog_hash": (
                    portraits._current_identity_evidence_catalog_hash(
                        source_text
                    )
                ),
                "input_hash": current_contract_hash,
                "mode": "targeted",
                "candidates": expected,
            }),
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

    assert provider_calls == ["screenplay_character_discovery"]
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
        return {"important": True, "reason": "反复出场", "role": "重要配角",
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
        return '{"important":true,"reason":"响应被截断'

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
        return {"important": True, "reason": "反复出场", "role": "反派",
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
        return {"important": False, "reason": "路人", "role": "重要配角",
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
            == "screenplay-future-identity.v10"
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
            "screenplay-identity-discovery.v14"
        )
        assert meta["current_identity_version"] == (
            "screenplay-current-identity.v11"
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


def test_current_identity_rf10_schema_stays_under_strict_property_limit() -> None:
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


def test_current_identity_rf10_manual_alias_k_is_backend_projected() -> None:
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
        all_evidence_by_id={
            str(record["evidence_id"]): record
            for record in records
        },
        reserved_authority_labels={"师尊", "苍玄"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    assert errors == []
    assert projected[0]["source_label"] == "师尊"
    assert projected[0]["name"] == "苍玄"
    assert projected[0]["authority_id"] == "manual:cangxuan"


def test_current_identity_rf10_manual_alias_cannot_reach_card_materialization(
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


def test_current_identity_rf10_manual_alias_mentioned_persists_one_authority(
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
        ("cross_f", "evidence_id 与已知逐字 source_label 不匹配"),
    ],
)
def test_current_identity_rf10_custom_exact_gates(
    mutation: str,
    error_fragment: str,
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
    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref=evidence_by_ref,
        known_decisions=known,
        all_evidence_by_id={
            str(record["evidence_id"]): record
            for record in records
        },
        reserved_authority_labels={"耳根"},
        group_scope="current-1",
        existing_functional_routes=set(),
    )

    assert any(error_fragment in error for error in errors)


@pytest.mark.parametrize("failure", ["attempt15_rf9", "unknown_evidence"])
def test_current_identity_rf10_rejects_unbound_provider_output_once(
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


def test_current_identity_rf10_unsupported_schema_is_one_call(
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


def test_current_identity_same_label_multiple_groups_fails_closed_once(
    monkeypatch,
) -> None:
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
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="source_label 重复",
    ):
        asyncio.run(portraits.discover_character_candidates(
            "两名绿袍修士同时开口。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))
    assert calls == 1


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


def test_current_identity_literal_label_rejects_cross_evidence_once(
    monkeypatch,
) -> None:
    calls = 0
    downstream: list[str] = []

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

    async def forbidden_future(*_args, **_kwargs):
        downstream.append("future")
        raise AssertionError("cross-evidence current result reached future")

    monkeypatch.setattr(model_gateway, "chat", fake_chat)
    monkeypatch.setattr(
        portraits,
        "resolve_future_identity_candidates",
        forbidden_future,
    )
    with pytest.raises(
        model_gateway.StructuredSemanticError,
        match="evidence_id 与已知逐字 source_label 不匹配",
    ):
        asyncio.run(portraits.discover_character_candidates(
            "门卫守在山门。\n\n银袍女子站在殿前。",
            Bible(world=World(visual_style_canonical="国风"), characters=[]),
            1,
        ))

    assert calls == 1
    assert downstream == []


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
    guard_record = next(
        item for item in records if "门卫" in str(item["text"])
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

    _projected, errors = portraits._project_current_identity_response(
        response,
        evidence_by_ref={"E001": unrelated_record},
        known_decisions={},
        all_evidence_by_id={
            guard_record["evidence_id"]: guard_record,
            unrelated_record["evidence_id"]: unrelated_record,
        },
        group_scope="current-2",
        existing_functional_routes=set(),
    )

    assert errors == [
        "current evidence_id 与已知逐字 source_label 不匹配：门卫"
    ]


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
    assert result["resolutions"][0]["authority_id"].startswith("functional:")


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
