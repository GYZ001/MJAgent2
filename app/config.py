"""全局配置：.env 加载 + 运行参数。禁止在代码中出现任何密钥字面量。

API Key 通过前端「监制房」页面填写，保存到 .env，后续启动自动加载。
用户只需提供各 provider 的 Key，其他配置（base URL、模型名等）均有合理默认值。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

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

# Seedance 参数边界：单镜时长按动作密度在 [MIN, MAX] 间由模型逐镜决定（5~15s）。
# 首尾帧模式下，简单/静态动作给短时长可避免人物停滞；强运动镜给长时长。
# Seedance 2.0 官方 duration 支持 [4,15]s；产品侧仍用 5s 作为最小镜头时长。
MIN_VIDEO_DURATION_S = 5
MAX_VIDEO_DURATION_S = 15  # Seedance 2.0 实测可出 15s；台词较多的镜头需要更长时长才念得完
FIXED_VIDEO_DURATION_S = 10  # 默认/兜底时长，也作为「每集基础节拍单元 ≈ 10s」的换算基准（可因口播压力额外拆镜）
ALLOWED_DURATIONS = set(range(MIN_VIDEO_DURATION_S, MAX_VIDEO_DURATION_S + 1))
EPISODE_TARGET_MIN_S = 40
EPISODE_TARGET_MAX_S = 90   # 放宽上限给模型更大质量保证空间：内容密/高潮集可取更长时长，简单集仍可短
EPISODE_TARGET_DEFAULT_S = 50
EPISODE_TARGET_STEP_S = FIXED_VIDEO_DURATION_S
# 自动分镜最多允许拆到 18 镜；总时长仍由 EPISODE_TARGET_MAX_S 和单镜最短时长共同约束。
STORYBOARD_MAX_SHOTS = 18
# 集目标时长合法取值：[MIN, MAX] 内 STEP 的整数倍（当前 40/50/60/70/80/90）。prompt 与校验统一引用，避免各处硬编码漂移。
EPISODE_TARGET_CHOICES = tuple(range(EPISODE_TARGET_MIN_S, EPISODE_TARGET_MAX_S + 1, EPISODE_TARGET_STEP_S))
COMPACT_SHOT_MAX_DURATION = FIXED_VIDEO_DURATION_S
LONG_SHOT_MIN_DURATION = FIXED_VIDEO_DURATION_S
LONG_SHOT_MIN_CHARS_PER_SECOND = 4
# 配音发声节奏：中文舒适念白约 4.5 字/秒（NARRATION_HARD_MAX=52 字判定为 10s 念不完，约 5.2 字/秒过快）。
# 镜头视频时长 duration_s 必须 ≥ 本镜台词+旁白的发声时间，否则画面动作会先于台词结束 → 音画不同步。
SPEECH_CHARS_PER_SECOND = 4.5
SPEECH_TAIL_BUFFER_S = 1.0  # 末字念完后留一点收势/换气时间，避免话音一落画面就切走
SPEECH_LEAD_IN_S = 0.8      # 开场留白：让本镜动作先建立一下，人物再开口/旁白再起，避免一上来就贴脸说话

# 第一集第一镜=全片开场建场镜，出片侧也差异化：拉长时长 + 强制远景建场 + 缓慢推近运镜。
ESTABLISHING_SHOT_DURATION_S = 12  # 固定较长时长，给足时间交代世界观/环境（介于常规与上限之间）
ESTABLISHING_SHOT_SIZE = "远景"   # 强制远景建场（最能交代环境与主角处境）
ESTABLISHING_CAMERA_MOVE = "推近"  # 缓慢推近：带观众从环境进入主角

# 单镜口播字数上限：必须能在 MAX 时长内念完（扣掉开场留白与收势），超出则提示拆分/精简，避免被截断。
MAX_SPOKEN_CHARS_PER_SHOT = int((MAX_VIDEO_DURATION_S - SPEECH_LEAD_IN_S - SPEECH_TAIL_BUFFER_S) * SPEECH_CHARS_PER_SECOND)
MAX_SLOW_SHOT_SHARE_DENOMINATOR = 3
MAX_LONG_SHOT_SHARE_DENOMINATOR = 5
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
    "use_first_frame_chaining": "true",  # 兼容旧设置；当前视频链路固定使用参考图模式
    "max_ref_images": "2",            # 单镜头最多附几张定妆照
    "auto_qa": "true",
    "auto_retake_threshold": "0.6",
    "plan_episode_count": "12",  # 分集"每批"集数（分批续写直至铺满全书，非总集数上限）
    "max_repair_attempts": "8",  # LLM 输出校验失败的最大修复重试次数（含首次）；模型不可用不走此重试
    "model_route": "hiagent",           # 文本/质检模型路由：hiagent（火山）| openrouter
    "storyboard_concurrency": "2",      # 手动批量分镜的并发上限
    "video_generation_enable_reference_image_mode": "true",
    "video_generation_default_mode": "AUTO",
    "video_reference_max_images": "8",
    "video_reference_first_shot_default_count": "4",
    "video_reference_first_shot_complex_count": "8",
    "video_reference_reuse_previous_scene_max_count": "4",
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
        env_file.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")

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
