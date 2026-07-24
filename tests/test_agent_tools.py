"""对话 Agent 的 OpenAI tools 数组：暴露白名单 + 内部字段剔除。"""
from __future__ import annotations

import pytest

from app.agent import tools as agent_tools
from app.capabilities import ensure_catalog_loaded
from app.capabilities.tool_schemas import INTERNAL_INPUT_FIELDS


@pytest.fixture(autouse=True)
def _load_catalog(tmp_path, monkeypatch):
    from app import db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "agent-tools-test.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    ensure_catalog_loaded()
    agent_tools.reset_tools_cache_for_tests()
    yield


def _names(tools: list[dict]) -> set[str]:
    return {t["function"]["name"] for t in tools}


def test_build_agent_tools_exposes_resource_read_and_domain_commands() -> None:
    tools = agent_tools.build_agent_tools()
    names = _names(tools)
    assert agent_tools.RESOURCE_READ_TOOL_NAME in names
    assert "project.delete" in names
    assert "bible.generate" in names
    # admin_only 命令绝不下发给模型（与 MCP 同一张白名单）。
    assert "system.update_settings" not in names
    assert "system.model_create" not in names
    # 每个工具都是合法的 OpenAI function 定义。
    for tool in tools:
        assert tool["type"] == "function"
        assert tool["function"]["name"]
        assert tool["function"]["parameters"]["type"] == "object"


def test_agent_tool_schema_strips_internal_protocol_fields() -> None:
    tools = agent_tools.build_agent_tools()
    delete = next(t for t in tools if t["function"]["name"] == "project.delete")
    props = delete["function"]["parameters"].get("properties", {})
    for field in INTERNAL_INPUT_FIELDS:
        assert field not in props, f"内部字段 {field} 不应暴露给模型"
    # 领域参数仍在。
    assert "project_id" in props


def test_resource_read_tool_lists_uri_templates() -> None:
    tools = agent_tools.build_agent_tools()
    resource_read = next(t for t in tools if t["function"]["name"] == agent_tools.RESOURCE_READ_TOOL_NAME)
    description = resource_read["function"]["description"]
    assert "manju://" in description
    assert resource_read["function"]["parameters"]["required"] == ["uri"]
