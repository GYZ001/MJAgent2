"""EP-01 §5 权限点目录守卫：从 registry 推导，不允许维护第二张名单。

断言方式：不是照抄一份预期命令名单去比对（那就是在维护第二张名单），而是
用 registry 自身的计数关系（``len(catalog) == commands + exemptions + 1``）
与"往 registry 里塞一条新命令，目录必须自动出现"这两条结构性不变量守住
"从数据推导"这句话——任何人往 catalog.py 里手写枚举都会被这两条戳破。
"""
from __future__ import annotations

from app.authz import catalog as authz_catalog
from app.authz import policy
from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.registry import CapabilityRegistry, CommandSpec
from app.capabilities.schemas import (
    ConfirmationPolicy,
    IdempotencyPolicy,
    RiskLevel,
    StandardCommandInput,
)


def test_catalog_size_equals_commands_plus_exemptions_plus_one() -> None:
    registry = ensure_catalog_loaded()
    catalog = authz_catalog.build_permission_catalog()
    assert len(catalog) == len(registry.commands) + len(registry.rest_exemptions) + 1


def test_every_command_and_exemption_present_with_metadata() -> None:
    registry = ensure_catalog_loaded()
    catalog = authz_catalog.build_permission_catalog()
    for name, spec in registry.commands.items():
        point = catalog[name]
        assert point.title == spec.title
        assert point.risk == spec.risk.value
        assert point.admin_only == spec.admin_only
    for route in registry.rest_exemptions:
        assert f"route:{route}" in catalog
    assert policy.READ_PROJECT_KEY in catalog


def test_new_command_automatically_appears_without_touching_catalog_py(monkeypatch) -> None:
    """往 registry 里插一条从未在 app/authz/catalog.py 出现过的命令名，目录
    必须原样反映它——证明 build_permission_catalog() 真的在"读"而不是在
    "抄一份写死的清单"。
    """
    real_registry = ensure_catalog_loaded()
    fake_registry = CapabilityRegistry(
        commands=dict(real_registry.commands),
        resources=dict(real_registry.resources),
        ui_intents=dict(real_registry.ui_intents),
        human_only=dict(real_registry.human_only),
        rest_bindings=dict(real_registry.rest_bindings),
        rest_exemptions=dict(real_registry.rest_exemptions),
    )
    new_spec = CommandSpec(
        name="test.throwaway_command_never_in_catalog_py",
        version="1.0.0",
        title="仅用于验证目录自动出现的临时命令",
        description="test_permission_catalog.py 专用，不出现在任何生产代码里",
        input_model=StandardCommandInput,
        risk=RiskLevel.R1_REVERSIBLE,
        confirmation=ConfirmationPolicy.NEVER,
        idempotency=IdempotencyPolicy.NONE,
        scopes=frozenset({"manju:read"}),
        side_effect="none",
    )
    fake_registry.commands[new_spec.name] = new_spec
    from tests.conftest import patch_authz_everywhere

    patch_authz_everywhere(monkeypatch, "get_registry", lambda: fake_registry)

    catalog = authz_catalog.build_permission_catalog()
    assert new_spec.name in catalog
    assert catalog[new_spec.name].title == new_spec.title
    # org_admin/owner 覆盖"全部非 admin_only 命令"，新命令天然被收进去——见
    # app/authz/catalog.py 模块文档对 unclassified_commands() 语义的说明。
    assert new_spec.name in authz_catalog.build_builtin_role_permissions("org_admin")
    assert new_spec.name in authz_catalog.build_builtin_role_permissions("owner")


def test_unclassified_commands_is_structurally_empty() -> None:
    """非 admin_only 却没出现在任何内置模板里的命令——应恒为空集合。

    非空说明某个模板的谓词逻辑本身有 bug（见 app/authz/catalog.py 模块
    docstring），是需要立刻修的回归，不是"新命令待人工归类"的软提醒。
    """
    ensure_catalog_loaded()
    assert authz_catalog.unclassified_commands() == []


def test_admin_only_commands_are_never_in_any_builtin_template() -> None:
    registry = ensure_catalog_loaded()
    admin_only_names = {spec.name for spec in registry.commands.values() if spec.admin_only}
    assert admin_only_names, "expected at least one admin_only command in the real registry"
    for key in policy.BUILTIN_ROLE_KEYS:
        granted = authz_catalog.build_builtin_role_permissions(key)
        assert not (admin_only_names & granted), (
            f"builtin role {key!r} must never be granted an admin_only command"
        )


def test_reviewer_gets_decide_and_delivery_but_not_generate() -> None:
    """审校角色是本单元的正确性试金石（EP-01 §6）：结构性核对权限点集合本身
    （命令行为断言见 tests/test_org_rbac_matrix.py）。
    """
    ensure_catalog_loaded()
    reviewer = authz_catalog.build_builtin_role_permissions("reviewer")
    assert "storyboard.confirm" in reviewer
    assert "video.adopt_version" in reviewer
    assert "reference.review" in reviewer
    assert "video.stop_shot" in reviewer
    assert "video.stop_episode" in reviewer
    assert "delivery.review" in reviewer
    assert "video.generate_shot" not in reviewer
    assert "video.generate_episode" not in reviewer


def test_viewer_is_read_only() -> None:
    ensure_catalog_loaded()
    assert authz_catalog.build_builtin_role_permissions("viewer") == frozenset({policy.READ_PROJECT_KEY})


def test_unknown_role_key_raises() -> None:
    ensure_catalog_loaded()
    import pytest

    with pytest.raises(ValueError):
        authz_catalog.build_builtin_role_permissions("not_a_real_role")
