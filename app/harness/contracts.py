from __future__ import annotations

from app.harness.types import StageContract


_CONTRACTS: dict[str, StageContract] = {
    "character_bible": StageContract(
        key="character_bible",
        version="1.0.0",
        input_types=["chapter_set"],
        output_type="character_bible",
        invariants=["character names are source traceable", "visual anchors are non-empty"],
        max_iterations=4,
        requires_human_gate=True,
    ),
    "scene_bible": StageContract(
        key="scene_bible",
        version="1.0.0",
        input_types=["chapter_set", "character_bible"],
        output_type="scene_bible",
        invariants=["scene names are unique", "visual anchors contain no characters"],
        max_iterations=4,
    ),
    "episode_mapping": StageContract(
        key="episode_mapping",
        version="1.0.0",
        input_types=["chapter_set"],
        output_type="episode_mapping",
        invariants=[
            "one chapter maps to exactly one episode",
            "episode numbers are contiguous",
            "no model call is allowed",
        ],
    ),
    "screenplay": StageContract(
        key="screenplay",
        version="1.0.0",
        input_types=["novel_source", "character_bible", "episode_mapping"],
        output_type="episode_screenplay",
        invariants=["blockers prevent ready status", "source claims have evidence"],
        max_iterations=4,
        requires_human_gate=True,
    ),
    "storyboard": StageContract(
        key="storyboard",
        version="2.1.1",
        input_types=["episode_screenplay"],
        output_type="storyboard",
        invariants=[
            "the model selects each shot duration as an integer from 5 through 10 seconds",
            "spoken-content budget scales with the selected shot duration",
            "shot numbers are contiguous",
            "sequential generation emits exactly one singular shot per iteration",
            "single-shot output tokens are bounded independently from full-screenplay generation",
            "functional extras use deterministic generic labels and never mint persistent bible identities",
        ],
        max_iterations=4,
    ),
    "video": StageContract(
        key="video",
        version="2.0.0",
        input_types=["storyboard", "compiled_prompt", "shot_reference_set"],
        output_type="shot_video",
        invariants=[
            "video is decodable",
            "duration approximately matches the storyboard-selected value between 5 and 10 seconds",
        ],
        max_iterations=2,
    ),
}


def get_contract(key: str) -> StageContract:
    try:
        return _CONTRACTS[key].model_copy(deep=True)
    except KeyError as exc:
        raise KeyError(f"unknown stage contract: {key}") from exc
