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
        version="3.0.0",
        input_types=["novel_source", "character_bible", "episode_mapping"],
        output_type="episode_screenplay",
        invariants=[
            "blockers prevent ready status",
            "source claims have evidence",
            "every key line is delivered by an explicit screenplay dialogue line",
            "key dialogue is preserved as an ordered context chain rather than isolated quotes",
            "the first adapted dialogue turn cites a semantically matching source utterance before key lines are derived",
            "every indexed source segment is delivered, merged, retained as context, or linked to a proven duplicate",
            "scene count and story beat count are determined by complete content rather than a duration ceiling",
            "every scene declares entry state, exit state, and context requirements",
            "only one bounded structural repair may follow the initial screenplay generation",
        ],
        max_iterations=2,
        requires_human_gate=True,
    ),
    "storyboard": StageContract(
        key="storyboard",
        version="4.0.0",
        input_types=["episode_screenplay"],
        output_type="storyboard",
        invariants=[
            "the model selects each shot duration as an integer from 5 through 10 seconds",
            "spoken-content budget scales with the selected shot duration",
            "shot numbers are contiguous",
            "shot count has no product ceiling and completion is defined by content and context delivery",
            "the episode director plan assigns every story and context requirement before detailed generation",
            "detailed shots are generated and checkpointed as complete scene packs",
            "independent scene packs may generate concurrently under a bounded provider gate",
            "functional extras use deterministic generic labels and never mint persistent bible identities",
            "effective visible characters must belong to the bible or a deterministic extra class",
            "every shot states its purpose, delivered requirements, and resulting change",
            "every shot has a motivated shot-size, camera-angle, and camera-movement triple",
            "action scenes include a spatially readable medium or wide moving shot",
            "emotion turns include a face-readable close or close-up stable shot",
            "scene context is established before dependent action",
            "intentional repetition declares the additional reaction, verification, viewpoint, or payoff",
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
