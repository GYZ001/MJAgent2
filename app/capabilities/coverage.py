"""扫描 FastAPI mutating 路由，对照 Capability Registry 覆盖率。"""
from __future__ import annotations

import ast
import json
import re
import textwrap
from pathlib import Path
from typing import Any

from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.registry import CommandSpec, get_registry
from app.capabilities.schemas import ConfirmationPolicy

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"

_DECORATOR_RE = re.compile(
    r"@(?:router|app)\.(post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']",
    re.MULTILINE,
)
_ROUTER_DECL_RE = re.compile(
    r"router\s*=\s*APIRouter\(\s*(?P<args>[^)]*)\)",
    re.MULTILINE,
)
_PREFIX_ARG_RE = re.compile(r"prefix\s*=\s*[\"']([^\"']+)[\"']")

_ROUTE_METHODS = {"get", "post", "put", "delete", "patch"}
_ROUTER_NAMES = {"router", "public_router", "app"}
# Command Bus 路径：任一 REST 端点里出现，就说明这条路由真的把执行权交给了
# app.capabilities.bus（进而复用 app.capabilities.policy.requires_confirmation
# 这唯一一份判据）。本地二段式：端点自己判断 confirm/quote_id 并显式拒绝，
# 见 account.self_delete、app/domain/bible_ops 的付费报价流。两者都算「有等价
# 确认机制」，缺一不可的只有「两个都没有」。
#: 经 Command Bus 的两种真实调用（按 AST 的 Call 节点判，不按文本子串）。
_CONFIRMATION_GATE_CALLS = frozenset({"dispatch", "ui_route"})
#: 本地二段式确认的形参名。判据挂在**函数签名**上，不挂 docstring 措辞。
_CONFIRMATION_PARAM_NAMES = frozenset({"confirm", "quote_id", "confirmation_token"})
_GATE_RAISE_RE = re.compile(r"raise\s+(?:HTTPException|_payment_confirm_required)\b")


def _router_prefix_for_file(path: Path, text: str) -> str:
    """推断该文件路由的最终 URL 前缀（含 main.py 二次挂载）。"""
    declared = ""
    match = _ROUTER_DECL_RE.search(text)
    if match:
        prefix_match = _PREFIX_ARG_RE.search(match.group("args") or "")
        if prefix_match:
            declared = prefix_match.group(1)
    rel = path.relative_to(APP_DIR).as_posix()
    if rel.startswith("agent/"):
        # agent.api: APIRouter(prefix="/agent") + main include_router(..., prefix="/api")
        return "/api" + (declared or "/agent")
    if rel.startswith("mcp/"):
        # mcp.server: 挂在根路径 /mcp
        return declared or ""
    if declared.startswith("/api"):
        return declared
    return "/api" + declared


def _normalize_route_path(prefix: str, route_path: str) -> str:
    if not route_path.startswith("/"):
        route_path = "/" + route_path
    full_path = f"{prefix}{route_path}"
    if len(full_path) > 1 and full_path.endswith("/"):
        full_path = full_path.rstrip("/")
    while "//" in full_path:
        full_path = full_path.replace("//", "/")
    return full_path


#: 会改变数据的 HTTP 方法。GET 不在内——它不需要能力分类。
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def discover_mutating_routes(app_dir: Path = APP_DIR) -> list[str]:
    """从源码装饰器静态发现 mutating 路由（不启动 FastAPI / DB）。

    走 ``_iter_route_endpoints`` 的 AST 遍历，不再自己用正则扫一遍。原先这里
    用的 ``_DECORATOR_RE`` 只认 ``@router.`` 与 ``@app.``，**认不出
    ``@public_router.``**——于是 ``app/payments/routes.py`` 的两个渠道回调
    （``/api/payments/notify/wechat`` 与 ``/notify/alipay``）压根没被扫到，
    能力覆盖闸门对它们完全沉默。而公开路由恰恰是最该被分类的一类：它们不挂
    会话鉴权，验签是唯一防线。``app/system_api.py`` 的 ``public_router`` 同样
    受影响。

    同一份判据不该有两套实现：AST 那条路径的 ``_ROUTER_NAMES`` 早就包含
    ``public_router``，也不受多行装饰器影响，而这里的正则要求方法名与路径同行。
    两套并存必然漂移，本次就是已经漂了——统一到 AST 一份。
    """
    return sorted(
        f"{method} {full_path}"
        for method, full_path, _src in _iter_route_endpoints(app_dir)
        if method in _MUTATING_METHODS
    )


