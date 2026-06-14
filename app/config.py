"""全局配置：.env 加载 + 运行参数。禁止在代码中出现任何密钥字面量。"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT / "projects"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "manju.db"


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
