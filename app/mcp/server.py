"""`/mcp` Streamable HTTP（简化版）：手写 JSON-RPC over HTTP，不引入 MCP SDK 依赖。

对齐 PRD AGENT_MCP_CAPABILITY §9/§12：
- 本地绑定 + Origin allowlist + Bearer token + scope + 简单限流；
- Tools/Resources/Prompts 都只是协议适配，真正的风险判定发生在 Command Bus；
- 未获批准的高风险 Tool 调用绝不假装成功，只返回结构化的 waiting_approval 结果。
"""
from __future__ import annotations

import json
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.auth.principal import Principal, set_current_principal
from app.capabilities.loader import ensure_catalog_loaded
from app.mcp import auth, prompts, resources, tools
from app.mcp.errors import ForbiddenError, McpError
from app.mcp.rate_limit import RateLimitExceeded, get_rate_limiter

router = APIRouter()

PROTOCOL_VERSION = "2025-11-25"
SERVER_INFO = {"name": "manju-agent-mcp", "version": "0.1.0"}

# 只本地绑定；额外用 Origin allowlist 防 DNS rebinding（PRD §12.1）。可用环境变量扩展本机前端端口。
_DEFAULT_ORIGINS = {"http://localhost:5230", "http://127.0.0.1:5230"}
_EXTRA_ORIGINS = {
    origin.strip()
    for origin in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
}
ALLOWED_ORIGINS = frozenset(_DEFAULT_ORIGINS | _EXTRA_ORIGINS)

_READ_SCOPE = {"manju:read"}


def _origin_allowed(origin: str | None) -> bool:
    # 无 Origin 头的调用方（例如非浏览器的本机 MCP 客户端）不能靠“没有 Origin”绕过认证，
    # 但也没有可校验的浏览器来源；仍然强制走下面的 Bearer token 校验。
    if origin is None:
        return True
    return origin.rstrip("/") in ALLOWED_ORIGINS


def _rpc_error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _rpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _require_scope(claims: auth.TokenClaims, required: set[str]) -> None:
    if not (required & claims.scopes):
        raise ForbiddenError(f"token 缺少所需 scope：{sorted(required)}")


def _principal_from_claims(claims: auth.TokenClaims) -> Principal:
    """把 Bearer token 的 claims 转成账号级隔离咽喉点认识的 Principal。

    ``app.domain.common._project_or_404``/``_episode_or_404``（几乎所有
    bible/screenplay/storyboard/video 领域函数的项目存在性入口）与
    ``app.domain.projects.list_projects`` 只认 ``app.auth.principal.
    get_current_principal()``；HTTP 路径由中间件（``app.main::
    _inject_session_and_approval`` -> ``bind_request_principal``）在异步请求
    上下文里注入。MCP 走独立的 Bearer token 鉴权，不经那条中间件，此前
    ``get_current_principal()`` 在 MCP 请求里恒为 None——domain 层把它当成
    「未挂鉴权闸门的内部调用」直接放行（``_assert_principal_owns`` 的既有
    约定），等于任何 token 都能触达任意账号的项目。这里复用同一个
    ContextVar（在 ``mcp_endpoint`` 这个 async 函数体内直接写，不经
    ``run_in_threadpool``/``Depends``，不会重蹈「同步依赖写 ContextVar
    静默失效」的坑），不新造第二套判据。

    token 未绑定账号（``claims.owner_user_id`` 为空——bootstrap token、或
    脚本/测试在无 HTTP 会话上下文时直接调用 ``create_token()``）时，合成一个
    不会匹配任何真实 ``owner_user_id`` 的身份：``Principal.owns()`` 对它恒为
    False，结构上拒绝一切项目级资源，而不是放行。「空集合不等于无需检查」——
    未知归属必须解释成「什么都不合法」，不能解释成「不受限制」。
    """
    if claims.owner_user_id:
        return Principal(
            user_id=claims.owner_user_id,
            username=claims.owner_username or claims.owner_user_id,
            is_system_admin=claims.is_system_admin,
        )
    return Principal(
        user_id=f"mcp-unbound:{claims.token_id}",
        username="mcp-unbound",
        is_system_admin=False,
    )


