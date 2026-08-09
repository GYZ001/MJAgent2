"""媒体流水线阶段与状态常量（QPSP 权威枚举）。"""
from __future__ import annotations

# jobs / media_tasks 共用宏观生命周期
BLOCKED = "blocked"
QUEUED = "queued"
RUNNING = "running"
WAITING = "waiting"
WAITING_PROVIDER = "waiting_provider"
WAITING_RETRY = "waiting_retry"
WAITING_BUDGET = "waiting_budget"
WAITING_HUMAN = "waiting_human"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
ABANDONED = "abandoned"
PAUSED = "paused"

# 兼容旧粗粒度阶段名（读旧数据 / 迁移）
STAGE_REFERENCE = "reference_prepare"
STAGE_VIDEO_SUBMIT = "video_submit"
STAGE_VIDEO_POLL = "video_poll"
STAGE_DOWNLOAD = "video_download"
STAGE_QA = "video_qa"
STAGE_ADOPT = "video_adopt"

# PRD §6.1 细粒度 pipeline_stage
STAGE_PREFLIGHT_VALIDATING = "preflight_validating"
STAGE_PREFLIGHT_RETRY = "preflight_retry"
STAGE_PREFLIGHT_BLOCKED = "preflight_blocked"
STAGE_JOB_QUEUED = "job_queued"
STAGE_VIDEO_PROMPT = "video_prompt_generate"
STAGE_REFERENCE_PROMPT = "reference_prompt"
STAGE_REFERENCE_GENERATE = "reference_generate"
STAGE_REFERENCE_QA = "reference_qa"
STAGE_REFERENCE_CONSISTENCY = "reference_consistency"
STAGE_WAITING_CONTINUITY = "waiting_continuity_anchor"
STAGE_WAITING_DEPENDENCY = "waiting_dependency"
STAGE_CONTINUITY_ASSEMBLING = "continuity_assembling"
STAGE_VIDEO_READY = "video_ready"
STAGE_WAITING_VIDEO_SLOT = "waiting_video_slot"
STAGE_VIDEO_SUBMITTING = "video_submitting"
STAGE_VIDEO_GENERATING = "video_generating"
STAGE_VIDEO_DOWNLOADING = "video_downloading"
STAGE_VIDEO_TECHNICAL = "video_technical_check"
STAGE_VIDEO_QA = "video_qa"
STAGE_AUTO_RETAKE = "auto_retake_queued"
STAGE_WAITING_HUMAN = "waiting_human"
STAGE_CANDIDATE_READY = "candidate_ready"
STAGE_ADOPTED = "adopted"
STAGE_FAILED = "failed"
STAGE_CANCELLED = "cancelled"
STAGE_PAUSED_BUDGET = "paused_budget"

PIPELINE_STAGES = frozenset({
    STAGE_PREFLIGHT_VALIDATING,
    STAGE_PREFLIGHT_RETRY,
    STAGE_PREFLIGHT_BLOCKED,
    STAGE_JOB_QUEUED,
    STAGE_VIDEO_PROMPT,
    STAGE_REFERENCE_PROMPT,
    STAGE_REFERENCE_GENERATE,
    STAGE_REFERENCE_QA,
    STAGE_REFERENCE_CONSISTENCY,
    STAGE_WAITING_CONTINUITY,
    STAGE_WAITING_DEPENDENCY,
    STAGE_CONTINUITY_ASSEMBLING,
    STAGE_VIDEO_READY,
    STAGE_WAITING_VIDEO_SLOT,
    STAGE_VIDEO_SUBMITTING,
    STAGE_VIDEO_GENERATING,
    STAGE_VIDEO_DOWNLOADING,
    STAGE_VIDEO_TECHNICAL,
    STAGE_VIDEO_QA,
    STAGE_AUTO_RETAKE,
    STAGE_WAITING_HUMAN,
    STAGE_CANDIDATE_READY,
    STAGE_ADOPTED,
    STAGE_FAILED,
    STAGE_CANCELLED,
    STAGE_PAUSED_BUDGET,
    # 旧枚举保留以便兼容读取
    STAGE_REFERENCE,
    STAGE_VIDEO_SUBMIT,
    STAGE_VIDEO_POLL,
    STAGE_DOWNLOAD,
    STAGE_ADOPT,
})

