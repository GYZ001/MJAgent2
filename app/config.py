"""全局配置：.env 加载 + 运行参数。禁止在代码中出现任何密钥字面量。

API Key 通过前端「监制房」页面填写，保存到 .env，后续启动自动加载。
用户只需提供三个 provider 的 Key，其他配置（base URL、模型名等）均有合理默认值。
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
MANAGED_KEYS = ("HIAGENT_API_KEY", "OPENROUTER_API_KEY", "BAILIAN_API_KEY")

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

# 阿里云百炼（DashScope）：文本 LLM（兼容模式 chat/completions），以及音频 TTS/ASR。
BAILIAN_BASE_URL = os.environ.get("BAILIAN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
BAILIAN_API_KEY = os.environ.get("BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")
BAILIAN_MODEL_TEXT = os.environ.get("BAILIAN_MODEL_TEXT", "qwen3.7-max")
BAILIAN_MODEL_VLM = os.environ.get("BAILIAN_MODEL_VLM", "qwen3.7-plus")

# 音频（TTS 配音 + ASR 校验）走百炼/DashScope，独立于文本/VLM 路由，由 settings.audio_enabled 总开关控制。
# TTS 用 DashScope 原生多模态生成端点（兼容模式无 /audio/speech，已实测 404）；ASR 用兼容模式 omni（base64 输入）。
BAILIAN_NATIVE_TTS_URL = os.environ.get(
    "BAILIAN_NATIVE_TTS_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
BAILIAN_TTS_MODEL = os.environ.get("BAILIAN_TTS_MODEL", "qwen3-tts-flash")
BAILIAN_TTS_VOICE = os.environ.get("BAILIAN_TTS_VOICE", "Cherry")
BAILIAN_ASR_MODEL = os.environ.get("BAILIAN_ASR_MODEL", "qwen3-omni-flash")
TIMEOUT_AUDIO = 90.0

# 超时（秒）——依据 1.0 实测延迟：LLM ~22s、VLM ~57-66s（见 docs/HIAGENT_INTEGRATION.md §2）
TIMEOUT_CHAT_READ = 300.0
TIMEOUT_VIDEO_CREATE = 30.0
TIMEOUT_VIDEO_POLL = 30.0
TIMEOUT_DOWNLOAD = 180.0
VIDEO_POLL_INTERVAL = 10.0
VIDEO_POLL_BUDGET = 15 * 60  # 单任务轮询总预算

# Seedance 参数边界（M0 2026-06-12 实测：网关接受更宽范围，但当前产品策略固定 10s；
# 网关无同步校验，必须前置。每个 10s 视频段内用 prompt 推动模型塞入更多小镜头和剧情节点。）
FIXED_VIDEO_DURATION_S = 10
ALLOWED_DURATIONS = {FIXED_VIDEO_DURATION_S}
EPISODE_TARGET_MIN_S = 40
EPISODE_TARGET_MAX_S = 60
EPISODE_TARGET_DEFAULT_S = 50
EPISODE_TARGET_STEP_S = FIXED_VIDEO_DURATION_S
COMPACT_SHOT_MAX_DURATION = FIXED_VIDEO_DURATION_S
LONG_SHOT_MIN_DURATION = FIXED_VIDEO_DURATION_S
LONG_SHOT_MIN_CHARS_PER_SECOND = 4
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
    "use_first_frame_chaining": "true",  # 兼容旧设置；当前链路使用预生成首/尾关键图衔接
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
    "video_reference_fallback_failures": "2",
    "video_mode_selector_confidence_threshold": "0.7",
    "auto_concurrency": "24",           # 一键全自动：图像/视频 worker 并发槽数（公网网关吞吐强，可调大）
    "auto_storyboard_concurrency": "8", # 一键全自动：同时进行的分镜 LLM 数（各集流水线并行，分镜阶段单独限流）
    # ---- 音频（TTS 配音 + ASR 校验）总开关与参数；关闭时全流程跳过音频，保持现有无声链路 ----
    "audio_enabled": "false",           # 总开关：开启后才生成配音并混入成片；关闭=维持现状（无声）
    "audio_voice": "Cherry",            # TTS 音色（qwen-tts/qwen3-tts-flash 支持 Cherry/Chelsie/Ethan/Serena 等）
    "audio_max_regen": "2",             # 单镜配音 ASR 预检失败后的最大改写重试次数
    "asr_cer_s": "0.03",                # S 级（人名/境界/结果）字符错误率上限
    "asr_cer_a": "0.08",                # A 级（普通对白/旁白）上限
    "asr_cer_b": "0.18",                # B 级（群嘲/背景）上限
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
    global HIAGENT_API_KEY, OPENROUTER_API_KEY, BAILIAN_API_KEY
    HIAGENT_API_KEY = os.environ.get("HIAGENT_API_KEY", "")
    OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
    BAILIAN_API_KEY = os.environ.get("BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", "")


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
        else:
            label = provider
        result[provider] = {
            "key_name": key_name,
            "label": label,
            "configured": bool(val),
            "preview": f"{val[:6]}...{val[-4:]}" if len(val) > 10 else ("已配置" if val else ""),
        }
    return result
