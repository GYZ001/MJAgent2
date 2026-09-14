"""把字幕闸门的结论并进技术校验结果。L2：只读 ``qa_json`` 里的结论，不碰模型、不抽帧
（抽帧与 VLM 调用在 ``app.media_exec.subtitle_gate``，L5）。

有叠加字幕的候选与坏文件走同一条路：``passed=False`` + 一条 BLOCKER issue，
``app.media_exec.run_job`` 按 ``technical_resubmit_limit`` 自动重提。闸门没判定
（关闭、抽帧失败、模型不可用）时结果原样返回——放行是闸门自己的取舍，这里不替它兜底。
"""
from __future__ import annotations

from typing import Any

from app.harness.types import Issue, IssueSeverity

GATE_KEY = "subtitle_gate"
ISSUE_CODE = "subtitle_overlay"


def _describe(frames: list[dict[str, Any]]) -> str:
    parts = [
        f"『{item.get('text_seen') or '?'}』（第 {item.get('index')} 帧，{item.get('where') or '位置未报'}）"
        for item in frames[:3]
    ]
    return "、".join(parts) or "模型报告有叠加文字但未给出内容"


def technical_with_verdict(technical: dict[str, Any], qa: dict[str, Any] | None) -> dict[str, Any]:
    verdict = qa.get(GATE_KEY) if isinstance(qa, dict) else None
    if not isinstance(verdict, dict) or not verdict.get("checked") or not verdict.get("subtitle_overlay"):
        return technical
    issue = Issue(
        code=ISSUE_CODE,
        severity=IssueSeverity.BLOCKER,
        subject="video",
        message=f"画面叠加了字幕：{_describe(verdict.get('overlay_frames') or [])}；视频生成只负责画面与声音，字幕由后续功能另做",
        # repair_hint 会被 Supervisor 的定向重抽原样写进下一版提示词的「上一版必须改正」，
        # 所以写给视频模型看：完整的正面陈述 + 上一版具体错在哪。
        repair_hint=(
            "台词只以声音呈现，画面上不出现任何字幕、名条或标题条"
            f"（上一版画面叠加了{_describe(verdict.get('overlay_frames') or [])}）"
        ),
        repairable=True,
    )
    return {**technical, "passed": False, "issues": [*(technical.get("issues") or []), issue]}
