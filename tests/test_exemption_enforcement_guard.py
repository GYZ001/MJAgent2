"""AST 守卫：豁免路由声明的 admin 语义必须对应处理函数里真实存在的强制校验。

背景（EP-01 三阶段安全审查，2026-09-12）：``POST /api/system/mcp-tokens``
在 ``app/capabilities/exemptions.py`` 早就登记了 ``scopes={"manju:admin"}``，
注释也写明「Agent/外部 MCP 客户端不能自我签发或升级授权范围」——意图写在
注释里，但处理函数本身零鉴权，任何登录用户（含 viewer 只读角色）都能给自己
签一枚带 manju:admin 的 MCP token 再拿它提权。``POST
/api/video-capabilities/{provider}/{model:path}/probe`` 与 ``POST
/api/provider-media-publications`` 是同一类缺口：声明与强制分叉，且分叉
只能靠人工审查发现。

本文件把这类分叉变成可执行闸门：任何路由只要在 ``EXEMPT_ROUTE_PERMISSIONS``
里声明了 ``admin_only=True`` 或 ``scopes`` 含 ``manju:admin``，就必须在源码里
真的挂着下列三种强制形态之一——判据从 AST 推导，不维护路由白名单，新增/
改动的路由自动被覆盖：

1. 处理函数形参默认值是 ``Depends(require_system_admin)``；
2. 路由装饰器自带 ``dependencies=[...]``，其中含 ``Depends(require_system_admin)``；
3. 所在 router 在声明处（``APIRouter(dependencies=[...])``）或在
   ``app/main.py`` 的 ``include_router(..., dependencies=[...])`` 挂载点上
   叠加了 ``Depends(require_system_admin)``（后者需要顺着 ``app/main.py`` 的
   import 别名把路由文件与挂载调用对上，见 ``_main_admin_mounts``）；
4. 函数体内调用了已知会在非管理员/非组织管理员时 ``raise`` 的角色断言封装
   （``_require_org_admin`` / ``_require_project_manage_access``）——团队/
   角色/授权/配额等组织治理端点限定的是 ``org_admin`` 角色而非
   ``is_system_admin``，因此合法地不挂 ``require_system_admin``，见
   ``app/capabilities/exemptions.py::_RouteExemption`` 文档。

识别不了的路由一律 fail closed（判红），不当作「放行」处理。

本文件独立实现一份最小 AST 扫描器，不导入 ``app/capabilities/coverage.py``
里的下划线私有函数——两者目的不同（那边判「confirm 确认闸门」，这边判「admin
强制」），各自独立验证也符合 CLAUDE.md「验证要有独立观察点」；重合的只是
「解析路由装饰器」这一小块，量级足够小，独立实现比跨模块耦合到对方的私有
实现更不容易在对方重构时被带崩。
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path
from typing import NamedTuple

from app.capabilities.exemptions import EXEMPT_ROUTE_PERMISSIONS

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
MAIN_PY = APP_DIR / "main.py"

_ROUTE_METHODS = {"get", "post", "put", "delete", "patch"}
_ROUTER_NAMES = {"router", "public_router", "app"}
_ROUTER_DECL_RE = re.compile(r"\b(router|public_router)\s*=\s*APIRouter\(")
_PREFIX_ARG_RE = re.compile(r"prefix\s*=\s*[\"']([^\"']+)[\"']")

#: 已知会在非管理员/非组织管理员时 raise 的角色断言封装（见模块 docstring 第 4 条）。
#: 与 app/capabilities/coverage.py::_CONFIRMATION_GATE_CALLS 同一种做法：一份小的、
#: 有据可查的「已知强制原语名单」，不是路由白名单——新增同类助手函数需要显式加进来，
#: 这正是想要的效果（新的角色断言写法要能被人看见，而不是静默绕过守卫）。
_KNOWN_ADMIN_ASSERTION_CALLS = frozenset({
    "require_system_admin",
    "_require_org_admin",
    "_require_project_manage_access",
})


@functools.lru_cache(maxsize=None)
def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=None)
def _parse_file(path: Path) -> ast.Module:
    return ast.parse(_read_text(path), filename=str(path))


def _prefix_for_file(path: Path) -> str:
    """推断该文件路由的最终 URL 前缀。与
    ``app.capabilities.coverage._router_prefix_for_file`` 各自独立实现（见模块
    docstring）；本仓库当前所有 admin_only/manju:admin 豁免路由都不在
    ``app/agent/``、``app/mcp/`` 下，故不复刻那两个特判分支。
    """
    text = _read_text(path)
    match = _ROUTER_DECL_RE.search(text)
    declared = ""
    if match:
        tail = text[match.end():match.end() + 200]
        prefix_match = _PREFIX_ARG_RE.search(tail)
        if prefix_match:
            declared = prefix_match.group(1)
    if declared.startswith("/api"):
        return declared
    return "/api" + declared


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_depends_admin_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call) or _call_name(node.func) != "Depends":
        return False
    return bool(node.args) and _call_name(node.args[0]) == "require_system_admin"


class _RouteEntry(NamedTuple):
    method: str
    path: str
    router_var: str
    file_path: Path
    func_node: ast.FunctionDef | ast.AsyncFunctionDef
    dec: ast.Call


def _decorator_route(dec: ast.expr) -> tuple[str, str, str] | None:
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not (isinstance(func, ast.Attribute) and func.attr in _ROUTE_METHODS):
        return None
    if not (isinstance(func.value, ast.Name) and func.value.id in _ROUTER_NAMES):
        return None
    if not dec.args or not isinstance(dec.args[0], ast.Constant) or not isinstance(dec.args[0].value, str):
        return None
    return func.attr.upper(), dec.args[0].value, func.value.id


def _normalize_path(prefix: str, route_path: str) -> str:
    if not route_path.startswith("/"):
        route_path = "/" + route_path
    full = f"{prefix}{route_path}"
    if len(full) > 1 and full.endswith("/"):
        full = full.rstrip("/")
    while "//" in full:
        full = full.replace("//", "/")
    return full


@functools.lru_cache(maxsize=None)
def _scan_app() -> tuple[_RouteEntry, ...]:
    entries: list[_RouteEntry] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        try:
            tree = _parse_file(path)
        except SyntaxError:
            continue
        prefix = _prefix_for_file(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                found = _decorator_route(dec)
                if found is None:
                    continue
                method, route_path, router_var = found
                full_path = _normalize_path(prefix, route_path)
                entries.append(_RouteEntry(method, full_path, router_var, path, node, dec))
    return tuple(entries)


def _entries_by_route() -> dict[tuple[str, str], list[_RouteEntry]]:
    grouped: dict[tuple[str, str], list[_RouteEntry]] = {}
    for entry in _scan_app():
        grouped.setdefault((entry.method, entry.path), []).append(entry)
    return grouped


def _function_param_has_admin_depends(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    args = func_node.args
    defaults = [*args.defaults, *args.kw_defaults]
    return any(d is not None and _is_depends_admin_call(d) for d in defaults)


def _decorator_dependencies_have_admin(dec: ast.Call) -> bool:
    for kw in dec.keywords:
        if kw.arg == "dependencies" and isinstance(kw.value, ast.List):
            if any(_is_depends_admin_call(elt) for elt in kw.value.elts):
                return True
    return False


def _body_has_admin_assertion(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for n in ast.walk(func_node):
        if isinstance(n, ast.Call) and _call_name(n.func) in _KNOWN_ADMIN_ASSERTION_CALLS:
            return True
    return False


def _same_file_router_has_admin_dep(tree: ast.Module, router_var: str) -> bool:
    """``router = APIRouter(..., dependencies=[Depends(require_system_admin)])``
    直接声明在处理函数所在文件里的情形（如 app/sso/admin_api.py、app/audit/api.py）。
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == router_var for t in node.targets):
            continue
        value = node.value
        if not (isinstance(value, ast.Call) and _call_name(value.func) == "APIRouter"):
            continue
        for kw in value.keywords:
            if kw.arg == "dependencies" and isinstance(kw.value, ast.List):
                if any(_is_depends_admin_call(elt) for elt in kw.value.elts):
                    return True
    return False


