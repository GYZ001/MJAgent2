"""视频补齐"成功终态"的新鲜度判据。

终态是历史结论：它成立于写下它的那一刻、针对当时那一版分镜。分镜重做会整体
换掉镜头表（旧镜头连同它们的版本一起消失），而 checkpoint 一个字都不改。实测
``ep_0a70ec56e8e9`` 重做分镜后，``GET /video-completion`` 同一条响应里 coverage 是
``adopted 0 / total 4 / unadopted 4``，user_state 却仍是 ``completed``、
next_actions 只剩「查看成片」——界面宣布已补齐却没有任何入口能重新补齐
（CLAUDE.md「界面承诺必须与实际行为一致」「拦住用户时必须给出路」）；脚本化
调用同样被这个终态挡在门外，整段跳过视频阶段，零条供应商调用就直接报「没有
候选版本可采纳」。

因此判据不挂 ``phase`` 这个状态字段，而挂产物信号——当前分镜的每一镜是否都
还有采用版本（「挂产物信号，不挂状态字段」）。
"""
from __future__ import annotations

from typing import Any

TERMINAL_SUCCESS_PHASES = ("SUCCEEDED_COVERED", "COMPLETED_DEADLINE_FALLBACK")


def live_coverage_confirms_completion(projection: dict[str, Any]) -> bool | None:
    """当前这版分镜是不是每一镜都还有采用版本。

    ``None`` 表示没有产物信号可依（覆盖台账这次没建起来，或调用方给的投影里
    压根没有 coverage），调用方应维持 checkpoint 自己的结论——缺失不是证据，
    不要在这里拿它猜一个方向。
    """
    if projection.get("ledger_error"):
        return None
    coverage = projection.get("coverage")
    if not isinstance(coverage, dict) or "total" not in coverage:
        return None
    total = int(coverage.get("total") or 0)
    adopted = int(coverage.get("adopted") or 0)
    # total == 0 不是「无需检查」：一个镜头都没有的分集谈不上「全片已补齐」。
    return total > 0 and adopted >= total


def terminal_success_contract(
    episode_id: str,
    phase: str,
    projection: dict[str, Any],
    *,
    base: str,
    action,
) -> dict[str, Any]:
    """两个成功终态的用户契约：终态仍描述当前分镜就照常报完成，否则改口并给回
    重跑入口（模块 docstring 记的就是不改口时的实测后果）。"""
    if live_coverage_confirms_completion(projection) is not False:
        if phase == "SUCCEEDED_COVERED":
            return {
                "user_state": "completed",
                "message": "全片视频已补齐",
                "next_actions": [
                    action("view_results", "查看成片", "GET", f"/api/episodes/{episode_id}"),
                ],
            }
        return {
            "user_state": "completed",
            "message": "已按截止时间完成交差，部分镜头可能使用保底版本",
            "next_actions": [
                action("view_results", "查看结果", "GET", f"/api/episodes/{episode_id}"),
            ],
        }
    coverage = projection.get("coverage") or {}
    total = int(coverage.get("total") or 0)
    unadopted = int(coverage.get("unadopted") or 0)
    if total > 0:
        return {
            "user_state": "not_started",
            "message": (
                f"上一轮补齐针对的是旧版分镜；当前分镜有 {unadopted}/{total} "
                "个镜头没有采用版本，需要重新补齐"
            ),
            "next_actions": [
                action("start_completion", "重新补齐", "POST", base, True),
            ],
        }
    return {
        "user_state": "not_started",
        "message": "当前分集没有分镜镜头，上一轮补齐的结论已不适用，请先回分镜台生成分镜",
        "next_actions": [
            action(
                "open_storyboard", "去分镜台", "GET",
                f"/api/episodes/{episode_id}/storyboard/status",
            ),
        ],
    }
