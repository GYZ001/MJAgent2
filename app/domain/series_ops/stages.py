"""连播台五个步骤（映射/分镜/确认/生成/成片）的完成判据、启动与等待。

判据只挂产物信号（CLAUDE.md「Gates and Criteria」），不复制 ``episodes.status``
白名单：
- screenplay：``episodes.screenplay_status == 'ready'``
- storyboard：``storyboard_pack_prompts_complete`` 为真且分镜状态快照
  ``confirmable`` 为真
- confirm：``episodes.status in ('confirmed','generating','done')``
- video：``rebuild_coverage_ledger(episode_id).covered_within_quota()``
- final：``final/episode.mp4`` 存在且无 ``.stale`` 标记

``stage_is_complete``/``run_stage`` 是 orchestrator.py 唯一调用的分发入口——
用 if/elif 而不是 dict 分发表，是为了让测试只需要
``monkeypatch.setattr(stages, "run_stage", stub)``/``"stage_is_complete"``
两个符号就能整体打桩，不必逐一打五个私有实现（本包子模块都用 ``from . import
stages`` 这种模块限定访问，不用 ``from .stages import name``，所以这一个
symbol 就是全部调用点的唯一绑定，不需要额外的 patch_series_ops_everywhere）。
"""
from __future__ import annotations

import asyncio

from fastapi import HTTPException

from app import task_registry
from app.db import get_conn

STAGE_SEQUENCE: tuple[str, ...] = ("screenplay", "storyboard", "confirm", "video", "final")

STAGE_LABELS: dict[str, str] = {
    "screenplay": "映射台",
    "storyboard": "分镜台",
    "confirm": "确认",
    "video": "生成台",
    "final": "成片台",
}

#: 单集任务占用一集时的登记种类 → 台名。编排器在跑每一步之前都查一遍：占用着就等，
#: 不抢、不失败——重启后平台自己会把上一轮的映射台/分镜台运行恢复起来，那正是这个
#: 连播任务的产出，等它跑完再按完成判据跳过即可。
EPISODE_BUSY_KINDS: dict[str, str] = {
    "screenplay": "映射台",
    "storyboard": "分镜台",
    "video_completion": "生成台",
}


def busy_label(episode_id: str) -> str | None:
    """这一集正被哪个台的单集任务占用；空闲返回 None。"""
    kind = busy_kind(episode_id)
    return EPISODE_BUSY_KINDS[kind] if kind else None


def busy_kind(episode_id: str) -> str | None:
    """占用本集的单集任务种类（screenplay/storyboard/video_completion）；空闲返回 None。"""
    for kind in EPISODE_BUSY_KINDS:
        if task_registry.active(kind, episode_id):
            return kind
    return None


BUSY_KIND_TO_STAGE = {"screenplay": "screenplay", "storyboard": "storyboard", "video_completion": "video"}


def _http_error_code(exc: HTTPException) -> str | None:
    detail = exc.detail
    return detail.get("code") if isinstance(detail, dict) else None


def _http_error_message(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("code") or detail)
    return str(detail)


# ---------------------------------------------------------------- screenplay

def screenplay_complete(conn, episode_id: str) -> bool:
    """与分镜台开跑用的是同一份判据（``_screenplay_ready``：ready 且带正式投影），不再只看状态列——
    2026-09-05 实测重置后状态列残留 ready 而产物已删，连播台跳过映射台、分镜台报「请先生成可拍剧本」。"""
    from app.domain.common import _screenplay_ready  # 延迟导入：app.domain.common 会拉起整条 domain 依赖链

    row = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return bool(row) and _screenplay_ready(row)


async def _run_screenplay(episode_id: str) -> None:
    from app.capabilities.direct import enter_handler
    from app.domain.screenplay_ops import start_screenplay

    with enter_handler():
        await start_screenplay(episode_id, body={})
    while task_registry.active("screenplay", episode_id):
        await asyncio.sleep(5)
    _raise_stage_error(episode_id, "screenplay_error")


# ----------------------------------------------------------------- storyboard

def storyboard_complete(conn, episode_id: str) -> bool:
    from app.domain.common import storyboard_pack_prompts_complete
    from app.domain.storyboard_ops import storyboard_status

    if not storyboard_pack_prompts_complete(conn, episode_id):
        return False
    return bool(storyboard_status(episode_id).get("confirmable"))


