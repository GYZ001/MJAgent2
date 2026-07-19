"""Fail CI when frozen product contracts drift across active surfaces."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "app/config.py": ["FIXED_VIDEO_DURATION_S = 5"],
    "app/planning.py": ["one episode", "without an LLM"],
    "app/harness/contracts.py": ["one chapter maps to exactly one episode", "exactly 5 seconds"],
    "docs/PROMPT_SPEC.md": ["确定性剧集映射（非 Agent 阶段）", "duration_s 全部为 5"],
}
FORBIDDEN = {
    "app/stages.py": ["hiagent.chat", "_run_with_repair", "滚动摘要", "duration_s 全部为 10"],
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
    print("Contract surface OK: one chapter/episode, fixed 5s, AgentLoop-only text stages.")


if __name__ == "__main__":
    main()
