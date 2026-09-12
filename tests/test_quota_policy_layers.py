"""EP-04 第一阶段架构闸门：``app/quota_policy/`` 的层号声明 + 反向依赖红线。

派单原文的两条硬约束，专门开一个文件验证（不是重复
``tests/test_arch_graph_layers.py`` 已经覆盖的通用工具语义/全仓上行边归零）：

1. ``app/LAYERS.toml`` 必须显式声明 ``app.quota_policy``（=2）与
   ``app.quota_policy.api``（=5）——未声明按 fail-safe 判 L5 会让真实违规淹没
   在幻影报告里（见 ``app/LAYERS.toml`` 顶部文档的 115 个未声明模块教训）。
2. **同层不代表零风险**：``app.quota`` 与 ``app.quota_policy`` 都是 L2，通用
   的 ``scripts/arch_graph.py --check-layers`` 只抓「上行边」（低层 import 高
   层），天然抓不住"同层但方向定错了"这类反向依赖——``app.quota_policy`` 任
   何一个模块（``api.py`` 除外，它是 L5 路由层，允许依赖同/低层任何东西）反
   过来 import ``app.quota`` 都不会被那道通用闸门举报，必须单独用 AST 扫描
   兜底，这正是本文件存在的理由。
"""
from __future__ import annotations

import ast
from pathlib import Path

from scripts.arch_graph import DEFAULT_LAYERS_FILE, load_layers_config, resolve_layer

ROOT = Path(__file__).resolve().parent.parent
QUOTA_POLICY_DIR = ROOT / "app" / "quota_policy"

#: api.py 是 L5（REST 路由，需要 app.auth.principal/app.orgs.store），其余四个
#: 模块（schema/plans/allocation/usage_query）是 L2，与 app.quota 同层。
EXPECTED_LAYERS = {
    "app.quota_policy": 2,
    "app.quota_policy.schema": 2,
    "app.quota_policy.plans": 2,
    "app.quota_policy.allocation": 2,
    "app.quota_policy.usage_query": 2,
    "app.quota_policy.api": 5,
}

#: 允许反向依赖 app.quota 的唯一例外——api.py 是 L5 路由层，本身不禁止依赖
#: app.quota（派单红线明确针对"新包"整体，api.py 目前也确实没有这么写，但
#: 架构上它不像 schema/plans/allocation/usage_query 那样承担"app.quota 的取
#: 值来源"角色，未来即便真的需要也不构成本次红线要防的那种循环）。
_FORBIDDEN_REVERSE_DEP_MODULES = ("schema.py", "plans.py", "allocation.py", "usage_query.py")


def test_layers_toml_declares_quota_policy_at_the_documented_layers() -> None:
    config = load_layers_config(DEFAULT_LAYERS_FILE)
    for module_name, expected_layer in EXPECTED_LAYERS.items():
        layer, matched = resolve_layer(module_name, config.layers)
        assert matched is not None, (
            f"{module_name} 未在 app/LAYERS.toml 里被任何 key 覆盖到——会被 "
            "resolve_layer 的 fail-safe 规则误判成 L5，制造幻影违规"
        )
        assert layer == expected_layer, (
            f"{module_name} 期望层号 {expected_layer}，实际解析出 {layer}"
            f"（命中 key={matched!r}）"
        )


def _imports_app_quota(path: Path) -> list[str]:
    """AST 扫描 ``import app.quota`` / ``from app.quota import ...`` /
    ``from app import quota``——只认真实反向依赖 ``app.quota`` 本体，不误伤
    ``app.quota_addon``/``app.quota_scope``/``app.quota_tiers``/
    ``app.quota_expiry``/``app.quota_policy`` 这几个名字前缀相同的独立兄弟
    模块（朴素的 ``startswith("app.quota")`` 字符串匹配会把它们全部误杀）。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "app.quota":
                    hits.append(f"{path}:{node.lineno}: import app.quota")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "app.quota":
                hits.append(f"{path}:{node.lineno}: from app.quota import ...")
            elif node.module == "app" and any(a.name == "quota" for a in node.names):
                hits.append(f"{path}:{node.lineno}: from app import quota")
    return hits


def test_quota_policy_does_not_reverse_depend_on_app_quota() -> None:
    """架构红线（派单原文）：新包任何模块都不得反向依赖 app.quota——
    app.quota 已经 import app.quota_policy.allocation 来源化
    effective_limits，反过来会立刻在这两个 L2 模块间制造一个真实循环，且会
    叠加到既有的 db ↔ quota ↔ quota_addon ↔ quota_tiers 循环团上。通用的层
    级检查只抓"上行"（L2 依赖 L5 之类），两个模块同为 L2 时它认为"同层允许"，
    结构上抓不住这条——必须单独 AST 扫描。
    """
    violations: list[str] = []
    for filename in _FORBIDDEN_REVERSE_DEP_MODULES:
        path = QUOTA_POLICY_DIR / filename
        assert path.exists(), f"预期的模块不存在：{path}"
        violations.extend(_imports_app_quota(path))
    assert violations == [], "\n".join(violations)


def test_quota_reads_effective_limits_from_quota_policy_allocation() -> None:
    """反向验证接线没有假通过：``app.quota`` 确实 import 了
    ``app.quota_policy.allocation``（不是两边各自孤立、谁都没连起来）。"""
    quota_source = (ROOT / "app" / "quota.py").read_text(encoding="utf-8")
    assert "from app.quota_policy import allocation" in quota_source, (
        "app/quota.py 应该从 app.quota_policy.allocation 取三级配额判定的实现；"
        "如果这行断言失败，说明 effective_limits 的改造点没有真的接上"
    )


def test_quota_policy_package_files_are_exactly_the_declared_set() -> None:
    """新增/改名模块必须同步维护本文件的 ``EXPECTED_LAYERS``——防止有人加了
    第六个子模块却忘了给它声明层号（会被 fail-safe 静默吃成 L5）。"""
    actual = {
        p.stem for p in QUOTA_POLICY_DIR.glob("*.py")
        if p.stem != "__init__"
    }
    expected = {name.rsplit(".", 1)[-1] for name in EXPECTED_LAYERS if name != "app.quota_policy"}
    assert actual == expected, (
        f"app/quota_policy/ 下的模块集合与本文件 EXPECTED_LAYERS 不一致："
        f"实际={sorted(actual)}，期望={sorted(expected)}"
    )
