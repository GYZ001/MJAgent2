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
字段（``risk``/``admin_only``/``tags``）的谓词过滤，豁免路由则对
``RouteExemptionScope``（``scopes``/``admin_only``，EP-01 第三阶段新增，见
``app/capabilities/exemptions.py`` 顶部文档）做同一形状的谓词过滤——两类
权限点用**各自最贴合的结构化字段**，不是同一份谓词硬套两种数据：

- ``org_admin``/``owner`` = 全部非 ``admin_only`` 命令 + 全部非 ``admin_only``
  豁免路由（不按 scopes 过滤——这两个模板本来就该拿到"全部"）+
  ``read:project``
- ``producer`` = 非 ``admin_only`` 且 risk ∈ {R0,R1,R2} 的命令，+ 非
  ``admin_only`` 且 scopes 交上 ``{manju:project-write, manju:generation-text,
  manju:media-generate, manju:read}`` 非空的豁免路由（+ ``read:project``）
- ``reviewer`` = 非 ``admin_only`` 且 tags 含 ``"decide"`` 或 ``"delivery"``
  的命令，+ 非 ``admin_only`` 且 scopes 交上 ``{manju:media-decide,
  manju:delivery, manju:read}`` 非空的豁免路由（+ ``read:project``）——
  ``"decide"`` 标签是本次改造给 ``storyboard.confirm``/``video.adopt_version``/
  ``reference.review``/``video.stop_shot``/``video.stop_episode`` 五条命令
  新加的一等 registry 数据（见对应 ``app/capabilities/commands/*.py``），不是
  在本文件里另起一张按命令名判断的清单；豁免路由那一侧的 ``manju:media-decide``
  scope 是 EP-01 §7 给命令做"发起 vs 决策"拆分时定的**同一份**词汇表，不是
  另造的分类
- ``viewer`` = 仅 ``read:project``——豁免路由一条都不给（``manju:read`` 类
  的豁免路由，如遥测上报，不在任何模板的权限点集合里；``read:project`` 这个
  GET 缺省读面本身已经覆盖"能看"，viewer 不需要额外的 route:* 权限点）

两类权限点的"全无处安放"兜底不对称：命令侧因为 ``org_admin``/``owner``
覆盖"全部非 admin_only 命令"，新命令永远不会真正无处安放，
``unclassified_commands()`` 只做结构性回归防线（见该函数文档）；豁免路由则
没有对应的"未分类"概念——``app.capabilities.exemptions.exempt_rest()`` 在
注册时已经强制要求非空 ``scopes``（见该模块），新豁免路由天生带着分类，不
存在"注册了但没有 scopes"的中间态。
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
#: 豁免路由一侧的 producer/reviewer 谓词：与上面两个命令侧谓词并列使用，
#: 词汇表来自 EP-01 §7 给命令做"发起 vs 决策"拆分时定的同一份 scopes
#: （见模块文档）。``manju:read`` 都在场，允许两个角色各自访问自己能触达的
#: 只读/遥测类豁免路由，不必单独为它们再开一档。
_PRODUCER_ROUTE_SCOPES = frozenset(
    {"manju:project-write", "manju:generation-text", "manju:media-generate", "manju:read"}
)
_REVIEWER_ROUTE_SCOPES = frozenset({"manju:media-decide", "manju:delivery", "manju:read"})


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
        meta = registry.rest_exemption_scopes.get(route)
        catalog[key] = PermissionPoint(
            key=key, title=route, risk="n/a",
            scopes=tuple(sorted(meta.scopes)) if meta else (),
            side_effect=reason,
            admin_only=meta.admin_only if meta else False,
        )
    catalog[policy.READ_PROJECT_KEY] = PermissionPoint(
        key=policy.READ_PROJECT_KEY,
        title="项目读面",
        risk=RiskLevel.R0_READ.value,
        scopes=("manju:read",),
        side_effect="none_read",
    )
    return catalog


def _route_keys_for_scopes(registry: CapabilityRegistry, eligible_scopes: frozenset[str]) -> set[str]:
    """非 ``admin_only`` 且 scopes 与 ``eligible_scopes`` 有交集的豁免路由
    权限点键；``admin_only`` 的豁免路由不进任何 producer/reviewer 集合——
    那一档只放行系统管理员，与角色治理是两条不重叠的判定（见
    ``app/capabilities/exemptions.py::_RouteExemption`` 文档）。
    """
    return {
        f"route:{route}"
        for route, meta in registry.rest_exemption_scopes.items()
        if not meta.admin_only and (meta.scopes & eligible_scopes)
    }


def build_builtin_role_permissions(role_key: str) -> frozenset[str]:
    """按角色 key 从 registry 谓词推导权限点集合；未知 key 直接报错（fail closed）。"""
    if role_key not in BUILTIN_ROLE_KEYS:
        raise ValueError(f"unknown builtin role key: {role_key!r}")
    registry = _ensure_registry_loaded()
    commands = registry.commands.values()
    if role_key in {"org_admin", "owner"}:
        keys = {spec.name for spec in commands if not spec.admin_only}
        keys.update(
            f"route:{route}"
            for route, meta in registry.rest_exemption_scopes.items()
            if not meta.admin_only
        )
        keys.add(policy.READ_PROJECT_KEY)
        return frozenset(keys)
    if role_key == "producer":
        keys = {
            spec.name for spec in commands
            if not spec.admin_only and spec.risk in _PRODUCER_RISK_LEVELS
        }
        keys.update(_route_keys_for_scopes(registry, _PRODUCER_ROUTE_SCOPES))
        keys.add(policy.READ_PROJECT_KEY)
        return frozenset(keys)
    if role_key == "reviewer":
        keys = {
            spec.name for spec in commands
            if not spec.admin_only and (_REVIEWER_TAGS & set(spec.tags))
        }
        keys.update(_route_keys_for_scopes(registry, _REVIEWER_ROUTE_SCOPES))
        keys.add(policy.READ_PROJECT_KEY)
        return frozenset(keys)
    # viewer：仅 read:project，一条 route:* 都不给（见模块文档）。
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
