"""连播任务台「成片缺段」判据：本集 ``final/episode.mp4`` 是否缺段、这份缺段
信息是否已经过期——直接读合成时已持久化到 ``episode.edit-report.json`` 的
``timeline``，并复用 ``app.media_exec.concat`` 自己的 staleness 判据
（``_final_video_is_stale``），不重新计算、不新造第二套。字段名与
``episode_mix_status`` 供 CinemaPage 使用的 ``final_is_partial``/
``final_video_stale``/``skipped_shot_nos``/``skip_reasons`` 完全一致。

为什么带 ``final_video_stale``：采纳新镜头（``app/artifacts.py``/
``app/media_exec/run_job.py`` 调用 ``_invalidate_final_video()``）只给
``final/episode.mp4`` 打 ``.stale`` 标记，不会改 ``episode.edit-report.json``
本身——不带这个字段，连播任务台会一直展示合成时那一刻的旧缺段清单，且不告诉
用户这份信息可能已经不准（CLAUDE.md「空集合不等于无需检查」「不静默吞掉」）。

独立成一个子模块而不是塞进 ``stages.py``：``stages.py`` 已在文件行数棘轮的
上限（500 行），装不下时先拆，不加基线。与包内其余子模块同规格：调用方一律
``from . import final_status`` + ``final_status.name(...)`` 属性访问，不
``from .final_status import name``（同一份 monkeypatch 打桩才能覆盖全部调用点，
见 ``tests/test_series_ops_monkeypatch_guard.py`` 头部说明）。
"""
from __future__ import annotations


def final_partial_status(conn, project_id: str, episode_no: int) -> dict:
    """本集当前成片的缺段状态；``final_video_stale`` 为真时，其余三个字段读的
    是「打 .stale 标记那一刻之前」的旧报告，调用方应当优先展示「信息可能已
    过期」而不是继续展示旧缺段清单（与 CinemaPage 对 ``final_video_stale`` 优先
    于 ``final_is_partial`` 的展示顺序一致）。"""
    from app.media_exec.concat import (  # 与 stages.final_complete 同理：只有查这条产物信号时才需要合成模块，其余判据不依赖它
        _final_video_is_stale, _final_video_path, _read_edit_report,
    )

    final_path = _final_video_path(project_id, episode_no)
    report = _read_edit_report(final_path) if final_path.is_file() else None
    stale = _final_video_is_stale(conn, {"project_id": project_id, "episode_no": episode_no}, report)
    timeline = report.get("timeline") if isinstance(report, dict) else None
    if not isinstance(timeline, dict):
        return {"final_is_partial": False, "skipped_shot_nos": [], "skip_reasons": {}, "final_video_stale": stale}
    skipped = timeline.get("skipped_shot_nos")
    reasons = timeline.get("skip_reasons")
    return {
        "final_is_partial": bool(timeline.get("partial")),
        "skipped_shot_nos": sorted(n for n in skipped if isinstance(n, int)) if isinstance(skipped, list) else [],
        "skip_reasons": {k: v for k, v in reasons.items() if isinstance(k, str)} if isinstance(reasons, dict) else {},
        "final_video_stale": stale,
    }


def partial_episodes_in_range(
    conn, project_id: str, episode_from: int, episode_to: int, missing_episode_nos=(),
) -> list[dict]:
    """区间内「成片已合成但缺段」的集：集号 + 缺的段号 + 原因 + 是否过期——逐集调
    ``final_partial_status``，不重新算一遍合成、不新造第二套判据。全齐的集不
    出现在这份列表里；空列表是逐集查过的结果，不是跳过未查（CLAUDE.md「空
    集合不等于无需检查」）。``missing_episode_nos``（区间内尚不存在的集号）会
    被跳过——``_final_video_path`` 会顺带 mkdir，对压根没有的集号探测会在磁盘
    上留下空目录这种无意义的副作用。供连播任务列表/详情的序列化处直接复用。"""
    missing = set(missing_episode_nos)
    out = []
    for episode_no in range(episode_from, episode_to + 1):
        if episode_no in missing:
            continue
        found = final_partial_status(conn, project_id, episode_no)
        if found["final_is_partial"]:
            out.append({"episode_no": episode_no, **found})
    return out
