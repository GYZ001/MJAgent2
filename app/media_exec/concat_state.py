"""整集合成的进程内状态：进行中 / 最近一次失败原因，供 mix-status 投影与后台任务回写。

2026-09-15 实测：合成 122 秒期间 B 的健康页与查库接口全部超时——命令总线处理器把同步的
``concatenate_episode`` 直接跑在事件循环线程上，整个后端冻结；手机浏览器的这次请求又因
超过 60 秒被判成「无法连接本机后端服务」。现在合成在线程里跑、浏览器请求立即返回，
成片台按这里的状态轮询。单进程部署，进程内字典足够；重启即清空，与 task_registry 同寿命。
"""
from __future__ import annotations

from app import task_registry

TASK_KIND = "concat"
_last_error: dict[str, str] = {}


def in_progress(episode_id: str) -> bool:
    return task_registry.active(TASK_KIND, episode_id)


def record_error(episode_id: str, message: str) -> None:
    _last_error[episode_id] = str(message or "合成失败")[:600]


def clear_error(episode_id: str) -> None:
    _last_error.pop(episode_id, None)


def last_error(episode_id: str) -> str | None:
    return _last_error.get(episode_id)


__all__ = ["TASK_KIND", "clear_error", "in_progress", "last_error", "record_error"]
