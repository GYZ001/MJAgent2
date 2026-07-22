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

HIAGENT_BASE_URL = os.environ.get("HIAGENT_BASE_URL", "").rstrip("/")
HIAGENT_API_KEY = os.environ.get("HIAGENT_API_KEY", "")
MODEL_TEXT = os.environ.get("MODEL_TEXT", "")
MODEL_VIDEO = os.environ.get("MODEL_VIDEO", "")
MODEL_IMAGE = os.environ.get("MODEL_IMAGE", "")
MODEL_VLM = os.environ.get("MODEL_VLM", MODEL_TEXT)

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
TIMEOUT_CHAT_READ = 300.0
TIMEOUT_VIDEO_CREATE = 30.0
TIMEOUT_VIDEO_POLL = 30.0
TIMEOUT_DOWNLOAD = 180.0
# Base64 图片上传需要独立的写超时；不应沿用 httpx 默认的 30/60s。
# 图片生成与 VLM 共用并发门，避免多个视频 job 同时上传大体积 Base64 抢占带宽。
TIMEOUT_IMAGE_READ = float(os.environ.get("TIMEOUT_IMAGE_READ", "180"))
TIMEOUT_IMAGE_WRITE = float(os.environ.get("TIMEOUT_IMAGE_WRITE", "120"))
TIMEOUT_VLM_READ = float(os.environ.get("TIMEOUT_VLM_READ", "300"))
TIMEOUT_VLM_WRITE = float(os.environ.get("TIMEOUT_VLM_WRITE", "120"))
MEDIA_REQUEST_CONCURRENCY = max(1, int(os.environ.get("MEDIA_REQUEST_CONCURRENCY", "2")))
# 仅压缩上传给图生图/VLM 的输入，不改变 Seedream 输出分辨率。
MEDIA_INPUT_MAX_EDGE = max(512, int(os.environ.get("MEDIA_INPUT_MAX_EDGE", "1280")))
# ffmpeg JPEG qscale：2 最高质量，31 最低质量。
MEDIA_INPUT_JPEG_QUALITY = min(31, max(2, int(os.environ.get("MEDIA_INPUT_JPEG_QUALITY", "5"))))
VIDEO_POLL_INTERVAL = 10.0
VIDEO_POLL_BUDGET = 15 * 60  # 单任务轮询总预算

# 上游瞬时故障（超时/网络/限流/5xx）的 job 级自动重试。_post_json 的单次调用内重试只覆盖约 90s，
# 扛不住分钟级的上游抖动；没有 job 级兜底时，一次可恢复的瞬时故障会把整镜任务永久判失败、逼人工重试。
# 退避按 BASE * 2^(attempt-1) 秒：30s / 60s / 120s，三次合计 ~3.5min，足以越过常见的上游瞬时抖动。
VIDEO_JOB_MAX_RETRIES = 3
VIDEO_JOB_RETRY_BASE_DELAY = 30.0

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

# 分镜时长由模型按单镜动作与口播密度判断；供应商只接受 5~10 秒整数时长。
# DEFAULT 仅用于人工输入缺省值，模型输出必须经校验器显式落在合法区间内。
VIDEO_DURATION_MIN_S = 5
VIDEO_DURATION_MAX_S = 10
DEFAULT_VIDEO_DURATION_S = VIDEO_DURATION_MIN_S
ALLOWED_DURATIONS = frozenset(range(VIDEO_DURATION_MIN_S, VIDEO_DURATION_MAX_S + 1))
EPISODE_TARGET_MIN_S = 40
EPISODE_TARGET_MAX_S = 90   # 放宽上限给模型更大质量保证空间：内容密/高潮集可取更长时长，简单集仍可短
EPISODE_TARGET_DEFAULT_S = 50
EPISODE_TARGET_STEP_S = 10  # 分集规划字段仅保留为节奏参考，不再约束分镜总时长
# 仅用于防止异常模型无限循环的技术熔断，不是产品镜头数上限。
STORYBOARD_MAX_SHOTS = 1_000_000
# 集目标时长合法取值：[MIN, MAX] 内 STEP 的整数倍（当前 40/50/60/70/80/90）。prompt 与校验统一引用，避免各处硬编码漂移。
EPISODE_TARGET_CHOICES = tuple(range(EPISODE_TARGET_MIN_S, EPISODE_TARGET_MAX_S + 1, EPISODE_TARGET_STEP_S))
# 口播预算随模型选择的镜头时长线性增长：5 秒 14 字，10 秒 28 字。
# 超过 10 秒所能承载的口播仍必须拆镜，不能靠延长 duration_s 合并不同节拍。
SPOKEN_CHARS_PER_5_SECONDS = 14


