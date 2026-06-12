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
    "use_first_frame_chaining": "true",  # 同场景连续镜头用上一镜尾帧作首帧（连贯性核心）
    "max_ref_images": "2",            # 单镜头最多附几张定妆照
    "auto_qa": "true",
    "auto_retake_threshold": "0.6",
    "plan_episode_count": "12",  # 分集"每批"集数（分批续写直至铺满全书，非总集数上限）
    "max_repair_attempts": "8",  # LLM 输出校验失败的最大修复重试次数（含首次）；模型不可用不走此重试
}

PROJECTS_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)
