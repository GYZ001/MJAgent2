"""ERR-20260902-2aabcc：``reference_identity`` 决议必须折进具名语义组。

真实事故（《神墓》EP2，run_ededec3f4ae6）：第 1 章里雨馨只被辰南追忆、从未出场，
尝试 1 的人物发现正确地把她判成「仅提及」，落库为 ``reference_identity``、
``identity_group=current-1:F18``、``authority_id=bible:雨馨``。同一次运行的
资产映射步骤随后把雨馨补进了人物谱；尝试 2 重新加载刚落库的决议时，
``identity_authority_registry`` 只对 ``future_identity`` 折叠语义组，于是
``bible:雨馨`` 同时挂在 ``bible:雨馨`` 与 ``<fp>:current-1:F18`` 两个组上，
被自己的「authority 跨多个 identity_group」校验打死，整集映射失败。

``authority_id_for_resolution`` 早已把 ``reference_identity`` 与 ``future_identity``
同列为具名家族（都铸 ``bible:<name>``）；注册表却只认后者——两侧契约不对齐，
宽的那一侧就是必然发生的线上故障。本文件钉住对齐后的行为。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.identity_authority import (
    IdentityAuthorityConflictError,
    identity_authority_registry,
)
from app.schemas import Bible, World


EP2_SCOPE = "1d80620bd1947717f334afabaae29aaacdb762769443bb4650c395ea51229a70"


def _bible(*names: str) -> Bible:
    bible = Bible(characters=[], world=World(visual_style_canonical="国漫电影风"))
    for name in names:
        bible.characters.append(SimpleNamespace(name=name, aliases=[]))
    return bible


def _yuxin_reference_row(**overrides: object) -> dict:
    """与生产库 episodes.screenplay_character_resolutions 里那条同形。"""
    row = {
        "source_label": "雨馨",
        "canonical_name": "雨馨",
        "resolution": "reference_identity",
        "reason": "来源或蓝图引用该稳定身份，但当前集不需要人物卡或视觉资产",
        "identity_group": "current-1:F18",
        "identity_scope_fingerprint": EP2_SCOPE,
        "authority_id": "bible:雨馨",
        "source_label_provenance": "owned_current_literal.v1",
    }
    row.update(overrides)
    return row


def test_reference_identity_for_bible_character_joins_bible_group() -> None:
    """尝试 2 的真实输入：人物谱已有雨馨 + 尝试 1 落库的 reference 行。"""
    registry = identity_authority_registry(_bible("雨馨"), [_yuxin_reference_row()])

    entry = next(item for item in registry if item["authority_id"] == "bible:雨馨")
    assert entry["identity_group"] == "bible:雨馨"
    assert entry["canonical_name"] == "雨馨"
    assert "雨馨" in entry["source_labels"]
    assert len([item for item in registry if item["authority_id"] == "bible:雨馨"]) == 1


def test_reference_identity_before_bible_registration_uses_canonical_group() -> None:
    """尝试 1 的输入：雨馨还不在人物谱。语义组已经是 bible:雨馨，原始模型组只留作
    decision_identity_group——这样后续 K 决议对它是可物化的，与 future_identity 一致。"""
    registry = identity_authority_registry(_bible(), [_yuxin_reference_row()])

    assert len(registry) == 1
    entry = registry[0]
    assert entry["authority_id"] == "bible:雨馨"
    assert entry["identity_kind"] == "reference"
    assert entry["identity_group"] == "bible:雨馨"
    assert entry["decision_identity_group"] == "current-1:F18"
    assert entry["source_instance_key"] == f"{EP2_SCOPE}:current-1:F18"


def test_reference_and_future_rows_for_same_name_share_one_authority() -> None:
    """同一集里先被提及（reference）、后在另一批出场（future）：一个人一个权威。"""
    registry = identity_authority_registry(_bible(), [
        _yuxin_reference_row(),
        {
            "source_label": "那个女孩",
            "canonical_name": "雨馨",
            "resolution": "future_identity",
            "identity_group": "current-2:F3",
            "identity_scope_fingerprint": EP2_SCOPE,
        },
    ])

    assert len(registry) == 1
    assert registry[0]["identity_group"] == "bible:雨馨"
    assert set(registry[0]["source_labels"]) == {"雨馨", "那个女孩"}


def test_reference_identity_with_foreign_authority_coexists_with_bible_entry() -> None:
    """折叠只作用于语义组：reference 行若带着非 bible: 的显式权威（人工签发/历史
    future-name），与人物谱同名条目并存，不参与「同名多 named authority」校验——
    那条校验只在 future_identity 之间竞争一个真名（人工权威不得被 K 物化升级，见
    tests/test_character_discovery.py::test_materialized_bible_alias_k_never_upgrades_manual_authority）。"""
    registry = identity_authority_registry(
        _bible("雨馨"),
        [_yuxin_reference_row(authority_id="manual:yuxin")],
    )

    by_authority = {item["authority_id"]: item for item in registry}
    assert set(by_authority) == {"bible:雨馨", "manual:yuxin"}
    assert by_authority["manual:yuxin"]["identity_group"] == "manual:yuxin"
    assert by_authority["bible:雨馨"]["identity_group"] == "bible:雨馨"


def test_reference_and_functional_claims_in_same_raw_group_still_conflict() -> None:
    """正向唯一性照旧走原始模型组：同一个 F18 不得同时解析到两个权威。"""
    with pytest.raises(IdentityAuthorityConflictError, match="对应多个 canonical identity"):
        identity_authority_registry(_bible(), [
            _yuxin_reference_row(),
            {
                "source_label": "少女",
                "canonical_name": "少女",
                "resolution": "functional_identity",
                "identity_group": "current-1:F18",
                "identity_scope_fingerprint": EP2_SCOPE,
            },
        ])
