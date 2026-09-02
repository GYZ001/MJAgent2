"""video_supervisor 阶段字面量、Business Stage 展示元数据与 tick/预算常量。"""
from __future__ import annotations

import asyncio

from typing import Literal



SupervisorPhase = Literal[
    "CREATED",
    "PREFLIGHT",
    "PREPARING_ASSETS",
    "PLANNING_COVERAGE",
    "DISPATCHING",
    "OBSERVING",
    "EVALUATING",
    "REPAIRING",
    "FINALIZING",
    "DEADLINE_CLOSING",
    "SUCCEEDED_COVERED",
    "COMPLETED_DEADLINE_FALLBACK",
    "PARTIAL_NO_USABLE_CANDIDATE",
    "RECOVERING_CONTROL_PLANE",
    "FAILED_CLOSED",
    "WAITING_RETRY",
    "PAUSED_EXTERNAL",
    "WAITING_AUTHORIZATION",
    "WAITING_HUMAN",
    "CANCELLED",
]

SUPERVISOR_TICK_INTERVAL_S = 10.0
SUPERVISOR_TICK_MAX_INTERVAL_S = 60.0
MAX_REPAIR_EPOCHS = 8
MIN_ATTEMPTS_PER_SHOT = 2
MAX_ATTEMPTS_PER_SHOT = 6
CHECKPOINT_ARTIFACT_TYPE = "video_supervisor_checkpoint"
REPORT_ARTIFACT_TYPE = "video_coverage_report"
CONTROL_PLANE_MAX_RECOVERIES = 3
SUPERVISOR_HEARTBEAT_STALE_S = 60.0
ASSET_PREP_HEARTBEAT_INTERVAL_S = 20.0
DISPATCH_HEARTBEAT_INTERVAL_S = 20.0
LIFECYCLE_HEARTBEAT_INTERVAL_S = 20.0
TERMINAL_SUPERVISOR_PHASES = {
    "SUCCEEDED_COVERED",
    "COMPLETED_DEADLINE_FALLBACK",
    "PARTIAL_NO_USABLE_CANDIDATE",
    "FAILED_CLOSED",
    "CANCELLED",
}

VIDEO_COMPLETION_BUSINESS_STAGES = (
    {
        "key": "authorization",
        "name": "确认补齐范围与生成授权",
        "description": "确认待补镜头、预算、截止时间和允许的自动修复范围",
    },
    {
        "key": "plan",
        "name": "制定全片视频生成方案",
        "description": "为每个镜头决定参考图、首帧或首尾帧等生成方式与衔接依赖",
    },
    {
        "key": "assets",
        "name": "核对人物、场景与连续性素材",
        "description": "检查人物定妆、场景参考和前后镜头衔接素材是否可用",
    },
    {
        "key": "coverage",
        "name": "盘点全片缺口与生成顺序",
        "description": "统计已采用和待补镜头，并按依赖关系安排生成顺序",
    },
    {
        "key": "shots",
        "name": "逐镜生成视频",
        "description": "逐镜准备输入、调用视频模型、下载并保存候选结果",
    },
    {
        "key": "quality",
        "name": "检查质量并自动修复",
        "description": "执行技术检查和内容质检，对未通过镜头定向重试或转人工",
    },
    {
        "key": "finalize",
        "name": "验收全片覆盖并收口",
        "description": "为每个镜头选择可用版本，确认全片无缺镜后结束任务",
    },
)

_PHASE_LABELS: dict[str, str] = {
    "CREATED": "任务已创建",
    "PREFLIGHT": "核对授权与基础条件",
    "PREPARING_ASSETS": "准备人物与场景参考素材",
    "PLANNING_COVERAGE": "盘点待补镜头",
    "DISPATCHING": "派发逐镜生成任务",
    "OBSERVING": "等待镜头生成结果",
    "EVALUATING": "检查镜头质量",
    "REPAIRING": "自动修复未通过镜头",
    "FINALIZING": "验收全片覆盖",
    "DEADLINE_CLOSING": "按截止时间收口",
    "SUCCEEDED_COVERED": "全片视频已补齐",
    "COMPLETED_DEADLINE_FALLBACK": "已按截止时间完成收口",
    "PARTIAL_NO_USABLE_CANDIDATE": "仍有镜头缺少可用视频",
    "RECOVERING_CONTROL_PLANE": "恢复全片调度状态",
    "FAILED_CLOSED": "任务已安全停止",
    "WAITING_RETRY": "等待自动重试",
    "PAUSED_EXTERNAL": "外部服务暂停",
    "WAITING_AUTHORIZATION": "等待补充授权",
    "WAITING_HUMAN": "等待人工处理",
    "CANCELLED": "任务已取消",
}


def phase_label(phase: str | None) -> str:
    value = str(phase or "")
    return _PHASE_LABELS.get(value, f"未知业务阶段（{value or '未记录'}）")

_REFERENCE_ASSET_PREP_LOCKS: dict[str, asyncio.Lock] = {}
_CHECKPOINT_WRITE_SEMAPHORE = asyncio.Semaphore(1)