async def _run_storyboard(episode_id: str) -> None:
    from app.capabilities.direct import enter_handler
    from app.domain.storyboard_ops import start_storyboard, storyboard_start_preflight

    preflight = await asyncio.to_thread(storyboard_start_preflight, episode_id, None)
    token = preflight.get("preview_token")
    with enter_handler():
        await start_storyboard(episode_id, body={"preflight_token": token})
    while task_registry.active("storyboard", episode_id):
        await asyncio.sleep(5)
    _raise_stage_error(episode_id, "script_error")


def _raise_stage_error(episode_id: str, error_column: str) -> None:
    """后台任务退出后把实际原因带回连播台；只读取本步骤的错误列。"""
    row = get_conn().execute(
        "SELECT screenplay_error, script_error FROM episodes WHERE id=?", (episode_id,),
    ).fetchone()
    if row and row[error_column]:
        raise RuntimeError(str(row[error_column]))


# --------------------------------------------------------------------- confirm

def confirm_complete(conn, episode_id: str) -> bool:
    row = conn.execute("SELECT status FROM episodes WHERE id=?", (episode_id,)).fetchone()
    return bool(row) and row["status"] in {"confirmed", "generating", "done"}


async def _run_confirm(episode_id: str) -> None:
    from app.domain.video_ops import confirm_episode_core, create_storyboard_confirmation_preview

    def _do() -> None:
        # get_conn 是线程局部的：预览签发与确认落地必须在同一个工作线程里
        # 依次做，才能保证 confirm_episode_core 内部 BEGIN IMMEDIATE 的事务
        # 不会与事件循环线程上其它任务共用同一个连接对象（照
        # capabilities/handlers/storyboard.py::confirm 的同款注释）。
        preview = create_storyboard_confirmation_preview(episode_id)
        confirm_episode_core(
            episode_id,
            preview_token=preview["preview_token"],
            decided_by="series_film",
        )

    await asyncio.to_thread(_do)


# ----------------------------------------------------------------------- video

def video_complete(conn, episode_id: str) -> bool:
    from app.video_supervisor import rebuild_coverage_ledger

    _ = conn
    return rebuild_coverage_ledger(episode_id).covered_within_quota()


