"""Fail CI when frozen product contracts drift across active surfaces."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "app/config.py": [
        "VIDEO_DURATION_MIN_S = 5",
        "VIDEO_DURATION_MAX_S = 10",
        "STORYBOARD_SHOT_MAX_TOKENS",
        "TEXT_PROVIDER_MAX_RETRIES",
    ],
    "app/planning.py": ["one episode", "without an LLM"],
    "app/harness/contracts.py": [
        "one chapter maps to exactly one episode",
        "the model selects each shot duration as an integer from 5 through 10 seconds",
        "sequential generation emits exactly one singular shot per iteration",
        "single-shot output tokens are bounded independently",
        "functional extras use deterministic generic labels",
    ],
    "app/compiler.py": ["shot.duration_s not in config.ALLOWED_DURATIONS", "--dur {shot_dur}"],
    "app/validators.py": [
        "shot.duration_s not in config.ALLOWED_DURATIONS",
        "max_spoken_chars_for_duration(shot.duration_s)",
    ],
    "app/stages.py": [
        "duration_s 必须由你根据",
        "选择能完整、自然呈现本镜内容的最短时长",
        "禁止输出 shots 数组",
        "功能性路人合同",
        "max_tokens=config.STORYBOARD_SHOT_MAX_TOKENS",
    ],
    "docs/PROMPT_SPEC.md": [
        "确定性剧集映射（非 Agent 阶段）",
        "duration_s 全部为 5~10 秒整数",
        "只允许单数 `shot`",
        "功能性路人合同",
        "单镜输出 token 上限",
    ],
}
FORBIDDEN = {
    "app/stages.py": [
        "hiagent.chat", "_run_with_repair", "滚动摘要", "duration_s 全部为 10",
        "duration_s 固定为 5", "固定 5 秒视频分镜",
    ],
    "app/auto.py": ["_states"],
    "app/portraits.py": ["hiagent.chat"],
    "app/scenes.py": ["hiagent.chat"],
    "app/video_modes.py": ["hiagent.chat"],
    "docs/PROMPT_SPEC.md": [
        "duration_s 全部为 10", "自动生成总时长不超过 90s", "单镜台词+旁白总口播必须在 15s",
        "本次只规划前", "target_duration_s ∈", "正好 N 拍", "每集 40~90 秒",
    ],
}


def main() -> None:
    errors: list[str] = []
    for relative, tokens in REQUIRED.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        errors.extend(f"{relative}: missing required contract {token!r}" for token in tokens if token not in text)
    for relative, tokens in FORBIDDEN.items():
        text = (ROOT / relative).read_text(encoding="utf-8")
        errors.extend(f"{relative}: legacy contract found {token!r}" for token in tokens if token in text)
    if errors:
        raise SystemExit("Contract surface drift:\n- " + "\n- ".join(errors))
    print("Contract surface OK: one chapter/episode, model-selected 5-10s shots, AgentLoop-only text stages.")


if __name__ == "__main__":
    main()
