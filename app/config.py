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
PROJECTS_DIR = ROOT / "projects"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "manju.db"

# 可通过前端管理的 API Key 列表
MANAGED_KEYS = ("HIAGENT_API_KEY", "OPENROUTER_API_KEY", "BAILIAN_API_KEY", "DEEPSEEK_API_KEY", "ZHIPU_API_KEY")

_env_lock = threading.Lock()


def _load_env() -> None:
    env_file = ROOT / ".env"
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

# 文本模型调用由 Harness 网关做外层有界重试。provider adapter 内部的 1.5s / 3s
# 快速重试只处理瞬时网络抖动，跨不过按分钟计算的 TPM 限流窗口；外层退避仍重放
# 同一份请求，因此不会额外消耗 AgentLoop 的内容修复轮次。
TEXT_PROVIDER_MAX_RETRIES = max(0, int(os.environ.get("TEXT_PROVIDER_MAX_RETRIES", "3")))
TEXT_PROVIDER_RETRY_BASE_DELAY = max(
    0.0, float(os.environ.get("TEXT_PROVIDER_RETRY_BASE_DELAY", "30"))
)

# 逐镜生成一次只输出一个 Shot JSON。沿用整集剧本的 65535 输出预算会把短请求
# 计入过高的 TPM 配额并触发 429；8192 足够容纳单镜候选及定向修复，同时保留余量。
STORYBOARD_SHOT_MAX_TOKENS = max(
    1024, min(int(os.environ.get("STORYBOARD_SHOT_MAX_TOKENS", "8192")), 16384)
)

# 分镜大纲需要一次性输出整集节奏与镜头合同。4K 会让推理模型在正文前耗尽预算；
# 默认提升到约 32K，同时保留环境变量以便对供应商的更低硬上限做部署级覆盖。
STORYBOARD_OUTLINE_MAX_TOKENS = max(
    8192, min(int(os.environ.get("STORYBOARD_OUTLINE_MAX_TOKENS", "32768")), 65536)
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
    "screenplay_qa_pass_score": "80",  # 剧本 QA 只评估；低于此分由独立 Repair 修复后复验
    "model_route": "hiagent",           # 文本/质检模型路由：hiagent（火山）| openrouter
    # 职责分配必须落到明确 Model ID；init_db 以 INSERT OR IGNORE 补齐旧库。
    # provider 仍沿用 active_provider 的旧版 model_route 兼容逻辑，避免覆盖历史路由。
    "hiagent_model_text": MODEL_TEXT,
    "hiagent_model_vlm": MODEL_VLM,
    "hiagent_model_video": MODEL_VIDEO,
    "hiagent_model_image": MODEL_IMAGE,
    "storyboard_concurrency": "2",      # 手动批量分镜的并发上限
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

    env_file = ROOT / ".env"
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
