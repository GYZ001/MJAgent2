"""把画面人数与身份闸门的结论并进技术校验结果。L2：只读 ``qa_json`` 里的结论，
不碰模型、不抽帧（抽帧与 VLM 调用在 ``app.media_exec.character_count_gate``，L5）。

人数超额或角色重复的候选与坏文件走同一条路：``passed=False`` + 一条 BLOCKER
issue，``app.media_exec.run_job`` 按 ``technical_resubmit_limit`` 自动重提。闸门
没判定（关闭、抽帧失败、模型不可用、本段未登记可见角色）时结果原样返回——放行
是闸门自己的取舍，这里不替它兜底。
"""
from __future__ import annotations

from typing import Any

from app.harness.types import Issue, IssueSeverity

GATE_KEY = "character_count_gate"
ISSUE_CODE_HEADCOUNT = "headcount_exceeded"
ISSUE_CODE_DUPLICATED = "character_duplicated"


def _headcount_issue(verdict: dict[str, Any]) -> Issue:
    evidence = verdict.get("headcount_evidence") or []
    max_seen = max((int(item.get("headcount") or 0) for item in evidence), default=0)
    allowed = verdict.get("allowed_headcount")
    names = "、".join(verdict.get("roster_names") or [])
    message = f"画面里连续出现 {max_seen} 个人，分镜只允许 {allowed} 人（{names}）"
    return Issue(
        code=ISSUE_CODE_HEADCOUNT,
        severity=IssueSeverity.BLOCKER,
        subject="video",
        message=message,
        # repair_hint 会被 Supervisor 的定向重抽原样写进下一版提示词的「上一版必须改正」，
        # 所以写给视频模型看：完整的正面陈述 + 上一版具体错在哪，同 subtitle_overlay。
        repair_hint=f"{message}；本次每个画面里出镜的人数不得超过分镜登记的 {allowed} 人（{names}）",
        repairable=True,
    )


def _duplicated_issue(verdict: dict[str, Any]) -> Issue:
    parts: list[str] = []
    for item in verdict.get("duplicated_characters") or []:
        name = item.get("name")
        frames = item.get("frames") or []
        seconds = frames[0].get("seconds") if frames else None
        parts.append(
            f"上一版第 {seconds:.1f} 秒起画面里{name}出现了两次" if seconds is not None
            else f"画面里{name}出现了两次"
        )
    message = "；".join(parts) or "模型报告有角色在同一画面里重复出现"
    return Issue(
        code=ISSUE_CODE_DUPLICATED,
        severity=IssueSeverity.BLOCKER,
        subject="video",
        message=message,
        repair_hint=f"{message}；本次每个画面里每位角色只能出现一次",
        repairable=True,
    )


def technical_with_verdict(technical: dict[str, Any], qa: dict[str, Any] | None) -> dict[str, Any]:
    verdict = qa.get(GATE_KEY) if isinstance(qa, dict) else None
    if not isinstance(verdict, dict) or not verdict.get("checked"):
        return technical
    issues = [
        *([_headcount_issue(verdict)] if verdict.get("headcount_exceeded") else []),
        *([_duplicated_issue(verdict)] if verdict.get("character_duplicated") else []),
    ]
    if not issues:
        return technical
    return {**technical, "passed": False, "issues": [*(technical.get("issues") or []), *issues]}