def _route_decorator_path(dec: ast.expr) -> tuple[str, str] | None:
    """从单个装饰器 AST 节点里取出 ``(METHOD, path)``；不是路由装饰器则 None。"""
    if not isinstance(dec, ast.Call):
        return None
    func = dec.func
    if not (isinstance(func, ast.Attribute) and func.attr in _ROUTE_METHODS):
        return None
    if not (isinstance(func.value, ast.Name) and func.value.id in _ROUTER_NAMES):
        return None
    if not dec.args or not isinstance(dec.args[0], ast.Constant) or not isinstance(dec.args[0].value, str):
        return None
    return func.attr.upper(), dec.args[0].value


def _iter_route_endpoints(app_dir: Path = APP_DIR):
    """AST 遍历每条 mutating 路由，产出 ``(METHOD, 完整 URL, 端点函数源码)``。

    用 AST 而不是 ``_DECORATOR_RE``：那个正则要求方法名/路径在同一行，
    ``@router.post(\\n    "..."\\n)`` 这种多行装饰器（本仓库真实存在，例如
    ``app/domain/video_ops/plan.py::execute_episode_video_generation_plan``）
    会被正则漏掉；AST 不受换行影响。
    """
    for path in sorted(app_dir.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        prefix = _router_prefix_for_file(path, text)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                found = _route_decorator_path(dec)
                if found is None:
                    continue
                method, route_path = found
                full_path = _normalize_route_path(prefix, route_path)
                yield method, full_path, ast.get_source_segment(text, node) or ""


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """把函数/类/模块的首条 docstring 从树里摘掉，只留真正的代码。

    判据不能挂在源码**文本**上——docstring 和注释会撒谎，而且这里是反着撒：
    自删路由的 docstring 里写着「不经 Command Bus……不走 ``dispatch()``」，
    按文本子串匹配 ``"dispatch("`` 会把它判成「走了总线」而放行。全仓最认真
    记录「我为什么绕过总线」的那条路由，恰恰是被闸门waved through 的那条。
    """
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
    return tree


def _called_names(tree: ast.AST) -> set[str]:
    """函数体里**实际被调用**的名字（``f()`` 与 ``obj.f()`` 都算）。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _rest_route_has_confirmation_gate(src: str) -> bool:
    """判据从**代码结构**推导，不是文本子串、更不是命令名单枚举。

    两条合法形态（本仓库真实存在、各有道理）：
    1. 经 Command Bus（``dispatch()`` / ``ui_route()`` 的**真实调用**）——复用
       ``app.capabilities.policy.requires_confirmation`` 那一份判据；
    2. 自带本地二段式确认：端点收一个 ``confirm``/``quote_id`` 之类的形参，
       并在**代码里**基于它显式 ``raise``。

    为什么 account.self_delete 这类必须走第 2 种：Command Bus 的
    ``WAITING_APPROVAL`` 对 UI 调用方会被 ``frontend/src/api/client.ts`` 自动用
    approval_token 消费掉、不弹任何确认界面（2026-08-29 产品拍板下线生成前确认
    弹窗），所以真正需要用户在场点头的操作走总线反而是假保护。

    ⚠️ 2026-08-30 修正：本函数原先对**原始源码文本**做子串匹配
    （``"dispatch(" in src``、``re.search(r"\bconfirm\b", src)``），docstring
    与注释一并计入。结果是自删路由靠它 docstring 里那句「不走 ``dispatch()``」
    的解释就通过了——判据把「解释自己没做某事」读成了「做了某事」。现在先摘掉
    docstring，再按 AST 看**真实调用**与**真实形参**。
    """
    try:
        tree = ast.parse(textwrap.dedent(src))
    except SyntaxError:
        return False
    _strip_docstrings(tree)

    if _called_names(tree) & _CONFIRMATION_GATE_CALLS:
        return True

    fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)),
        None,
    )
    if fn is None:
        return False
    args = fn.args
    param_names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if not param_names & _CONFIRMATION_PARAM_NAMES:
        return False
    # 形参存在还不够：必须在代码里真的据此拦人（本仓库两种写法都算）。
    return any(
        isinstance(n, ast.Raise) for n in ast.walk(fn)
    ) and bool(_GATE_RAISE_RE.search(ast.unparse(fn)))


def find_always_confirm_routes_without_gate() -> list[str]:
    """治理闸门：catalog 里 ``confirmation=ALWAYS`` 的能力，真实 REST 路径必须有
    等价确认机制，否则风险登记只是宣称、请求路径上没有任何东西在拦
    （2026-08-30 曾有 quota.grant_video_addon 声明 ALWAYS 却在 REST 路径上零确认
    /零幂等/零审计，本函数是为了让这类缺口不必等人工审计才发现）。"""
    ensure_catalog_loaded()
    registry = get_registry()
    endpoints: dict[tuple[str, str], list[str]] = {}
    for method, full_path, src in _iter_route_endpoints():
        endpoints.setdefault((method, full_path), []).append(src)

    problems: list[str] = []
    for name, spec in sorted(registry.commands.items()):
        if spec.confirmation != ConfirmationPolicy.ALWAYS:
            continue
        for rr in spec.rest_routes:
            method, path = rr.split(" ", 1)
            sources = endpoints.get((method, path))
            if not sources:
                problems.append(f"{name}: 路由 {rr} 未能在源码中定位（rest_routes 是否过期？）")
            elif not any(_rest_route_has_confirmation_gate(s) for s in sources):
                problems.append(
                    f"{name}: 路由 {rr} 声明 confirmation=ALWAYS，"
                    "但源码既不经 Command Bus 也没有本地确认闸门"
                )
    return problems


def _is_resource_deletion(spec: CommandSpec) -> bool:
    """「删除资源」判据：``side_effect`` 前缀 ``deletes_``/``purges_`` 表示不可逆
    销毁资源本身。``soft_deletes_``（如 ``project.delete`` 移入回收站、
    ``account.admin_soft_delete`` 移入 30 天保留期）刻意不算——两者都各自配一个
    ``restore`` 命令，是可撤销的，本来就不该弹「不可撤销」的确认框。

    没有用 HTTP 方法（DELETE）单独判：本仓库同时有 (a) DELETE 方法但可撤销
    的路由（``account.admin_soft_delete``、``project.delete``，各自都配
    restore）、也有 (b) POST 方法但确实销毁数据的路由（``video.clear_shot``
    等走 ``POST .../clear-artifacts``，是这套 REST 命名的既有写法）——method
    在这两个方向上都会误判，``side_effect`` 才是这里唯一自解释、和业务语义
    直接绑定的字段。也没有用 ``risk == R3_DESTRUCTIVE``：``delivery.review``、
    ``system.update_settings``、``video.adopt_version`` 都是 R3 但都不是删除
    资源，用 risk 会把「破坏性」和「删除」两个不同的轴混成一个，反而把非删除
    操作也判进「必须弹窗」。
    """
    return spec.side_effect.startswith(("deletes_", "purges_"))


def find_confirmation_policy_mismatches() -> list[str]:
    """产品规则闸门（2026-08-30 拍板）：除了删除资源，否则不需要弹窗。

    catalog 的 ``confirmation`` 必须与这条规则双向一致，否则要么是「删资源却
    不拦」，要么是「登记了 ALWAYS 但对浏览器用户从不生效」的又一个空头承诺——
    Command Bus 的 ``WAITING_APPROVAL`` 对浏览器调用方会被
    ``frontend/src/api/client.ts`` 自动用 ``approval_token`` 消费掉、不弹任何
    确认界面，ALWAYS 对浏览器调用方唯一还生效的场景就是真正的资源删除（client.ts
    专门对这一档改成不自动消费，改由页面弹出确认框，见该文件与
    ``find_always_confirm_routes_without_gate`` 的判据说明）。
    """
    ensure_catalog_loaded()
    registry = get_registry()
    problems: list[str] = []
    for name, spec in sorted(registry.commands.items()):
        deletion = _is_resource_deletion(spec)
        always = spec.confirmation == ConfirmationPolicy.ALWAYS
        if deletion and not always:
            problems.append(
                f"{name}: side_effect={spec.side_effect!r} 是删除资源，"
                "但 confirmation != ALWAYS——用户删除前不会被拦"
            )
        if always and not deletion:
            problems.append(
                f"{name}: confirmation=ALWAYS 但 side_effect={spec.side_effect!r} "
                "不是删除资源——ALWAYS 对浏览器调用方无效（client.ts 自动消费 "
                "approval_token），这是又一个空头承诺"
            )
    return problems


def build_coverage_report() -> dict[str, Any]:
    ensure_catalog_loaded()
    registry = get_registry()
    routes = discover_mutating_routes()
    covered: list[dict[str, str]] = []
    exempted: list[dict[str, str]] = []
    missing: list[str] = []

    for route in routes:
        if route in registry.rest_bindings:
            covered.append({"route": route, "capability": registry.rest_bindings[route]})
        elif route in registry.rest_exemptions:
            exempted.append({"route": route, "reason": registry.rest_exemptions[route]})
        else:
            missing.append(route)

    snapshot = registry.coverage_snapshot()
    return {
        "ok": not missing,
        "mutating_routes": len(routes),
        "covered": len(covered),
        "exempted": len(exempted),
        "missing": missing,
        "covered_detail": covered,
        "exempted_detail": exempted,
        "registry": snapshot,
        "prd_section5_checklist": {
            "domain_commands": snapshot["counts"]["commands"],
            "resources": snapshot["counts"]["resources"],
            "ui_intents": snapshot["counts"]["ui_intents"],
            "human_only": snapshot["counts"]["human_only"],
        },
    }


def write_coverage_json(target: Path | None = None) -> Path:
    report = build_coverage_report()
    out = target or (ROOT / "data" / "reports" / "capability-coverage.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


def assert_full_coverage() -> dict[str, Any]:
    report = build_coverage_report()
    if report["missing"]:
        missing = "\n".join(f"  - {route}" for route in report["missing"])
        raise AssertionError(
            "Unclassified mutating endpoints (register Command/Human-only or exempt with reason):\n"
            + missing
        )
    return report


def validate_catalog_integrity() -> list[str]:
    """额外合同：每个 Domain Tool 元数据完整；Human-only 有原因。"""
    ensure_catalog_loaded()
    registry = get_registry()
    errors: list[str] = []
    for name, spec in registry.commands.items():
        if not spec.title or not spec.description:
            errors.append(f"{name}: missing title/description")
        if not spec.scopes:
            errors.append(f"{name}: empty scopes")
        if not spec.side_effect:
            errors.append(f"{name}: empty side_effect")
        if not spec.version:
            errors.append(f"{name}: empty version")
        try:
            schema = spec.input_model.model_json_schema()
            if "properties" not in schema and schema.get("type") != "object":
                errors.append(f"{name}: input schema is not an object")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: cannot export JSON schema: {exc}")
        if name in {
            "project.delete", "bible.generate", "storyboard.confirm", "video.generate_shot",
            "delivery.review", "run.control",
        } and spec.handler is None:
            errors.append(f"{name}: required handler missing")

    for name, spec in registry.human_only.items():
        if not spec.reason.strip():
            errors.append(f"{name}: human-only requires reason")

    required_tools = {
        "project.import_novel", "project.delete",
        "bible.generate", "bible.update", "bible.cancel",
        "portrait.update_prompt", "portrait.generate", "portrait.cancel", "portrait.regenerate_view",
        "scene.generate_bible", "scene.generate_refs", "scene.update_prompt", "scene.cancel_refs", "scene.regenerate_view", "scene.adopt_candidate",
        "episode.plan",
        "screenplay.generate", "screenplay.resume", "screenplay.repair_draft", "screenplay.generate_batch", "screenplay.update", "screenplay.delete", "screenplay.cancel",
        "storyboard.generate", "storyboard.generate_batch", "shot.update", "storyboard.confirm", "storyboard.cancel",
        "video.generate_episode", "video.complete_episode", "video.complete_project", "video.generate_shot", "video.stop_shot", "video.adopt_version",
        "video.clear_episode", "video.clear_shot", "video.delete_version", "video.repair_stale_assets", "reference.review",
        "delivery.concatenate", "delivery.check", "delivery.create_package", "delivery.review",
        "delivery.submit_feedback", "run.control", "system.model_test",
    }
    missing_tools = sorted(required_tools - set(registry.commands))
    errors.extend(f"missing required PRD tool: {name}" for name in missing_tools)

    required_ui = {
        "ui.navigate", "ui.select_shot", "ui.select_version", "ui.open_evidence",
        "ui.open_delivery", "ui.open_download", "ui.open_credentials", "ui.request_directory_grant",
    }
    missing_ui = sorted(required_ui - set(registry.ui_intents))
    errors.extend(f"missing required UI intent: {name}" for name in missing_ui)

    return errors