def max_spoken_chars_for_duration(duration_s: int) -> int:
    duration = min(max(int(duration_s), VIDEO_DURATION_MIN_S), VIDEO_DURATION_MAX_S)
    return duration * SPOKEN_CHARS_PER_5_SECONDS // VIDEO_DURATION_MIN_S


MAX_SPOKEN_CHARS_PER_SHOT = max_spoken_chars_for_duration(VIDEO_DURATION_MAX_S)
PROMPT_CHAR_LIMIT = 1500  # 保守值，触发真实上限后回填
VIDEO_PRICE_PER_SECOND = 0.8  # CNY，1.0 配置单价

# Seedream 定妆照（实测：尺寸下限 3,686,400 像素；1440x2560 与视频 9:16 同比例）
REF_IMAGE_SIZE = "1440x2560"
IMAGE_PRICE_PER_UNIT = 0.2  # CNY

# 可在 settings 表覆盖的默认值
DEFAULT_SETTINGS = {
    "video_concurrency": "2",
    "episode_cost_limit_cny": "100",
    "use_character_refs": "true",     # 出场角色定妆照随镜头注入 reference_image（跨集一致性核心）
    "max_ref_images": "2",            # 单镜头最多附几张定妆照
    "auto_qa": "true",
    "auto_retake_threshold": "0.6",
    "max_repair_attempts": "8",  # LLM 输出校验失败的最大修复重试次数（含首次）；模型不可用不走此重试
    "model_route": "hiagent",           # 文本/质检模型路由：hiagent（火山）| openrouter
    "storyboard_concurrency": "2",      # 手动批量分镜的并发上限
    "video_reference_max_images": "8",
    "video_reference_quality_threshold": "0.75",
    "video_reference_quality_floor": "0.4", # 兜底图质量地板：生成图全不达标时，最佳一版仍低于此分则不喂模型，只靠定妆照/场景锚点（脏图反而拖累成片）
    "video_reference_min_generated": "1",   # 参考图模式每镜至少新生成几张关键帧参考图（防止只剩定妆照）
    "video_reference_gen_retries": "2",     # 单张生成参考图 QA 不达标时的额外重试次数；仍不达标保留最佳一版而非丢弃
    "video_reference_prompt_async": "true", # 每张新参考图的提示词用独立 LLM 调用并发生成（防止一次性写多张时偷懒）
    "video_reference_consistency_check": "true",       # Phase 2：整组参考图相对一致性检查 Agent（点名漂移图并 i2i 重生/剔除）
    "video_reference_consistency_threshold": "0.7",    # 候选参考图与锚点（定妆照/上镜尾帧）的一致性达标线，低于则判漂移
    "video_reference_consistency_retries": "1",        # 漂移图从锚点 i2i 重生的最大次数；仍漂移则剔除（不喂 Seedance）
    "auto_concurrency": "24",           # 一键全自动：图像/视频 worker 并发槽数（公网网关吞吐强，可调大）
    "auto_storyboard_concurrency": "8", # 一键全自动：同时进行的分镜 LLM 数（各集流水线并行，分镜阶段单独限流）
    "provider_call_retention_days": "30",
    "error_log_retention_days": "30",
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
            "preview": f"{val[:6]}...{val[-4:]}" if len(val) > 10 else ("已配置" if val else ""),
        }
    return result
