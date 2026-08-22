"""全局配置：.env 加载 + 运行参数。禁止在代码中出现任何密钥字面量。

API Key 通过前端「监制房」页面填写，保存到 .env，后续启动自动加载。
用户只需提供各 provider 的 Key，其他配置（base URL、模型名等）均有合理默认值。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from app.atomic_io import atomic_write_text

ROOT = Path(__file__).resolve().parent.parent
TEST_PROFILE = os.environ.get("MANJU_TEST_PROFILE", "").strip().lower()
_test_sandbox = os.environ.get("MANJU_TEST_SANDBOX", "").strip()
if TEST_PROFILE == "isolated" and not _test_sandbox:
    raise RuntimeError(
        "MANJU_TEST_PROFILE=isolated requires MANJU_TEST_SANDBOX"
    )
RUNTIME_ROOT = (
    Path(_test_sandbox).expanduser().resolve()
    if TEST_PROFILE == "isolated"
    else ROOT
)
PROJECTS_DIR = RUNTIME_ROOT / "projects"
DATA_DIR = RUNTIME_ROOT / "data"
DB_PATH = DATA_DIR / "manju.db"

# 可通过前端管理的 API Key 列表
MANAGED_KEYS = ("HIAGENT_API_KEY", "OPENROUTER_API_KEY", "BAILIAN_API_KEY", "DEEPSEEK_API_KEY", "ZHIPU_API_KEY")

_env_lock = threading.Lock()


def _load_env() -> None:
    if TEST_PROFILE == "isolated":
        return
    env_file = RUNTIME_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

DEFAULT_HIAGENT_BASE_URL = "https://hia.volcenginepaas.com/api/aigw/v1"
HIAGENT_BASE_URL = os.environ.get(
    "HIAGENT_BASE_URL", DEFAULT_HIAGENT_BASE_URL
).rstrip("/")
HIAGENT_API_KEY = os.environ.get("HIAGENT_API_KEY", "")
# HiAgent 内置模型的技术标识。模型库、默认职责分配与真实调用必须共用同一来源，
# 避免“模型测试可用，但 active_model 仍为空”的双轨状态。
DEFAULT_HIAGENT_MODEL_TEXT = "d2a5n9rnvvm49eucvnvg"
DEFAULT_HIAGENT_MODEL_VLM = "d7ev7il5boeaebtf4sgg"
DEFAULT_HIAGENT_MODEL_VIDEO = "d7jf6nd5boeaebtfbdqg"
DEFAULT_HIAGENT_MODEL_IMAGE = "d7ute7ppcc7n89uuqqp0"
DEFAULT_MINIMAX_H3_MODEL_VIDEO = "minimax-h3"

# 局域网 MiniMax H3 ComfyUI 服务。该服务默认无鉴权；如部署侧重新启用
# Bearer Token，可仅通过环境变量注入，不在代码或数据库中保存明文。
MINIMAX_H3_BASE_URL = os.environ.get(
    "MINIMAX_H3_BASE_URL", "http://192.168.31.232:8181"
).rstrip("/")
MINIMAX_H3_API_KEY = os.environ.get("MINIMAX_H3_API_KEY", "")
MINIMAX_H3_VIDEO_WIDTH = max(
    32, min(4096, int(os.environ.get("MINIMAX_H3_VIDEO_WIDTH", "576")) // 32 * 32)
)
MINIMAX_H3_VIDEO_HEIGHT = max(
    32, min(4096, int(os.environ.get("MINIMAX_H3_VIDEO_HEIGHT", "1024")) // 32 * 32)
)
_minimax_h3_acceleration = os.environ.get(
    "MINIMAX_H3_ACCELERATION", "turbo"
).strip().lower()
MINIMAX_H3_ACCELERATION = (
    _minimax_h3_acceleration
    if _minimax_h3_acceleration in {"standard", "turbo"}
    else "turbo"
)
MINIMAX_H3_TURBO_PROFILE = (
    os.environ.get("MINIMAX_H3_TURBO_PROFILE", "quality").strip() or "quality"
)
MINIMAX_H3_VIDEO_VAE = (
    os.environ.get("MINIMAX_H3_VIDEO_VAE", "fp16").strip() or "fp16"
)
_minimax_h3_step_min, _minimax_h3_step_max = (
    (4, 8) if MINIMAX_H3_ACCELERATION == "turbo" else (1, 100)
)
MINIMAX_H3_STEPS = max(
    _minimax_h3_step_min,
    min(
        _minimax_h3_step_max,
        int(os.environ.get(
            "MINIMAX_H3_STEPS",
            "8" if MINIMAX_H3_ACCELERATION == "turbo" else "20",
        )),
    ),
)
MINIMAX_H3_USE_TE_SPEED = (
    os.environ.get(
        "MINIMAX_H3_USE_TE_SPEED",
        "false" if MINIMAX_H3_ACCELERATION == "turbo" else "true",
    ).strip().lower()
    not in {"0", "false", "off", "no"}
)
MINIMAX_H3_TURBO_STRENGTH = max(
    0.5, min(1.5, float(os.environ.get("MINIMAX_H3_TURBO_STRENGTH", "1.0")))
)
MINIMAX_H3_TURBO_LOW_VRAM = (
    os.environ.get("MINIMAX_H3_TURBO_LOW_VRAM", "false").strip().lower()
    in {"1", "true", "on", "yes"}
)
MINIMAX_H3_POLL_INTERVAL = max(
    2.0, min(5.0, float(os.environ.get("MINIMAX_H3_POLL_INTERVAL", "5")))
)

_model_text_env = os.environ.get("MODEL_TEXT", "").strip()
MODEL_TEXT = _model_text_env or DEFAULT_HIAGENT_MODEL_TEXT
MODEL_VLM = (
    os.environ.get("MODEL_VLM", "").strip()
    or _model_text_env
    or DEFAULT_HIAGENT_MODEL_VLM
)
MODEL_VIDEO = os.environ.get("MODEL_VIDEO", "").strip() or DEFAULT_HIAGENT_MODEL_VIDEO
MODEL_IMAGE = os.environ.get("MODEL_IMAGE", "").strip() or DEFAULT_HIAGENT_MODEL_IMAGE

# OpenRouter：文本 LLM（分集/分镜）与质检 VLM 的可选第二路由；图像/视频始终走火山 HiAgent。
# 路由选择存数据库 settings.model_route（hiagent|openrouter），可在监制房切换。
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL_TEXT = os.environ.get("OPENROUTER_MODEL_TEXT", "anthropic/claude-opus-4.8")
OPENROUTER_MODEL_VLM = os.environ.get("OPENROUTER_MODEL_VLM", "google/gemini-3.5-flash")
OPENROUTER_TEXT_REASONING_EFFORT = os.environ.get("OPENROUTER_TEXT_REASONING_EFFORT", "high")

# 阿里云百炼（DashScope）：文本 LLM（兼容模式 chat/completions）。
BAILIAN_BASE_URL = os.environ.get("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
BAILIAN_API_KEY = os.environ.get("BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
BAILIAN_MODEL_TEXT = os.environ.get("BAILIAN_MODEL_TEXT", "qwen3.7-max")
BAILIAN_MODEL_VLM = os.environ.get("BAILIAN_MODEL_VLM", "qwen3.7-plus")

# DeepSeek：仅作为 Text 模型路由，OpenAI 兼容 chat/completions。
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL_TEXT = os.environ.get("DEEPSEEK_MODEL_TEXT", "deepseek-v4-pro")

# 智谱官方 API：仅作为 Text 模型路由，兼容 chat/completions。
ZHIPU_BASE_URL = os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/paas/v4").rstrip("/")
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_MODEL_TEXT = os.environ.get("ZHIPU_MODEL_TEXT", "glm-5.2")

# 超时（秒）——依据 1.0 实测延迟：LLM ~22s、VLM ~57-66s（见 docs/HIAGENT_INTEGRATION.md §2）
TIMEOUT_CHAT_READ = float(os.environ.get("TIMEOUT_CHAT_READ", "300"))
# 场次分片写作的线上 P99 约 160s；按 P99×3 单独放宽到 480s，
# 避免与更长的语义审稿共用 baseline 上限。
TIMEOUT_CHAT_SCENE_SHARD_READ = float(
    os.environ.get("TIMEOUT_CHAT_SCENE_SHARD_READ", "480")
)
# 整版剧本需要同时生成骨架、场次、对白链与正文，实测长章可超过 300s。
# 单独放宽该阶段，不让短请求共享一个过大超时，也避免已接近完成时整次重发。
TIMEOUT_CHAT_BASELINE_READ = float(os.environ.get("TIMEOUT_CHAT_BASELINE_READ", "600"))
# 整集视频计划会携带全部镜头合同，当前项目实测请求体约 72KB，推理耗时可超过
# 通用 300s。使用独立 600s 上限，避免旧请求仍在服务端运行时重放并触发 TPM 限流。
TIMEOUT_CHAT_VIDEO_PLAN_READ = float(
    os.environ.get("TIMEOUT_CHAT_VIDEO_PLAN_READ", "600")
)
# 分镜大纲同样是长结构化生成：需要一次性铺完整集节奏与交付 ID。推理模型在
# 上游繁忙时可能超过通用 300s；过早断开后立刻重放会让仍在服务端运行的旧请求
# 与新请求叠加，进一步触发 TPM 限流。
TIMEOUT_CHAT_STORYBOARD_OUTLINE_READ = float(
    os.environ.get("TIMEOUT_CHAT_STORYBOARD_OUTLINE_READ", "600")
)
# 蓝图语义审稿是长结构化生成：完整复审要把整份蓝图 + 来源投影一起送审。
# 曾因一次 312s ReadTimeout 放宽到 600s，但那次同样是 0 字节卡死而不是慢审稿：
# ep_3d523ff4d0a4 的三次成功审稿是 35.3s/37.1s/45.9s（1202/1875/2138 tokens），
# 而失败那次在 618.9s 时仍然 received_chars=0。按 54 次同网关调用拟合出的
# latency ≈ 3.7s + chars/170，即使跑满 16384 tokens（≈36K 字）也只要 ~216s，
# 所以 600s 只是把卡死的空等拉长到 10 分钟，并不能救活任何一次真实审稿。
# 收紧到 360s：对理论满额输出仍有 1.7× 余量，卡死则少空等 4 分钟。
# 注意这只是止血——真正的问题是「两名审稿人少一个就废掉整份蓝图」，见 stages.py。
TIMEOUT_CHAT_BLUEPRINT_REVIEW_READ = float(
    os.environ.get("TIMEOUT_CHAT_BLUEPRINT_REVIEW_READ", "360")
)
# 蓝图分片是本流水线里最短的一类结构化生成：ep_3d523ff4d0a4 的 54 次成功调用
# 中最慢一次 67.8s（13K 字输出）。上游网关实际是「攒够再吐」，读超时因此等价于
# 整次生成超时，用通用 300s 意味着一次卡死要空等 5 分钟才暴露，而且会把
# 「什么都没发生」升级成 outcome-unknown、必须人工签发 Production Grant。
# 按实测最慢值 ×2.6 单独收紧到 180s：健康调用留足余量，卡死更早暴露。
TIMEOUT_CHAT_BLUEPRINT_SHARD_READ = float(
    os.environ.get("TIMEOUT_CHAT_BLUEPRINT_SHARD_READ", "180")
)
# 人物身份预检的读超时。曾按一个非推理模型标定到 120s（当时实测最慢 38.0s，
# 理论满额约 57s）。换成推理模型后这个标定失效：推理耗时计入同一次请求，
# 生产上一次身份调用跑到 123.4s 才被这个上限掐断，整集随之失败（EP5）。
# 常量的口径没变——仍然是"给正常调用留足余量，同时让卡死尽早暴露"——只是
# 基准换成了会先思考再作答的模型；模型再换时必须重新按实测标定。
TIMEOUT_CHAT_IDENTITY_READ = float(
    os.environ.get("TIMEOUT_CHAT_IDENTITY_READ", "420")
)
TIMEOUT_VIDEO_CREATE = 30.0
TIMEOUT_VIDEO_POLL = 30.0
TIMEOUT_DOWNLOAD = 180.0
# Base64 图片上传需要独立的写超时；不应沿用 httpx 默认的 30/60s。
# 图片生成与 VLM 共用并发门，避免多个视频 job 同时上传大体积 Base64 抢占带宽。
TIMEOUT_IMAGE_READ = float(os.environ.get("TIMEOUT_IMAGE_READ", "180"))
TIMEOUT_IMAGE_WRITE = float(os.environ.get("TIMEOUT_IMAGE_WRITE", "120"))
TIMEOUT_VLM_READ = float(os.environ.get("TIMEOUT_VLM_READ", "300"))
TIMEOUT_VLM_WRITE = float(os.environ.get("TIMEOUT_VLM_WRITE", "120"))
# 兼容旧环境变量；媒体流水线 V2 已拆成 image / vlm 独立通道（见 DEFAULT_SETTINGS）。
MEDIA_REQUEST_CONCURRENCY = max(1, int(os.environ.get("MEDIA_REQUEST_CONCURRENCY", "2")))
IMAGE_REQUEST_CONCURRENCY = max(1, int(os.environ.get("IMAGE_REQUEST_CONCURRENCY", "4")))
VLM_REQUEST_CONCURRENCY = max(1, int(os.environ.get("VLM_REQUEST_CONCURRENCY", "6")))
# 仅压缩上传给图生图/VLM 的输入，不改变 Seedream 输出分辨率。
MEDIA_INPUT_MAX_EDGE = max(512, int(os.environ.get("MEDIA_INPUT_MAX_EDGE", "1280")))
# ffmpeg JPEG qscale：2 最高质量，31 最低质量。
MEDIA_INPUT_JPEG_QUALITY = min(31, max(2, int(os.environ.get("MEDIA_INPUT_JPEG_QUALITY", "5"))))
VIDEO_POLL_INTERVAL = 10.0
# Phase 1：提交后单次查询即释放 worker；不再用 15 分钟连续占槽窗口。
# 保留 VIDEO_POLL_BUDGET 仅作兼容旧测试/文档，实际轮询路径不再占用该预算。
VIDEO_POLL_BUDGET = 0
VIDEO_POLL_RESUME_DELAY = float(os.environ.get("VIDEO_POLL_RESUME_DELAY", "10"))
# 供应商任务允许的总墙钟时间。用于防止上游永远停在 running；正常长任务跨越
# 多次 waiting_provider 轮询继续等待。
VIDEO_PROVIDER_MAX_WAIT = float(os.environ.get("VIDEO_PROVIDER_MAX_WAIT", str(6 * 60 * 60)))

# 上游瞬时故障（超时/网络/限流/5xx）的 job 级自动重试。_post_json 的单次调用内重试只覆盖约 90s，
# 扛不住分钟级的上游抖动；没有 job 级兜底时，一次可恢复的瞬时故障会把整镜任务永久判失败、逼人工重试。
# 退避按 BASE * 2^(attempt-1) 秒：30s / 60s / 120s，三次合计 ~3.5min，足以越过常见的上游瞬时抖动。
VIDEO_JOB_MAX_RETRIES = 3
VIDEO_JOB_RETRY_BASE_DELAY = 30.0
# 视频入队前校验也必须进入持久任务生命周期。校验本身不调用付费供应商，
# 因此可以用更短的有界重试；确定性的内容门禁失败会直接转人工，不盲目重放。
VIDEO_PREFLIGHT_MAX_RETRIES = max(
    0, int(os.environ.get("VIDEO_PREFLIGHT_MAX_RETRIES", "2"))
)
VIDEO_PREFLIGHT_RETRY_BASE_DELAY = max(
    1.0, float(os.environ.get("VIDEO_PREFLIGHT_RETRY_BASE_DELAY", "15"))
)
VIDEO_PREFLIGHT_VALIDATION_TIMEOUT = max(
    10.0, float(os.environ.get("VIDEO_PREFLIGHT_VALIDATION_TIMEOUT", "45"))
)
# 连续镜只有在上游已无可恢复任务时才会使用该超时降级；上游仍在真实生成时
# 不会因为超过此时长而断开连续性。
VIDEO_CONTINUITY_ORPHAN_TIMEOUT = max(
    30.0, float(os.environ.get("VIDEO_CONTINUITY_ORPHAN_TIMEOUT", "180"))
)

# 文本模型调用只在连接阶段能证明请求未送达时，由 Harness 做外层有界重试。
# ReadTimeout、流中断和其他已发送后的不确定结果必须等待页面显式重试，避免重复计费。
TEXT_PROVIDER_MAX_RETRIES = max(0, int(os.environ.get("TEXT_PROVIDER_MAX_RETRIES", "3")))
TEXT_PROVIDER_RETRY_BASE_DELAY = max(
    0.0, float(os.environ.get("TEXT_PROVIDER_RETRY_BASE_DELAY", "30"))
)

# 逐镜生成一次只输出一个 Shot JSON。沿用整集剧本的 65535 输出预算会把短请求
# 计入过高的 TPM 配额并触发 429；8192 足够容纳单镜候选及定向修复，同时保留余量。
STORYBOARD_SHOT_MAX_TOKENS = max(
    1024, min(int(os.environ.get("STORYBOARD_SHOT_MAX_TOKENS", "8192")), 16384)
)

# 仅供无 narrative_plan 的历史大纲路径使用；新叙事剧本由本地编译器生成大纲。
STORYBOARD_OUTLINE_MAX_TOKENS = max(
    8192, min(int(os.environ.get("STORYBOARD_OUTLINE_MAX_TOKENS", "32768")), 65536)
)

# 场景画面创作按有界镜头块调用模型，避免长场次重新形成整集大响应。
STORYBOARD_SCENE_PACK_MAX_SHOTS = max(
    1,
    min(
        int(os.environ.get("STORYBOARD_SCENE_PACK_MAX_SHOTS", "8")),
        16,
    ),
)

# 分镜时长：默认 5s（PREFERRED）；6~10s 仅当口播/连续动作需要，并进入 AI 审核。
# DEFAULT 仅用于人工输入缺省值，模型输出必须经校验器显式落在合法区间内。
VIDEO_DURATION_MIN_S = 5
VIDEO_DURATION_MAX_S = 10
DEFAULT_VIDEO_DURATION_S = VIDEO_DURATION_MIN_S
ALLOWED_DURATIONS = frozenset(range(VIDEO_DURATION_MIN_S, VIDEO_DURATION_MAX_S + 1))
EPISODE_TARGET_MIN_S = 40
EPISODE_TARGET_MAX_S = None  # 整集时长不设产品上限；完整剧情与容量估算决定最终值
EPISODE_TARGET_DEFAULT_S = 50
EPISODE_TARGET_STEP_S = 10  # 用户输入是最低节奏参考；生成后只允许按实际容量向上扩展
# 仅用于防止异常模型无限循环的技术熔断，不是产品镜头数上限。
STORYBOARD_MAX_SHOTS = 1_000_000
# 常用建议值仅供 UI 快捷输入，不构成合法值上限。
EPISODE_TARGET_CHOICES = tuple(range(EPISODE_TARGET_MIN_S, 181, EPISODE_TARGET_STEP_S))
# 口播预算（纯文字、不计标点）：5 秒 18 字，10 秒 36 字。
# 超过 10 秒所能承载的口播仍必须拆镜，不能靠延长 duration_s 合并不同节拍。
SPOKEN_CHARS_PER_5_SECONDS = 18


def max_spoken_chars_for_duration(duration_s: int) -> int:
    """单镜口播纯文字上限；与 continuity.max_speech_chars 同口径。"""
    duration = min(max(int(duration_s), VIDEO_DURATION_MIN_S), VIDEO_DURATION_MAX_S)
    return duration * SPOKEN_CHARS_PER_5_SECONDS // VIDEO_DURATION_MIN_S


MAX_SPOKEN_CHARS_PER_SHOT = max_spoken_chars_for_duration(VIDEO_DURATION_MAX_S)
PROMPT_CHAR_LIMIT = 8000  # 与生成台提示词编辑合同一致
VIDEO_PRICE_PER_SECOND = 0.8  # CNY，1.0 配置单价

# Seedream 定妆照（实测：尺寸下限 3,686,400 像素；1440x2560 与视频 9:16 同比例）
REF_IMAGE_SIZE = "1440x2560"
IMAGE_PRICE_PER_UNIT = 0.2  # CNY

# 可在 settings 表覆盖的默认值
DEFAULT_SETTINGS = {
    # 模型 token 能力探测结果；未知/既有模型由运行时兼容为 128K context / 32K output。
    "model_token_capabilities": "{}",
    # 兼容旧键；新调度以分通道为准（见 media_pipeline.concurrency）
    "video_concurrency": "15",
    "auto_concurrency": "15",
    "reference_pipeline_concurrency": "15",
    "image_request_concurrency": "4",
    "vlm_request_concurrency": "6",
    "video_submit_concurrency": "15",
    "video_inflight_limit": "15",
    "video_poll_concurrency": "15",
    "download_concurrency": "3",
    "finalize_concurrency": "4",
    "episode_video_inflight_limit": "15",
    "project_video_inflight_limit": "15",
    "reference_prepared_backlog": "8",
    # QPSP 调度：高低水位 / cohort / 策略开关
    "media_scheduler_policy": "stage_aware",  # legacy | stage_aware
    "video_ready_low_watermark": "2",
    "video_ready_high_watermark": "6",
    "reference_shot_cohort_limit": "15",
    "video_qa_reserved_concurrency": "2",
    "video_control_reserved_concurrency": "2",
    "video_reference_batch_prompt": "true",   # P1：一镜一次提示词合同
    "video_reference_role_adaptive": "false", # P2：质量角色自适应（实验，默认关）
    "video_plan_confidence_floor": "0.55",
    "video_plan_allow_unknown_dimensions": "false",
    # 本地项目媒体映射到自有对象存储/CDN 的公开基址；为空时视频输入明确阻断。
    "provider_media_public_base_url": "",
    "provider_media_max_download_bytes": str(512 * 1024 * 1024),
    "episode_cost_limit_cny": "100",
    "use_character_refs": "true",     # 出场角色定妆照随镜头注入 reference_image（跨集一致性核心）
    "max_ref_images": "2",            # 单镜头最多附几张定妆照
    "auto_qa": "true",
    # VAL-422：口播一致性 / 结构化主线门禁分阶段开关
    "spoken_contract_audit_mode": "enforce",  # audit_only | enforce
    "spine_structured_hard_gate": "true",     # false 时 LEGACY_COVERAGE_UNCERTAIN 降为 warning
    "max_repair_attempts": "8",  # LLM 输出校验失败的最大修复重试次数（含首次）；模型不可用不走此重试
    "screenplay_qa_pass_score": "80",  # 剧本生产门禁；低于此分或存在 blocker 时由 Repair 修复后复验
    "model_route": "hiagent",           # 文本/质检模型路由：hiagent（火山）| openrouter
    # 职责分配必须落到明确 Model ID；init_db 以 INSERT OR IGNORE 补齐旧库。
    # provider 仍沿用 active_provider 的旧版 model_route 兼容逻辑，避免覆盖历史路由。
    "hiagent_model_text": MODEL_TEXT,
    "hiagent_model_vlm": MODEL_VLM,
    "hiagent_model_video": MODEL_VIDEO,
    "hiagent_model_image": MODEL_IMAGE,
    "minimax_h3_model_video": DEFAULT_MINIMAX_H3_MODEL_VIDEO,
    "minimax_h3_base_url": MINIMAX_H3_BASE_URL,
    "text_generation_concurrency": "10", # 剧本与分镜共享文本模型资源池
    "text_generation_workflow_concurrency": "10", # 活跃文本工作流；真实请求另受 provider call gate 约束
    "screenplay_scene_shards_enabled": "true",
    "screenplay_targeted_identity_enabled": "true",
    "screenplay_targeted_blueprint_review_enabled": "true",
    "screenplay_scene_shard_parallelism": "2",
    "screenplay_scene_shard_max_units": "24",
    "screenplay_scene_shard_max_output_chars": "12000",
    # 语义审查的 compact 最坏合法 JSON 之外，预留有界的格式布局/修复空间。
    # 仅在 compact 需求已超过 2048-token 短请求 floor 时生效。
    "screenplay_scene_semantic_review_output_reserve_percent": "100",
    "screenplay_format_retry_limit": "1",
    "screenplay_semantic_retry_limit": "2",
    "screenplay_fidelity_max_rounds": "8",
    "text_stream_total_timeout_s": "1200", # 流式文本调用总墙钟熔断；空闲超时仍由 httpx 负责
    "storyboard_concurrency": "2",      # 旧设置兼容读取，不再作为新资源池名称
    # PRD-03 分镜台独立灰度/回滚开关；P0 服务端防线不受 UI 开关影响。
    "storyboard_workspace_safe_readonly": "false",
    "storyboard_structure_edit_enabled": "true",
    "storyboard_source_rebind_enabled": "true",
    "video_reference_max_images": "9",
    "video_reference_quality_threshold": "0.8",  # 综合 QA 分门禁：≥此分必须留在「使用中」
    "video_reference_quality_floor": "0.4", # 兜底图质量地板：生成图全不达标时，最佳一版仍低于此分则不喂模型，只靠定妆照/场景锚点（脏图反而拖累成片）
    "video_reference_min_generated": "1",   # 参考图模式每镜至少新生成几张关键帧参考图（防止只剩定妆照）
    "video_supporting_keyframe_candidates": "3", # 每张辅助时序关键帧同样固定生成 3 张，并独立择优保留 1 张
    "video_reference_gen_retries": "2",     # 单张生成参考图 QA 不达标时的额外重试次数；仍不达标保留最佳一版而非丢弃
    "video_reference_prompt_async": "true", # 每张新参考图的提示词用独立 LLM 调用并发生成（防止一次性写多张时偷懒）
    "video_reference_consistency_check": "true",       # Phase 2：整组参考图相对一致性检查（扣分 + 可选 i2i 重生提分）
    "video_reference_consistency_threshold": "0.7",    # 仅触发 i2i 重生尝试的内部线，不再单独决定废弃
    "video_reference_consistency_retries": "1",        # 漂移图从锚点 i2i 重生的最大次数；仍漂移则只靠综合分门禁
    "provider_call_retention_days": "30",
    "error_log_retention_days": "30",
    "agent_enabled": "true",            # 内嵌对话 Agent 总开关（API 入口会检查）
    "agent_max_tool_calls_per_turn": "8",
    "agent_max_consecutive_same_error": "2",
    # 人物多视角资产与关键帧一致性 QA
    "character_multiview_enabled": "true",
    "scene_multiview_enabled": "true",
    "narrative_keyframe_required": "true",
    "visual_evidence_qa_enabled": "true",
    "video_visual_anchor_qa_enabled": "true",
    # 水印门禁可配置；reject 严格拒绝，ignore_unless_occluding 只放行不遮挡主体的供应商角落标识。
    "watermark_qa_mode": "reject",
    "keyframe_qa_overall_threshold": "0.80",
    "keyframe_qa_action_threshold": "0.70",
    "keyframe_qa_body_threshold": "0.72",
    "keyframe_qa_identity_threshold": "0.75",
}

PROJECTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)


# ---------- API Key 管理：前端填写 → 持久化 .env → 运行时热更新 ----------

def save_keys_to_env(keys: dict[str, str]) -> list[str]:
    """将 API Key 保存到 .env 文件（读-合并-写），并更新运行时变量。

    keys: {"HIAGENT_API_KEY": "xxx", ...}，只接受 MANAGED_KEYS 中的键。
    返回实际更新的 key 名列表。空字符串视为删除（保留原值不动）。
    """
    updated: list[str] = []
    to_write: dict[str, str] = {}
    for k, v in keys.items():
        if k not in MANAGED_KEYS:
            continue
        v = (v or "").strip()
        if v:
            to_write[k] = v
            updated.append(k)

    if not to_write:
        return updated

    env_file = RUNTIME_ROOT / ".env"
    with _env_lock:
        # 读取现有 .env 内容
        existing_lines: list[str] = []
        existing_keys: dict[str, int] = {}  # key → line index
        if env_file.exists():
            for i, line in enumerate(env_file.read_text(encoding="utf-8").splitlines()):
                existing_lines.append(line)
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    ek, _, _ = stripped.partition("=")
                    existing_keys[ek.strip()] = i

        # 合并：已有的替换值，没有的追加
        for k, v in to_write.items():
            if k in existing_keys:
                existing_lines[existing_keys[k]] = f"{k}={v}"
            else:
                existing_lines.append(f"{k}={v}")

        # 写回 .env
        atomic_write_text(env_file, "\n".join(existing_lines) + "\n")

        # 更新 os.environ 和模块级变量
        for k, v in to_write.items():
            os.environ[k] = v

    # 热更新模块级变量
    _reload_keys()
    return updated


def _reload_keys() -> None:
    """从 os.environ 重新加载 API Key 相关的模块级变量。"""
    global HIAGENT_API_KEY, OPENROUTER_API_KEY, BAILIAN_API_KEY, DEEPSEEK_API_KEY, ZHIPU_API_KEY
    HIAGENT_API_KEY = os.environ.get("HIAGENT_API_KEY", "")
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    BAILIAN_API_KEY = os.environ.get("BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
    ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")


def get_key_status() -> dict[str, dict]:
    """返回各 provider 的 key 配置状态（不暴露完整 key 值）。"""
    result = {}
    for key_name in MANAGED_KEYS:
        val = os.environ.get(key_name, "")
        provider = key_name.replace("_API_KEY", "").lower()
        if provider == "hiagent":
            label = "火山引擎"
        elif provider == "openrouter":
            label = "OpenRouter"
        elif provider == "bailian":
            label = "百炼（阿里云）"
        elif provider == "deepseek":
            label = "DeepSeek"
        elif provider == "zhipu":
            label = "智谱（官方）"
        else:
            label = provider
        result[provider] = {
            "key_name": key_name,
            "label": label,
            "configured": bool(val),
            # 不回传前后缀 preview，避免泄露密钥族信息（Todolist T2/S15）
            "preview": "已配置" if val else "",
        }
    return result
