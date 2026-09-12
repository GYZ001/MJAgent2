"""EP-01 第二阶段挂账：``project_grants``/``org_admin`` 判定的请求级缓存。

``app.domain.common._principal_access_check`` 在"非 owner 但有授权"这条路径
上，此前每次调用都要打三次库（``project_grant_hit``/``project_org_id``/
``user_has_org_admin``），且完全没有缓存——同一个请求里，一次列表页渲染可能
对同一个 ``project_id`` 反复判定多次（嵌套对象各自调 ``_project_or_404``/
``_episode_or_404`` 等）。

复用 ``app.authz.resolve`` 的既有写法：缓存必须落在 ``request.state``，不能
落在 ContextVar 本身——``app/authz/resolve.py`` 模块顶部已经写明原因（FastAPI
用 ``run_in_threadpool`` 跑同步依赖，线程内对 ContextVar 的写入不会传回请求
上下文）。这里能拿到 ``Request`` 对象，是因为 ``app.local_session.
bind_current_request()`` 在 HTTP 中间件（真正的 async 请求上下文，不是
threadpool）里把它写进了一个 ContextVar；本模块只*读*那个引用（读取在线程
拷贝的 context 里仍然可见，会丢失的只有"写"），再把结果写进这个引用指向的
同一个 ``request.state`` 对象——写的是那个堆对象的属性，不是 ContextVar 本身
，因此在任何线程里执行都是安全的。

没有 ``Request``（后台任务、CLI、未挂中间件直接调用 domain 函数的测试）时
``get_current_request()`` 返回 ``None``，退化为不缓存、每次都查库——功能上
与 EP-01 第一阶段完全一致，只是丢失这一层性能优化，不影响正确性。
"""
from __future__ import annotations

from app.db import get_conn
from app.local_session import get_current_request
from app.orgs import store as orgs_store


def project_access_allowed(
    project_id: str, user_id: str, team_ids: frozenset[str], org_id: str | None,
) -> bool:
    """EP-01 §8 的"project_grants 命中 OR 同组织 org_admin"判定，带请求级缓存。

    只在真正命中缓存时才跳过查库；未命中（含没有 Request 的场景）一律照旧
    执行三次查询，行为与缓存加入前逐字一致。
    """
    request = get_current_request()
    cache: dict[str, bool] | None = None
    if request is not None:
        cache = getattr(request.state, "org_access_cache", None)
        if cache is None:
            cache = {}
            request.state.org_access_cache = cache
        cached = cache.get(project_id)
        if cached is not None:
            return cached

    conn = get_conn()
    if orgs_store.project_grant_hit(conn, project_id, user_id, team_ids):
        result = True
    else:
        project_org_id = orgs_store.project_org_id(conn, project_id)
        result = bool(
            project_org_id is not None
            and project_org_id == org_id
            and orgs_store.user_has_org_admin(conn, user_id, project_org_id)
        )

    if cache is not None:
        cache[project_id] = result
    return result
