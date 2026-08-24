"""RBAC 第四阶段：HTTP 边界的工作空间隔离。

把一个请求的路径参数解析成它归属的 project，再翻译成 workspace，交给
``Principal.can_access`` 判断。真源表结构以 ``app/db.py`` 为准，这里不重复
定义、只查询。

解析结果分四种（见 ``ScopeResolution``）：
- ``workspace``：命中了归属的项目，按常规工作空间成员校验。
- ``admin_only``：对象天生没有项目归属（``error_logs``/``mcp_tokens``），或者
  归属字段是启发式回填、回填不到时为 NULL（``provider_calls.project_id``）——
  两种都只放行系统管理员，绝不 fail open。
- ``creator``：对象允许为空归属，但保留了创建者（``agent_conversations``
  没有 ``project_id`` 时），只放行创建者本人或系统管理员。
- ``none``：路径参数里没有本模块认识的归属参数，或者参数指向的对象根本不
  存在——两种都不拦截，交给路由自身的业务逻辑处理（后者会走到路由自己的
  404，不会被这里抢先掩盖成别的语义）。

**不要在同步依赖里写 ContextVar**：`app/local_session.py::bind_request_principal`
的教训是 FastAPI 用 `run_in_threadpool` 跑同步依赖，线程内对 ContextVar 的写入
不会传回请求上下文。这里的依赖只*读* `get_current_principal()`
（读取在线程拷贝的 context 里仍然可见，只有写不会传回），
需要跨调用缓存的解析结果一律存进 `request.state`，而不是 ContextVar。
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.auth.principal import get_current_principal
from app.db import get_conn

_DENIED_DETAIL = "对象不存在"


@dataclass(frozen=True)
class ScopeResolution:
    kind: str  # "workspace" | "admin_only" | "creator" | "none"
    value: str | None = None


_UNRESOLVED = ScopeResolution("none")
_ADMIN_ONLY = ScopeResolution("admin_only")


def _workspace_of_project(conn, project_id: str | None) -> ScopeResolution:
    if not project_id:
        return _UNRESOLVED
    row = conn.execute("SELECT workspace_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        return _UNRESOLVED
    return ScopeResolution("workspace", row["workspace_id"])


def _episode_project(conn, episode_id: str) -> str | None:
    row = conn.execute("SELECT project_id FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return row["project_id"] if row else None


def _shot_project(conn, shot_id: str) -> str | None:
    row = conn.execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        return None
    return _episode_project(conn, row["episode_id"])


def _version_project(conn, version_id: str) -> str | None:
    row = conn.execute("SELECT shot_id FROM shot_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        return None
    return _shot_project(conn, row["shot_id"])


def _job_project(conn, job_id: str) -> str | None:
    row = conn.execute("SELECT project_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    return row["project_id"] if row else None


def _package_project(conn, package_id: str) -> str | None:
    row = conn.execute(
        "SELECT episode_id FROM delivery_packages WHERE id=?", (package_id,)
    ).fetchone()
    if not row:
        return None
    return _episode_project(conn, row["episode_id"])


def _scope_pointer_project(conn, scope_type: str | None, scope_id: str | None) -> str | None:
    """``workflow_runs``/``artifacts`` 共用的 ``scope_type``+``scope_id`` 解释。

    与 ``app.domain.projects._delete_scoped_evidence``/``_scope_ids`` 的口径一致：
    真正的归属对象是 ``scope_id`` 冒号前的那一段 id，``scope_type`` 只是"这段 id
    属于哪张表"的提示，不是权威依据——``storyboard_checkpoint`` 的 scope_id 前缀
    实际是 episode_id，``reference_asset`` 的前缀实际是 project_id。未知
    scope_type 一律按 project -> episode -> shot 顺序兜底尝试，一个都不命中就
    视为无法归属（例如全局校准作用域 ``global-narrative-continuity``），交给
    上层 fail closed，不去猜。
    """
    if not scope_id:
        return None
    base_id = scope_id.split(":", 1)[0]
    if scope_type == "project":
        row = conn.execute("SELECT id FROM projects WHERE id=?", (base_id,)).fetchone()
        return base_id if row else None
    if scope_type == "shot":
        return _shot_project(conn, base_id)
    if scope_type == "episode":
        return _episode_project(conn, base_id)
    row = conn.execute("SELECT id FROM projects WHERE id=?", (base_id,)).fetchone()
    if row:
        return base_id
    project_id = _episode_project(conn, base_id)
    if project_id:
        return project_id
    return _shot_project(conn, base_id)


def _run_project(conn, run_id: str) -> str | None:
    row = conn.execute(
        "SELECT scope_type, scope_id FROM workflow_runs WHERE id=?", (run_id,)
    ).fetchone()
    if not row:
        return None
    return _scope_pointer_project(conn, row["scope_type"], row["scope_id"])


def _artifact_project(conn, artifact_id: str) -> str | None:
    row = conn.execute(
        "SELECT scope_type, scope_id FROM artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    if not row:
        return None
    return _scope_pointer_project(conn, row["scope_type"], row["scope_id"])


def _call_scope(conn, call_id: str) -> ScopeResolution:
    try:
        call_pk = int(call_id)
    except (TypeError, ValueError):
        return _UNRESOLVED
    row = conn.execute("SELECT project_id FROM provider_calls WHERE id=?", (call_pk,)).fetchone()
    if not row:
        return _UNRESOLVED
    project_id = row["project_id"]
    if not project_id:
        # provider_calls.project_id 是启发式回填的历史列，回填不到就是 NULL；
        # 归属不明的调用记录只能给系统管理员看。
        return _ADMIN_ONLY
    return _workspace_of_project(conn, project_id)


def _conversation_scope(conn, conversation_id: str) -> ScopeResolution:
    row = conn.execute(
        "SELECT project_id, created_by FROM agent_conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    if not row:
        return _UNRESOLVED
    if row["project_id"]:
        return _workspace_of_project(conn, row["project_id"])
    creator = row["created_by"]
    return ScopeResolution("creator", creator) if creator else _ADMIN_ONLY


def _turn_scope(conn, turn_id: str) -> ScopeResolution:
    row = conn.execute("SELECT conversation_id FROM agent_turns WHERE id=?", (turn_id,)).fetchone()
    if not row:
        return _UNRESOLVED
    return _conversation_scope(conn, row["conversation_id"])


def _tool_call_scope(conn, tool_call_id: str) -> ScopeResolution:
    row = conn.execute(
        "SELECT turn_id FROM agent_tool_calls WHERE id=?", (tool_call_id,)
    ).fetchone()
    if not row:
        return _UNRESOLVED
    return _turn_scope(conn, row["turn_id"])


# 解析顺序即优先级：命中第一个就地返回，不做无谓的多路查询。project_id 永远
# 排最前——嵌套路由（``/projects/{project_id}/...``）即便同一条路径上还带着
# artifact_id/run_id 等参数，也应该用近在咫尺的 project_id，不必为了别的参数
# 再多查一次库（那些参数各自的业务路由自己会做更细的项目内校验）。
_PATH_PARAM_ORDER = (
    "project_id", "episode_id", "shot_id", "version_id", "job_id", "package_id",
    "run_id", "artifact_id", "call_id",
    "conversation_id", "turn_id", "tool_call_id",
    "error_id", "token_id",
)


def _resolve_one(conn, name: str, raw: str) -> ScopeResolution:
    if name == "project_id":
        return _workspace_of_project(conn, raw)
    if name == "episode_id":
        return _workspace_of_project(conn, _episode_project(conn, raw))
    if name == "shot_id":
        return _workspace_of_project(conn, _shot_project(conn, raw))
    if name == "version_id":
        return _workspace_of_project(conn, _version_project(conn, raw))
    if name == "job_id":
        return _workspace_of_project(conn, _job_project(conn, raw))
    if name == "package_id":
        return _workspace_of_project(conn, _package_project(conn, raw))
    if name == "run_id":
        return _workspace_of_project(conn, _run_project(conn, raw))
    if name == "artifact_id":
        return _workspace_of_project(conn, _artifact_project(conn, raw))
    if name == "call_id":
        return _call_scope(conn, raw)
    if name == "conversation_id":
        return _conversation_scope(conn, raw)
    if name == "turn_id":
        return _turn_scope(conn, raw)
    if name == "tool_call_id":
        return _tool_call_scope(conn, raw)
    if name in ("error_id", "token_id"):
        # 没有任何项目归属字段（error_logs/mcp_tokens），一律只放行系统管理员。
        return _ADMIN_ONLY
    return _UNRESOLVED


def resolve_request_scope(path_params: dict, cache: dict) -> ScopeResolution:
    """把请求路径参数解析为它所归属的 workspace（或 admin_only/creator 特例）。

    ``scene_name``/``character_name`` 永远嵌套在 ``/projects/{project_id}/...``
    下，project_id 命中优先级最高，走到它们之前就已经返回，不需要单独处理。
    ``cache`` 由调用方（依赖）在单个请求生命周期内持有，避免同一参数被反复解析。
    """
    for name in _PATH_PARAM_ORDER:
        raw = path_params.get(name)
        if not raw:
            continue
        cache_key = f"{name}:{raw}"
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        resolution = _resolve_one(get_conn(), name, str(raw))
        cache[cache_key] = resolution
        return resolution
    return _UNRESOLVED


def _request_cache(request: Request) -> dict:
    cache = getattr(request.state, "authz_scope_cache", None)
    if cache is None:
        cache = {}
        request.state.authz_scope_cache = cache
    return cache


def require_workspace_access(request: Request) -> None:
    """路由级依赖：非系统管理员只能触达自己所在 workspace 名下的对象。

    挂在 ``app.main`` 里跟 ``_SESSION_DEPS`` 同一批 ``include_router`` 上，
    执行顺序在 ``require_local_session`` 之后——真正走到这里时 Principal
    必定已经由中间件（``bind_request_principal``）解析好，可以放心只读
    ``get_current_principal()``。``principal is None`` 与 Command Bus 的既有
    约定保持一致：视为未挂会话闸门的内部调用，直接放行（本依赖只会被注册在
    已经挂了 ``require_local_session`` 的路由组上，正常请求不会走到这条分支）。
    """
    principal = get_current_principal()
    if principal is None or principal.is_system_admin:
        return
    resolution = resolve_request_scope(request.path_params, _request_cache(request))
    if resolution.kind == "none":
        return
    if resolution.kind == "workspace" and principal.can_access(resolution.value):
        return
    if resolution.kind == "creator" and resolution.value == principal.user_id:
        return
    # 统一 404，不用 403：既不能让外部区分「对象不存在」和「对象存在但你无权」，
    # 也匹配现有约定（tests/test_project_observability.py 对跨项目对象一律断言 404）。
    raise HTTPException(404, _DENIED_DETAIL)
