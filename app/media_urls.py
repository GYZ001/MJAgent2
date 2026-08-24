"""统一的 /media URL 构造 + 访问票据（RBAC 收口 /media 的鉴权凭据）。

背景：`/media/*` 曾经零鉴权挂载，任何能连上端口的人都能按 URL 读到所有项目的
生成图片/视频。`/api/*` 已经有工作空间隔离，但浏览器的 ``<img>``/``<video>``
标签不会带自定义请求头，`/api` 用的 ``X-Manju-Session`` 头方案在结构上保护
不了 `/media`——凭据必须放进 URL 里。这里给每个 `/media` URL 追加一枚
``mt=`` 票据，`app/main.py` 的 `/media` 路由据此校验。

改造前散落着 7 处各自拼接 ``f"/media/{rel}?v=..."`` 的调用点，任何一处漏签就
会在鉴权收紧后变成 403。全部收口到本模块这一个入口，新增调用点也不会漏签。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

from app import config
from app.local_session import ensure_session_secret

_DAY_SECONDS = 86400
_TICKET_HEX_LEN = 24
_FLAG_ENV = "MJ_MEDIA_REQUIRE_TICKET"


def _day_bucket(ts: float | None = None) -> int:
    return int((time.time() if ts is None else ts) // _DAY_SECONDS)


def _sign(project_id: str, day_bucket: int) -> str:
    secret = ensure_session_secret().encode("utf-8")
    msg = f"{project_id}|{day_bucket}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()[:_TICKET_HEX_LEN]


def media_ticket_required() -> bool:
    """`/media` 是否强制校验票据；默认关闭（Todolist RBAC /media 收口）。

    关闭时 `build_media_url` 仍然签发并附带 ``mt=`` 票据，但 `/media` 路由不会
    因为票据缺失/错误拒绝请求——此时行为与改造前的裸 StaticFiles 挂载完全一致。
    这个开关的存在是为了不打断本机正在跑的多小时回归（它会不定期重启后端，
    中间任何一次部署都必须继续可用）。观察一段时间没问题后再打开这个环境变量，
    从那一刻起才真正开始拒绝无效票据；因为 URL 一直都在签发有效票据，切换本身
    不需要重新渲染页面。
    """
    raw = os.environ.get(_FLAG_ENV, "0").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def build_media_url(abs_path_or_rel: str | Path | None, *, version: str | int | None = None) -> str | None:
    """把落盘路径转成前端可访问的 `/media` URL，并附带当天的访问票据。

    ``abs_path_or_rel``：既可以是绝对落盘路径（会先转成相对 PROJECTS_DIR 的
    路径），也可以是已经相对 PROJECTS_DIR 的路径字符串（部分调用点在传进来之前
    已经自己算过一次 relative_to）。不存在/无法归属 PROJECTS_DIR 的输入返回 None，
    调用方历史上一直用 None 表示「这个资源还没生成」，此处沿用。

    ``version``：不传就不带 ``?v=``，保持个别调用点原本没有版本号的行为不变。

    票据按天分桶（而非每次请求随机），是因为 nginx 给 `/media` 配的是
    ``Cache-Control: public, max-age=31536000, immutable``，缓存键是包含
    query string 的完整 URL；随机票据会让每次响应的 URL 都不同，直接打穿缓存——
    一个页面大约 26MB 的图片会在每次轮询时重新下载一遍。同一天内同一文件的 URL
    保持稳定，才能吃到缓存命中。**不要把这里改成随机数或每次请求都变的值。**
    """
    if not abs_path_or_rel:
        return None
    path = Path(abs_path_or_rel)
    try:
        if path.is_absolute():
            # 用 config.PROJECTS_DIR 现取（而不是模块顶部一次性 import），
            # 测试里常见 monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path) 按用例
            # 切换沙盒目录，这里必须跟着最新值走，否则会误判「不在 PROJECTS_DIR 下」。
            rel_path = path.relative_to(config.PROJECTS_DIR).as_posix()
        else:
            rel_path = path.as_posix()
    except ValueError:
        return None
    if not rel_path or rel_path in (".", "/"):
        return None
    project_id = rel_path.split("/", 1)[0]
    ticket = _sign(project_id, _day_bucket())
    query = f"v={version}&mt={ticket}" if version is not None else f"mt={ticket}"
    return f"/media/{rel_path}?{query}"


def verify_media_ticket(rel_path: str, ticket: str | None) -> bool:
    """校验某个相对 PROJECTS_DIR 的媒体路径与 ``mt=`` 票据是否匹配。

    同时接受「今天」与「昨天」两个分桶的签名，容忍跨零点时仍在浏览器里的旧链接
    继续可用一天，避免用户刷新页面前一秒生成的 URL 在零点后突然失效。
    """
    if not ticket or not rel_path:
        return False
    project_id = rel_path.split("/", 1)[0]
    if not project_id:
        return False
    today = _day_bucket()
    return any(
        hmac.compare_digest(_sign(project_id, bucket), ticket)
        for bucket in (today, today - 1)
    )