def _module_dotted(path: Path) -> str:
    return path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")


@functools.lru_cache(maxsize=None)
def _main_admin_mounts() -> dict[tuple[str, str], bool]:
    """``{(模块点分路径, 变量名): 是否在 app/main.py 的 include_router(...) 挂载点
    上叠加了 Depends(require_system_admin)}``——覆盖 app/observability/api.py
    这类「router 自身不挂依赖、挂载时才叠加」的情形（见 app/main.py
    ``dependencies=_PROJECT_OWNER_DEPS + [Depends(require_system_admin)]``）。
    """
    tree = _parse_file(MAIN_PY)
    import_map: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                import_map[alias.asname or alias.name] = (node.module, alias.name)

    module_assigns: dict[str, ast.expr] = {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }

    def resolves_admin(expr: ast.expr, depth: int = 0) -> bool:
        if depth > 6:
            return False
        if isinstance(expr, ast.List):
            return any(_is_depends_admin_call(e) for e in expr.elts)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            return resolves_admin(expr.left, depth + 1) or resolves_admin(expr.right, depth + 1)
        if isinstance(expr, ast.Name) and expr.id in module_assigns:
            return resolves_admin(module_assigns[expr.id], depth + 1)
        return False

    mounts: dict[tuple[str, str], bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "include_router":
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        target = import_map.get(node.args[0].id)
        if target is None:
            continue
        admin = any(resolves_admin(kw.value) for kw in node.keywords if kw.arg == "dependencies")
        mounts[target] = admin
    return mounts


def _router_mount_has_admin(entry: _RouteEntry) -> bool:
    if _same_file_router_has_admin_dep(_parse_file(entry.file_path), entry.router_var):
        return True
    return bool(_main_admin_mounts().get((_module_dotted(entry.file_path), entry.router_var)))


def _requires_admin_metadata(item) -> bool:
    return bool(item.admin_only or "manju:admin" in item.scopes)


def _route_is_enforced(entries: list[_RouteEntry]) -> tuple[bool, str]:
    """任一处理函数命中任一种强制形态就算数（同一路径理论上只会有一个真实
    handler，这里按「至少一个匹配」而不是「全部匹配」判定，与 AST 扫描可能
    因装饰器写法产生多重命中时保持宽松一致）。"""
    reasons: list[str] = []
    for entry in entries:
        if (
            _function_param_has_admin_depends(entry.func_node)
            or _decorator_dependencies_have_admin(entry.dec)
            or _body_has_admin_assertion(entry.func_node)
            or _router_mount_has_admin(entry)
        ):
            return True, ""
        reasons.append(
            f"{entry.file_path.relative_to(ROOT)}::{entry.func_node.name} 没有 "
            "Depends(require_system_admin) / 角色断言调用 / router 级挂载"
        )
    return False, "; ".join(reasons)


def _route_has_require_system_admin_literal(entries: list[_RouteEntry]) -> bool:
    """只认「字面上挂了 require_system_admin」的三种形态（不含组织治理端点的
    _require_org_admin 类角色断言）——用于反向核对：元数据 admin_only 必须
    覆盖到位，不能宽于代码（组织治理端点合法地保持 admin_only=False）。"""
    return any(
        _function_param_has_admin_depends(entry.func_node)
        or _decorator_dependencies_have_admin(entry.dec)
        or _router_mount_has_admin(entry)
        for entry in entries
    )


def test_admin_declared_exemption_routes_have_real_enforcement() -> None:
    """正向核对：声明了 admin 语义的豁免路由，处理函数必须真的挂着强制校验。

    修复前（2026-09-12 EP-01 三阶段审查发现时）本测试在下列三条路由上判红：
    POST /api/system/mcp-tokens、POST
    /api/video-capabilities/{provider}/{model:path}/probe、POST
    /api/provider-media-publications——三条路由的 exemptions.py 早就登记了
    scopes={"manju:admin"}，但处理函数零鉴权。
    """
    routes = _entries_by_route()
    gaps: list[str] = []
    for route, item in sorted(EXEMPT_ROUTE_PERMISSIONS.items()):
        if not _requires_admin_metadata(item):
            continue
        method, path = route.split(" ", 1)
        entries = routes.get((method, path))
        if not entries:
            gaps.append(f"{route}: 在源码中定位不到该路由（exemptions.py 是否过期？）")
            continue
        enforced, detail = _route_is_enforced(entries)
        if not enforced:
            gaps.append(f"{route}: {detail}")
    assert gaps == [], (
        "以下豁免路由声明了 admin_only=True 或 scopes 含 manju:admin，但处理函数"
        "没有真正的强制校验——声明与强制分叉，任何登录用户都能调用：\n"
        + "\n".join(gaps)
    )


def test_routes_enforcing_require_system_admin_declare_admin_only() -> None:
    """反向核对：处理函数已经字面挂着 require_system_admin 的路由，元数据必须
    如实声明 admin_only=True，不能声明得比代码窄。"""
    routes = _entries_by_route()
    gaps: list[str] = []
    for route, item in sorted(EXEMPT_ROUTE_PERMISSIONS.items()):
        method, path = route.split(" ", 1)
        entries = routes.get((method, path))
        if not entries or item.admin_only:
            continue
        if _route_has_require_system_admin_literal(entries):
            gaps.append(route)
    assert gaps == [], (
        "以下豁免路由处理函数已经真实挂着 Depends(require_system_admin)，但"
        "app/capabilities/exemptions.py 里的元数据 admin_only 仍是 False——"
        "声明比强制窄，请补 admin_only=True：\n" + "\n".join(gaps)
    )