def _active_video_run(conn, episode_id: str) -> dict | None:
    ep = conn.execute(
        "SELECT active_video_run_id FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    run_id = ep["active_video_run_id"] if ep else None
    if not run_id or str(run_id).startswith("starting:"):
        return None
    run = conn.execute(
        "SELECT id, status, failure_message FROM workflow_runs WHERE id=?", (run_id,)
    ).fetchone()
    return dict(run) if run else None


def _waiting_human_shots(conn, episode_id: str) -> list[str]:
    """停在「等人工」的镜头，带上供应商/闸门给的原话。"""
    rows = conn.execute(
        """SELECT s.shot_no, j.error FROM jobs j JOIN shots s ON s.id=j.shot_id
            WHERE j.episode_id=? AND j.kind='video' AND j.status='waiting_human'
            ORDER BY s.shot_no LIMIT 3""",
        (episode_id,),
    ).fetchall()
    if not rows:
        return []
    return ["待人工处理：" + "；".join(
        f"第{row['shot_no']}镜 {str(row['error'] or '').strip()[:200]}" for row in rows
    )]


def _preflight_blocked_shots(conn, episode_id: str) -> list[str]:
    """被视频输入预检拦下的镜头。

    2026-09-12 实测（我欲封天第 4 集镜 24/25）：这两句在原文里是带引号的人物心声，
    分镜台把发声主体写成了旁白，预检的 ``STORYBOARD_IDENTITY_REPAIR_REQUIRED``
    拦住不放。连播台当时只报「生成台未能补齐全部镜头」——可操作的那句话躺在
    ``jobs.reason_text`` 里没人看得到，而任务级文案又说「修好失败的集后重新加入
    队列」，照着做只会一直重试（实测两次，连供应商请求都没发出去）。

    与 ``_waiting_human_shots`` 分开查而不是并进一条 SQL：这类 job 的终态是
    ``cancelled``、可操作文本在 ``reason_text`` 而不是 ``error``，两边的取数口径
    本来就不一样；更要紧的是这一类必须额外说清「重试无效」，那是另一句话。
    """
    rows = conn.execute(
        """SELECT s.shot_no, j.reason_text FROM jobs j JOIN shots s ON s.id=j.shot_id
            WHERE j.episode_id=? AND j.kind='video' AND j.status='cancelled'
              AND j.reason_code='VIDEO_PREFLIGHT_BLOCKED'
            ORDER BY s.shot_no LIMIT 3""",
        (episode_id,),
    ).fetchall()
    if not rows:
        return []
    detail = "；".join(
        f"第{row['shot_no']}镜 {str(row['reason_text'] or '').strip()[:200]}" for row in rows
    )
    return [f"需在分镜台修订后才能生成（重新加入队列不会改变结果）：{detail}"]


def _stalled_video_reason(episode_id: str) -> str:
    """补齐失败时附在错误后面的可操作说明。

    运行级结论只说「需人工」，不说是哪一镜、卡在什么上；把镜号与原话带上，用户在
    连播台就能看到出路（CLAUDE.md「拦住用户时必须给出路」）。镜头级事实不依赖
    「有没有活跃的视频 run」——预检拦截是 jobs 上的持久事实，run 早已收口时同样要说；
    2026-09-12 就是因为挂在 run 上，两条分支一起落空，只剩一句没有信息量的失败。
    """
    conn = get_conn()
    parts: list[str] = []
    run = _active_video_run(conn, episode_id)
    if run:
        parts.append(str(run["failure_message"] or run["status"]))
    parts.extend(_preflight_blocked_shots(conn, episode_id))
    parts.extend(_waiting_human_shots(conn, episode_id))
    return "：" + "；".join(parts) if parts else ""


# 补齐 Supervisor 停在这些 checkpoint 阶段时都不能静默发起新的 fresh 尝试：
# WAITING_AUTHORIZATION/WAITING_HUMAN/WAITING_RETRY 需要人工在生成台处理，
# PAUSED_EXTERNAL 这里特指「不是服务重启」的暂停（用户手动点了暂停，见
# app/video_supervisor/run_loop.py 的 action=="pause" 分支）——它和 workflow_runs
# 记录的字面 status='PAUSED_EXTERNAL'（唯一来源是 recorder.pause_external()，只在
# task_registry.shutdown_in_progress() 时触发）是两回事，见下面 _kick_video_completion
# 的分支顺序：先判 workflow_runs.status，命中才自动唤醒；命中不了才落到这里按
# checkpoint phase 停下来讲人话。
_VIDEO_WAIT_PHASES = {
    "WAITING_AUTHORIZATION", "WAITING_HUMAN", "WAITING_RETRY", "PAUSED_EXTERNAL",
}

# outcome 大多是 app.completion_grant.GrantValidationError.code（如
# GRANT_EXPIRED/GRANT_REVOKED），本身已是可读的英文短语，直接透出即可；只有
# STORYBOARD_REPAIR_PROPOSAL_NOT_AUTHORIZED（见 run_loop.py 第 404 行）没有配套
# 人话，专门补这一条。不新造一整套阶段文案——阶段名复用
# app.video_supervisor.constants.phase_label，不重复 _PHASE_LABELS。
#: 等待补充授权里能由连播台自己续上的原因：旧授权绑定的分镜/资格已过时或已用尽。


def _can_reauthorize(cp) -> bool:
    """连播台重新入队这个动作本身就是「人已处理」：生成台停在等人工/等授权的检查点时，连播台按当前
    分镜重新发起一次 fresh 补齐，而不是把人晾在「请到生成台处理后再回来点继续」——2026-09-05 第三轮 k
    第 11 集：版权拒绝转人工后补跑任务永远起不来；重新发起后拒绝会按「3 个独立任务相同失败」跳过。
    WAITING_RETRY / PAUSED_EXTERNAL 仍按服务重启续跑处理，不在这里。"""
    return cp.phase in {"WAITING_AUTHORIZATION", "WAITING_HUMAN"}


_VIDEO_WAIT_OUTCOME_DETAILS: dict[str, str] = {
    "STORYBOARD_REPAIR_PROPOSAL_NOT_AUTHORIZED": (
        "AI 提议修改分镜以补齐镜头，需要你在生成台批准或自行修分镜"
    ),
}


def _video_wait_message(detail: str, phase: str | None) -> str:
    from app.video_supervisor import phase_label

    label = phase_label(phase) if phase else "等待处理"
    return (
        f"生成台正在等待处理（{label}）：{detail}。"
        "请到生成台处理后，再回连播台点「继续」"
    )


def _checkpoint_wait_message(cp) -> str:
    outcome = cp.outcome or ""
    detail = _VIDEO_WAIT_OUTCOME_DETAILS.get(outcome) or outcome or "需要人工处理"
    return _video_wait_message(detail, cp.phase)


async def _resume_paused_video(episode_id: str, run_id: str, cp) -> str | None:
    """PAUSED_EXTERNAL 且是服务重启导致时尝试唤醒原运行；返回 None 表示已唤醒，
    返回值非 None 时是给用户看的等待文案（唤醒失败或缺少可续跑的授权）。"""
    from app.domain.video_ops import _complete_episode_core

    if not cp.grant_id:
        return _video_wait_message(
            "生成台因服务重启暂停，但缺少可续跑的补齐授权，需要人工在生成台重新发起",
            cp.phase,
        )
    try:
        await _complete_episode_core(episode_id, {
            "mode": "resume",
            "completion_grant_id": cp.grant_id,
            "idempotency_key": f"{run_id}:video-resume:{episode_id}",
        })
        return None
    except HTTPException as exc:
        return _video_wait_message(
            f"服务重启后自动恢复未成功：{_http_error_message(exc)}", cp.phase,
        )


def _cancel_orphan_video_run(run_id: str) -> None:
    from app.orchestration.engine import WorkflowRecorder
    WorkflowRecorder(run_id).cancel(
        "服务重启时该运行尚未写下检查点，无法续跑；连播台已按当前分镜重新发起全片补齐",
        conn=None,
    )


async def _kick_video_completion(episode_id: str, run_id: str) -> None:
    from app.domain.video_ops import _complete_episode_core
    from app.video_supervisor import load_latest_checkpoint

    run = _active_video_run(get_conn(), episode_id)
    cp = load_latest_checkpoint(episode_id) if run else None
    matched_cp = cp if (cp and run and cp.run_id == run["id"]) else None
    if run and run["status"] == "PAUSED_EXTERNAL" and matched_cp is None:
        # 服务重启把运行标成了 PAUSED_EXTERNAL，但它还没来得及写下任何检查点
        # （2026-09-05 我欲封天第 13/14 集）：开机恢复找不到检查点不会接管，续跑
        # 也无从续起，fresh 又被它挡成 ALREADY_ACTIVE——连播台把这种孤儿运行收尾
        # 掉，按当前分镜重新发起，而不是判本集失败让人去生成台点。
        _cancel_orphan_video_run(run["id"])
        run = None

    if matched_cp and run["status"] == "PAUSED_EXTERNAL":
        wait_message = await _resume_paused_video(episode_id, run_id, matched_cp)
        if wait_message is None:
            return
        raise RuntimeError(wait_message)

    if matched_cp and matched_cp.phase in _VIDEO_WAIT_PHASES and not _can_reauthorize(matched_cp):
        raise RuntimeError(_checkpoint_wait_message(matched_cp))

    # 走到这里要么没有在等的运行，要么是「旧授权因分镜重做/过期/用尽而失效」——那是流程
    # 自己造成的状态变化，连播台自己重新发起一次 fresh 补齐（按当前发布版分镜签新授权），
    # 不把人晾到生成台点确认（2026-09-05 产品复盘：我欲封天第 10 集卡在 UPSTREAM_VERSION_CHANGED）。
    try:
        await _complete_episode_core(episode_id, {
            "mode": "fresh",
            "allow_fallback_adopt": True,
            "allow_storyboard_edit": False,
            "idempotency_key": f"{run_id}:video:{episode_id}",
        })
    except HTTPException as exc:
        if _http_error_code(exc) != "VIDEO_COMPLETION_ALREADY_ACTIVE":
            raise


# 补齐运行因「流程自己造成的授权失效」收口：分镜重做/重规划让旧授权失效、或重启后旧计划在
# 新开关下不再合法（2026-09-05 第 18 集：上一段取帧关闭后旧计划失效→重规划→新授权撞上旧计划
# 变更，两秒内连收 RELEASE_QUALIFICATION_INVALID 与 UPSTREAM_VERSION_CHANGED）。这类不是本集
# 内容的问题，连播台按当前发布版分镜再发起一次即可，最多再试两次；其它失败原样报出。
_AUTHORIZATION_LOST_CODES = frozenset({
    "UPSTREAM_VERSION_CHANGED", "RELEASE_QUALIFICATION_CHANGED", "RELEASE_QUALIFICATION_INVALID",
})
_AUTHORIZATION_LOST_RETRIES = 2


def _last_video_run_failure(conn, episode_id: str) -> str:
    row = conn.execute(
        """SELECT failure_message FROM workflow_runs
            WHERE scope_id=? AND workflow_type='episode_video_completion'
            ORDER BY started_at DESC, updated_at DESC LIMIT 1""",
        (episode_id,),
    ).fetchone()
    return str((row["failure_message"] if row else "") or "").split("：", 1)[0].split(":", 1)[0].strip()


async def _run_video(episode_id: str, run_id: str) -> None:
    for attempt in range(_AUTHORIZATION_LOST_RETRIES + 1):
        if not task_registry.active("video_completion", episode_id):
            await _kick_video_completion(episode_id, run_id)
        while task_registry.active("video_completion", episode_id):
            await asyncio.sleep(8)
        if video_complete(get_conn(), episode_id):
            return
        if _last_video_run_failure(get_conn(), episode_id) not in _AUTHORIZATION_LOST_CODES:
            break
        if attempt < _AUTHORIZATION_LOST_RETRIES:
            await asyncio.sleep(5)  # 让上一次运行的收口写完，再按当前分镜重新发起
    raise RuntimeError(f"生成台未能补齐全部镜头{_stalled_video_reason(episode_id)}")


# ----------------------------------------------------------------------- final

def final_complete(conn, episode_id: str) -> bool:
    from app.media_exec.concat import _final_video_path

    _ = conn
    row = get_conn().execute(
        "SELECT project_id, episode_no FROM episodes WHERE id=?", (episode_id,)
    ).fetchone()
    if not row:
        return False
    path = _final_video_path(row["project_id"], row["episode_no"])
    return path.is_file() and not path.with_suffix(".stale").is_file()


_FINAL_LOCKS: dict[str, asyncio.Lock] = {}


async def _run_final(episode_id: str) -> None:
    """同一集的合片在进程内串行：补跑任务与主任务同时覆盖一集时，后到者等前者
    发布完再看一眼判据，已成片就不再重复渲染、也不再与前者争发布租约。"""
    from app import worker

    lock = _FINAL_LOCKS.setdefault(episode_id, asyncio.Lock())
    async with lock:
        if final_complete(get_conn(), episode_id):
            return
        await asyncio.to_thread(worker.concatenate_episode, episode_id)


# -------------------------------------------------------------------- dispatch

def stage_is_complete(stage: str, conn, episode_id: str) -> bool:
    if stage == "screenplay":
        return screenplay_complete(conn, episode_id)
    if stage == "storyboard":
        return storyboard_complete(conn, episode_id)
    if stage == "confirm":
        return confirm_complete(conn, episode_id)
    if stage == "video":
        return video_complete(conn, episode_id)
    if stage == "final":
        return final_complete(conn, episode_id)
    raise ValueError(f"未知步骤：{stage}")


async def run_stage(stage: str, episode_id: str, run_id: str) -> None:
    if stage == "screenplay":
        await _run_screenplay(episode_id)
    elif stage == "storyboard":
        await _run_storyboard(episode_id)
    elif stage == "confirm":
        await _run_confirm(episode_id)
    elif stage == "video":
        await _run_video(episode_id, run_id)
    elif stage == "final":
        await _run_final(episode_id)
    else:
        raise ValueError(f"未知步骤：{stage}")
