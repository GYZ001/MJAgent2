"""媒体流水线阶段与状态常量。"""
from __future__ import annotations

# jobs / media_tasks 共用阶段状态
BLOCKED = "blocked"
QUEUED = "queued"
RUNNING = "running"
WAITING_PROVIDER = "waiting_provider"
WAITING_RETRY = "waiting_retry"
WAITING_BUDGET = "waiting_budget"
WAITING_HUMAN = "waiting_human"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
ABANDONED = "abandoned"

# 阶段名
STAGE_REFERENCE = "reference_prepare"
STAGE_VIDEO_SUBMIT = "video_submit"
STAGE_VIDEO_POLL = "video_poll"
STAGE_DOWNLOAD = "video_download"
STAGE_QA = "video_qa"
STAGE_ADOPT = "video_adopt"

# 资源通道
RESOURCE_REFERENCE = "reference_pipeline"
RESOURCE_IMAGE = "image_request"
RESOURCE_VLM = "vlm_request"
RESOURCE_VIDEO_SUBMIT = "video_submit"
RESOURCE_VIDEO_INFLIGHT = "video_inflight"
RESOURCE_VIDEO_POLL = "video_poll"
RESOURCE_DOWNLOAD = "download"
RESOURCE_FINALIZE = "finalize"

ACTIVE_JOB_STATUSES = frozenset({
    QUEUED, RUNNING, WAITING_PROVIDER, WAITING_RETRY, WAITING_BUDGET,
})
PROVIDER_HELD_STATUSES = frozenset({WAITING_PROVIDER, RUNNING})

PIPELINE_STAGE_LABELS = {
    STAGE_REFERENCE: "准备参考图",
    STAGE_VIDEO_SUBMIT: "提交视频",
    STAGE_VIDEO_POLL: "Seedance 生成中",
    STAGE_DOWNLOAD: "下载中",
    STAGE_QA: "视频质检中",
    STAGE_ADOPT: "待采用",
}
