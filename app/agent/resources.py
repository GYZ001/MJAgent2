"""只读 Resource 读取器：按 `manju://` URI 读取项目/剧集/Run/Artifact 摘要。

复用 MCP Resources 读取器，保证 Agent / MCP 读到同一份数据；
返回业务 content（已脱敏），杜绝密钥进入模型上下文（PRD §9.2 / §12.1）。
"""
from __future__ import annotations

from typing import Any

from app.agent.redaction import redact_value


class ResourceNotFound(Exception):
    """URI 格式合法但目标不存在，供上层如实回复模型而不是抛 500。"""


class ResourceUriInvalid(Exception):
    """URI 不匹配任何已注册 Resource Template。"""


def read_resource(uri: str) -> dict[str, Any]:
    """按 URI 分发到具体只读查询；密钥字段在返回前统一剔除。"""
    from app.mcp.resources import ResourceError, read_resource as mcp_read

    uri = (uri or "").strip()
    try:
        envelope = mcp_read(uri)
    except ResourceError as exc:
        if exc.code in {"not_found"} or exc.status == 404:
            raise ResourceNotFound(exc.message) from exc
        raise ResourceUriInvalid(exc.message) from exc

    data = envelope.get("content")
    if not isinstance(data, dict):
        data = {k: v for k, v in envelope.items() if k not in {"mimeType"}}
    # 兼容旧 Agent 合同：projects 列表使用 projects 键
    if envelope.get("name") == "projects" and "items" in data and "projects" not in data:
        data = {**data, "projects": data["items"]}
    meta = {
        "uri": envelope.get("uri", uri),
        "resource": envelope.get("name"),
        "version": envelope.get("version"),
        "trust_level": envelope.get("trust_level"),
    }
    return redact_value({**meta, **data})