# 资源通道
RESOURCE_REFERENCE = "reference_pipeline"
RESOURCE_IMAGE = "image_request"
RESOURCE_VLM = "vlm_request"
RESOURCE_VIDEO_SUBMIT = "video_submit"
RESOURCE_VIDEO_INFLIGHT = "video_inflight"
RESOURCE_VIDEO_POLL = "video_poll"
RESOURCE_DOWNLOAD = "download"
RESOURCE_FINALIZE = "finalize"
RESOURCE_VIDEO_CONTROL = "video_control"

# 调度车道
LANE_FINALIZE = "finalize"
LANE_VIDEO_READY = "video_ready_first_pass"
LANE_REFERENCE_CRITICAL = "reference_critical"
LANE_REFERENCE_NORMAL = "reference_normal"
LANE_RETAKE = "retake"

ACTIVE_JOB_STATUSES = frozenset({
    QUEUED, RUNNING, WAITING_PROVIDER, WAITING_RETRY, WAITING_BUDGET, WAITING,
})
PROVIDER_HELD_STATUSES = frozenset({WAITING_PROVIDER, RUNNING})

PIPELINE_STAGE_LABELS = {
    STAGE_PREFLIGHT_VALIDATING: "正在校验视频输入",
    STAGE_PREFLIGHT_RETRY: "入队校验等待自动重试",
    STAGE_PREFLIGHT_BLOCKED: "入队校验需要处理",
    STAGE_JOB_QUEUED: "已入队",
    STAGE_VIDEO_PROMPT: "AI 编写视频提示词",
    STAGE_REFERENCE_PROMPT: "编写参考图提示词",
    STAGE_REFERENCE_GENERATE: "生成参考图",
    STAGE_REFERENCE_QA: "参考图质量检查",
    STAGE_REFERENCE_CONSISTENCY: "参考图一致性检查",
    STAGE_WAITING_CONTINUITY: "等待上一镜尾帧",
    STAGE_WAITING_DEPENDENCY: "等待上一镜采用素材",
    STAGE_CONTINUITY_ASSEMBLING: "装配连续性参考",
    STAGE_VIDEO_READY: "视频输入已就绪",
    STAGE_WAITING_VIDEO_SLOT: "等待视频槽位",
    STAGE_VIDEO_SUBMITTING: "正在提交视频模型",
    STAGE_VIDEO_GENERATING: "视频模型生成中",
    STAGE_VIDEO_DOWNLOADING: "下载视频",
    STAGE_VIDEO_TECHNICAL: "视频技术校验",
    STAGE_VIDEO_QA: "视频内容质检",
    STAGE_AUTO_RETAKE: "自动重抽排队",
    STAGE_WAITING_HUMAN: "等待人工处理",
    STAGE_CANDIDATE_READY: "候选待采用",
    STAGE_ADOPTED: "已采用",
    STAGE_FAILED: "生成失败",
    STAGE_CANCELLED: "已停止",
    STAGE_PAUSED_BUDGET: "预算暂停",
    # 旧标签
    STAGE_REFERENCE: "准备参考图",
    STAGE_VIDEO_SUBMIT: "提交视频",
    STAGE_VIDEO_POLL: "视频模型生成中",
    STAGE_DOWNLOAD: "下载中",
    STAGE_ADOPT: "待采用",
}

# 旧阶段 → 新阶段映射（展示兼容）
LEGACY_STAGE_MAP = {
    STAGE_REFERENCE: STAGE_REFERENCE_GENERATE,
    STAGE_VIDEO_SUBMIT: STAGE_VIDEO_SUBMITTING,
    STAGE_VIDEO_POLL: STAGE_VIDEO_GENERATING,
    STAGE_DOWNLOAD: STAGE_VIDEO_DOWNLOADING,
    STAGE_QA: STAGE_VIDEO_QA,
    STAGE_ADOPT: STAGE_CANDIDATE_READY,
}
