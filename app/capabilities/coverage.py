"""扫描 FastAPI mutating 路由，对照 Capability Registry 覆盖率。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.registry import get_registry

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


def discover_mutating_routes(app_dir: Path = APP_DIR) -> list[str]:
    """从源码装饰器静态发现 mutating 路由（不启动 FastAPI / DB）。"""
    found: set[str] = set()
    for path in sorted(app_dir.rglob("*.py")):
        if path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8")
        prefix = _router_prefix_for_file(path, text)
        for match in _DECORATOR_RE.finditer(text):
            method = match.group(1).upper()
            route_path = match.group(2)
            if not route_path.startswith("/"):
                route_path = "/" + route_path
            full_path = f"{prefix}{route_path}"
            if len(full_path) > 1 and full_path.endswith("/"):
                full_path = full_path.rstrip("/")
            # 规范化重复斜杠
            while "//" in full_path:
                full_path = full_path.replace("//", "/")
            found.add(f"{method} {full_path}")
    return sorted(found)


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
