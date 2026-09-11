"""EP-01 §5 权限点目录：从运行时 Command Registry 推导，禁止维护第二张名单。

真源是 ``app.capabilities.registry.get_registry()``——76 条命令 + 74 条豁免
路由（``REST_EXEMPTIONS``）+ 1 条通用读面。整包声明 L5（见
``app/LAYERS.toml::app.capabilities``），本模块因此也只能落在 L5——按依赖
如实归层，不按 EP-01 派单原稿建议的 L2（该建议未考虑 ``registry`` 本身就在
L5，交付报告里已单独说明这处与 PRD 不符）。

::

    permission_key ::= <command_name>              # Command Bus 命令
                      | "route:<METHOD> <path>"     # 豁免路由，来自
                                                     # registry.rest_exemptions
                      | "read:project"              # GET 缺省读面，单条

``build_permission_catalog()`` 只读 registry，不缓存、不落库——每次调用都是
当下真实注册状态的快照，新增命令天然出现，不需要人工同步第二份清单。

内置角色模板（EP-01 §6）不是硬编码的命令名单，而是对 ``CommandSpec`` 结构化
字段（``risk``/``admin_only``/``tags``）的谓词过滤：
- ``org_admin``/``owner`` = 全部非 ``admin_only`` 命令（+ 全部豁免路由 +
  ``read:project``）
- ``producer`` = 非 ``admin_only`` 且 risk ∈ {R0,R1,R2}（+ ``read:project``）
- ``reviewer`` = 非 ``admin_only`` 且 tags 含 ``"decide"`` 或 ``"delivery"``
  （+ ``read:project``）——``"decide"`` 标签是本次改造给
  ``storyboard.confirm``/``video.adopt_version``/``reference.review``/
  ``video.stop_shot``/``video.stop_episode`` 五条命令新加的一等 registry
  数据（见对应 ``app/capabilities/commands/*.py``），不是在本文件里另起一张
  按命令名判断的清单
- ``viewer`` = 仅 ``read:project``

因为 ``org_admin``/``owner`` 覆盖"全部非 admin_only 命令"，新增命令永远不会
真正"无处安放"；``unclassified_commands()`` 的实际意义是兜底防线——只要有命令
的 ``admin_only=False`` 却没出现在任何模板里，说明五个谓词本身出了 bug（不是
在提醒"这条命令还没被人工分类"），测试据此失败并列出命令名，而不是静默漏判。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.authz import policy
from app.capabilities.registry import CapabilityRegistry, get_registry
from app.capabilities.schemas import RiskLevel

#: 五个内置角色模板的 key/展示名/说明，真源在 ``app.authz.policy``（L1，见该
#: 模块顶部为什么放在那一层的说明）；这里 re-export 供既有调用方沿用
#: ``app.authz.catalog.BUILTIN_ROLE_TEMPLATES`` 这个名字。
BUILTIN_ROLE_TEMPLATES = policy.BUILTIN_ROLE_TEMPLATES
BUILTIN_ROLE_KEYS = policy.BUILTIN_ROLE_KEYS

_PRODUCER_RISK_LEVELS = frozenset({RiskLevel.R0_READ, RiskLevel.R1_REVERSIBLE, RiskLevel.R2_MATERIAL})
_REVIEWER_TAGS = frozenset({"decide", "delivery"})


@dataclass(frozen=True, slots=True)
class PermissionPoint:
    """权限点展示元数据，字段全部透出自 ``CommandSpec``（EP-01 §5）。"""

    key: str
    title: str
    risk: str
    scopes: tuple[str, ...]
    side_effect: str
    admin_only: bool = False
    tags: tuple[str, ...] = ()


def _ensure_registry_loaded() -> CapabilityRegistry:
    from app.capabilities.catalog import ensure_registered

    ensure_registered()
    return get_registry()


def build_permission_catalog() -> dict[str, PermissionPoint]:
    """遍历运行时 registry 推导权限点目录；不维护第二张名单。"""
    registry = _ensure_registry_loaded()
    catalog: dict[str, PermissionPoint] = {}
    for name, spec in registry.commands.items():
        catalog[name] = PermissionPoint(
            key=name,
            title=spec.title,
            risk=spec.risk.value,
            scopes=tuple(sorted(spec.scopes)),
            side_effect=spec.side_effect,
            admin_only=spec.admin_only,
            tags=spec.tags,
        )
    for route, reason in registry.rest_exemptions.items():
        key = f"route:{route}"
        catalog[key] = PermissionPoint(
            key=key, title=route, risk="n/a", scopes=(), side_effect=reason,
        )
    catalog[policy.READ_PROJECT_KEY] = PermissionPoint(
        key=policy.READ_PROJECT_KEY,
        title="项目读面",
        risk=RiskLevel.R0_READ.value,
        scopes=("manju:read",),
        side_effect="none_read",
    )
    return catalog


def build_builtin_role_permissions(role_key: str) -> frozenset[str]:
    """按角色 key 从 registry 谓词推导权限点集合；未知 key 直接报错（fail closed）。"""
    if role_key not in BUILTIN_ROLE_KEYS:
        raise ValueError(f"unknown builtin role key: {role_key!r}")
    registry = _ensure_registry_loaded()
    commands = registry.commands.values()
    if role_key in {"org_admin", "owner"}:
        keys = {spec.name for spec in commands if not spec.admin_only}
        keys.update(f"route:{route}" for route in registry.rest_exemptions)
        keys.add(policy.READ_PROJECT_KEY)
        return frozenset(keys)
    if role_key == "producer":
        keys = {
            spec.name for spec in commands
            if not spec.admin_only and spec.risk in _PRODUCER_RISK_LEVELS
        }
        keys.add(policy.READ_PROJECT_KEY)
        return frozenset(keys)
    if role_key == "reviewer":
        keys = {
            spec.name for spec in commands
            if not spec.admin_only and (_REVIEWER_TAGS & set(spec.tags))
        }
        keys.add(policy.READ_PROJECT_KEY)
        return frozenset(keys)
    # viewer
    return frozenset({policy.READ_PROJECT_KEY})


def unclassified_commands() -> list[str]:
    """非 admin_only 却未出现在任何内置模板里的命令名——结构上应恒为空。

    见模块 docstring：``org_admin``/``owner`` 覆盖全部非 admin_only 命令，
    非空说明某个模板的谓词逻辑本身有 bug，不是"新命令待人工归类"的提示。
    """
    registry = _ensure_registry_loaded()
    classified: set[str] = set()
    for key in BUILTIN_ROLE_KEYS:
        classified.update(build_builtin_role_permissions(key))
    return sorted(
        spec.name for spec in registry.commands.values()
        if not spec.admin_only and spec.name not in classified
    )
