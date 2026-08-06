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
        "shot count has no product ceiling",
        "detailed shots are generated and checkpointed as complete scene packs",
        "every shot has a motivated shot-size, camera-angle, and camera-movement triple",
        "functional extras use deterministic generic labels",
    ],
    "app/compiler.py": ["shot.duration_s not in config.ALLOWED_DURATIONS", "--dur {shot_dur}"],
    "app/validators.py": [
        "shot.duration_s not in config.ALLOWED_DURATIONS",
        "spoken_chars_from_shot(shot)",
    ],
    "app/stages.py": [
        "镜头数量不设软上限或硬上限",
        "generate_storyboard_scene_pack",
        "camera_motivation",
        "context_requirement_ids",
    ],
    "docs/PROMPT_SPEC.md": [
        "确定性剧集映射（非 Agent 阶段）",
        "duration_s 全部为 5~10 秒整数",
        "镜头数量不设产品上限",
        "按场景批量生成",
        "景别 + 角度 + 运动",
        "上下文建立窗口",
    ],
}
FORBIDDEN = {
    "app/stages.py": [
        "hiagent.chat", "_run_with_repair", "滚动摘要", "duration_s 全部为 10",
        "duration_s 固定为 5", "固定 5 秒视频分镜",
    ],
    # app/auto.py was removed in the slimdown; keep the legacy token listed only if the
    # file reappears so CI fails when old auto-pipeline state machines return.
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
        path = ROOT / relative
        if not path.exists():
            errors.append(f"{relative}: required contract surface missing")
            continue
        text = path.read_text(encoding="utf-8")
        errors.extend(
            f"{relative}: missing required contract {token!r}"
            for token in tokens if token not in text
        )
    for relative, tokens in FORBIDDEN.items():
        path = ROOT / relative
        if not path.exists():
            # Deleted modules cannot reintroduce forbidden legacy contracts.
            continue
        text = path.read_text(encoding="utf-8")
        errors.extend(
            f"{relative}: legacy contract found {token!r}"
            for token in tokens if token in text
        )
    if errors:
        raise SystemExit("Contract surface drift:\n- " + "\n- ".join(errors))
    print(
        "Contract surface OK: one chapter/episode, complete source coverage, "
        "unbounded scene-packed 5-10s shots, motivated camera triples."
    )


if __name__ == "__main__":
    main()
