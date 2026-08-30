"""Agent / Capability 只读 API（M0：能力目录；M1 扩展 conversation/SSE）。"""
from __future__ import annotations

from fastapi import APIRouter

from app.capabilities.loader import ensure_catalog_loaded
from app.capabilities.coverage import build_coverage_report
from app.capabilities.registry import get_registry

router = APIRouter(prefix="/agent", tags=["agent"])


@router.get("/capabilities")
def list_capabilities():
    """前端/Agent 展示能力与风险元数据。"""
    ensure_catalog_loaded()
    registry = get_registry()
    commands = [
        {
            "name": spec.name,
            "version": spec.version,
            "title": spec.title,
            "description": spec.description,
            "kind": spec.kind.value,
            "risk": spec.risk.value,
            "confirmation": spec.confirmation.value,
            "idempotency": spec.idempotency.value,
            "scopes": sorted(spec.scopes),
            "side_effect": spec.side_effect,
            "supports_dry_run": spec.supports_dry_run,
            "supports_cancel": spec.supports_cancel,
            "mcp_exposed": spec.mcp_exposed,
            "admin_only": spec.admin_only,
            "input_schema": spec.input_model.model_json_schema(),
            "tags": list(spec.tags),
        }
        for spec in registry.commands.values()
    ]
    return {
        "commands": commands,
        "resources": [
            {
                "name": spec.name,
                "uri_template": spec.uri_template,
                "title": spec.title,
                "description": spec.description,
                "kind": spec.kind.value,
                "risk": spec.risk.value,
                "scopes": sorted(spec.scopes),
                "mcp_exposed": spec.mcp_exposed,
            }
            for spec in registry.resources.values()
        ],
        "ui_intents": [
            {
                "name": spec.name,
                "title": spec.title,
                "description": spec.description,
                "kind": spec.kind.value,
                "intent_type": spec.intent_type,
                "mcp_exposed": spec.mcp_exposed,
            }
            for spec in registry.ui_intents.values()
        ],
        "human_only": [
            {
                "name": spec.name,
                "title": spec.title,
                "description": spec.description,
                "kind": spec.kind.value,
                "reason": spec.reason,
                "related_ui_intent": spec.related_ui_intent,
            }
            for spec in registry.human_only.values()
        ],
    }


@router.get("/capabilities/coverage")
def capability_coverage():
    """开发/CI 用覆盖快照（不含密钥）。"""
    return build_coverage_report()
