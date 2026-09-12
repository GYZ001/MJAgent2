"""``GET /metrics``——Prometheus 文本格式，手写零依赖。L5（见 app/LAYERS.toml）：
挂 APIRouter，只被 app.main 引用，与 app.audit.api/app.observability.api 同一
"路由→域" 入口角色，同一条理由判 L5。

鉴权（PRD/enterprise/EP-06_部署运维与安全基线.md §5：默认不裸奔到公网）：
系统管理员会话 **或** 独立 metrics token 二选一；都没有 401。token 用环境变量
``MJ_METRICS_TOKEN`` 而不是塞进 ``app.config``——后者行数基线已经顶到零余量
（612/612，见 app/FILE_CONVENTIONS.toml），这个读取点不属于现有任何配置分组，
没必要为了一行 env 读取去占用那份预算或触发"新模块该不该抵消存量"的争议。

不能直接把 ``Depends(require_system_admin)`` 挂在路由上：那样会让持有合法
metrics token、但没有用户会话的 Prometheus 抓取请求先一步被拒。两条允许路径
必须在同一个判定函数里短路"任一为真即放行"。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.auth.principal import get_current_principal
from app.observability import metrics_collectors, metrics_registry

router = APIRouter()

METRICS_TOKEN_ENV = "MJ_METRICS_TOKEN"
METRICS_TOKEN_HEADER = "X-Metrics-Token"


def _token_matches(request: Request) -> bool:
    configured = os.environ.get(METRICS_TOKEN_ENV, "").strip()
    if not configured:
        return False
    supplied = request.headers.get(METRICS_TOKEN_HEADER) or request.query_params.get("token") or ""
    return bool(supplied) and supplied == configured


def _is_authorized(request: Request) -> bool:
    if _token_matches(request):
        return True
    principal = get_current_principal()
    return bool(principal and principal.is_system_admin)


@router.get("/metrics")
def get_metrics(request: Request) -> PlainTextResponse:
    if not _is_authorized(request):
        # 403（PRD/enterprise/EP-06 §8 验收清单明文要求）而不是 404：明确告诉
        # 运维"这条路由存在，但你没有权限"，避免误以为指标端点根本没部署——
        # 见 CLAUDE.md「拦住用户时必须给出路」。
        raise HTTPException(
            403,
            f"缺少有效的系统管理员会话，也没有匹配的 {METRICS_TOKEN_HEADER} / ?token= "
            f"（需先设置环境变量 {METRICS_TOKEN_ENV}）",
        )
    metrics_collectors.collect_all()
    body = metrics_registry.render_prometheus_text()
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")