async def _dispatch(method: str, params: dict[str, Any], claims: auth.TokenClaims) -> Any:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": SERVER_INFO,
            "instructions": (
                "漫剧 Agent MCP：所有 Tool 调用都会重新走服务端 Policy/Approval，"
                "客户端声明的 annotations（如 readOnlyHint）不会降低真实风险判定。"
            ),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        _require_scope(claims, _READ_SCOPE)
        return {"tools": tools.list_mcp_tools()}
    if method == "tools/call":
        name = params.get("name")
        if not name:
            raise McpError(-32602, "missing required param: name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise McpError(-32602, "arguments must be an object")
        return await tools.call_tool(name, arguments, claims=claims)
    if method == "resources/list":
        _require_scope(claims, _READ_SCOPE)
        return {"resources": resources.list_resources()}
    if method == "resources/templates/list":
        _require_scope(claims, _READ_SCOPE)
        return {"resourceTemplates": resources.list_resource_templates()}
    if method == "resources/read":
        _require_scope(claims, _READ_SCOPE)
        uri = params.get("uri")
        if not uri:
            raise McpError(-32602, "missing required param: uri")
        try:
            resource = resources.read_resource(uri)
        except resources.ResourceError as exc:
            raise McpError(-32001, exc.message, {"code": exc.code}) from exc
        return {
            "contents": [
                {
                    "uri": resource["uri"],
                    "mimeType": resource["mimeType"],
                    "text": json.dumps(resource["content"], ensure_ascii=False),
                    "_meta": {
                        "name": resource["name"],
                        "version": resource["version"],
                        "content_hash": resource["content_hash"],
                        "trust_level": resource["trust_level"],
                    },
                }
            ]
        }
    if method == "prompts/list":
        _require_scope(claims, _READ_SCOPE)
        return {"prompts": prompts.list_prompts()}
    if method == "prompts/get":
        _require_scope(claims, _READ_SCOPE)
        name = params.get("name")
        if not name:
            raise McpError(-32602, "missing required param: name")
        try:
            return prompts.get_prompt(name, params.get("arguments") or {})
        except KeyError as exc:
            raise McpError(-32602, str(exc)) from exc
        except prompts.PromptError as exc:
            raise McpError(-32602, str(exc)) from exc
    raise McpError(-32601, f"method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    ensure_catalog_loaded()

    origin = request.headers.get("origin")
    if not _origin_allowed(origin):
        return JSONResponse({"error": "origin_not_allowed", "message": f"Origin 不在允许列表：{origin}"}, status_code=403)

    try:
        claims = auth.validate_bearer(request.headers.get("authorization"))
    except auth.AuthError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)

    # Principal 必须在这里（本函数的 async 上下文里）直接写 ContextVar，不能
    # 经 Depends（FastAPI 用 run_in_threadpool 跑同步依赖，线程内的写入不会
    # 传回请求上下文）——与 app.main::_inject_session_and_approval 中间件对
    # HTTP 会话的处理是同一条纪律，见 _principal_from_claims 的文档字符串。
    set_current_principal(_principal_from_claims(claims))
    try:
        return await _handle_rpc_request(request, claims)
    finally:
        set_current_principal(None)


async def _handle_rpc_request(request: Request, claims: auth.TokenClaims) -> JSONResponse:
    try:
        get_rate_limiter().check(claims.token_id)
    except RateLimitExceeded as exc:
        return JSONResponse({"error": "rate_limited", "message": str(exc)}, status_code=429)

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return JSONResponse(_rpc_error(None, -32700, "invalid JSON body"))

    if isinstance(payload, list):
        return JSONResponse(_rpc_error(None, -32600, "batch requests are not supported in this minimal transport"))
    if not isinstance(payload, dict):
        return JSONResponse(_rpc_error(None, -32600, "request body must be a JSON-RPC object"))

    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse(_rpc_error(request_id, -32602, "params must be an object"))
    if not method or not isinstance(method, str):
        return JSONResponse(_rpc_error(request_id, -32600, "missing or invalid method"))

    try:
        result = await _dispatch(method, params, claims)
    except ForbiddenError as exc:
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=403)
    except McpError as exc:
        return JSONResponse(_rpc_error(request_id, exc.code, exc.message, exc.data))
    except Exception:  # noqa: BLE001 — 协议层绝不把内部堆栈/密钥回显给客户端
        return JSONResponse(_rpc_error(request_id, -32603, "internal error"))

    return JSONResponse(_rpc_result(request_id, result))


@router.get("/mcp")
async def mcp_info() -> JSONResponse:
    """便于人工在浏览器确认端点存在；不返回任何业务数据，且仍需 POST + 鉴权才能真正调用。"""
    return JSONResponse(
        {
            "protocol": "mcp-streamable-http-minimal",
            "protocolVersion": PROTOCOL_VERSION,
            "server": SERVER_INFO,
            "note": "POST JSON-RPC 2.0 请求到本地址；需要 Origin allowlist + Bearer token。",
        }
    )
