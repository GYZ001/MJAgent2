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
        requires_human_gate=False,
    ),
    "scene_bible": StageContract(
        key="scene_bible",
        version="1.0.0",
        input_types=["chapter_set", "character_bible"],
        output_type="scene_bible",
        invariants=["scene names are unique", "visual anchors contain no characters"],
        max_iterations=4,
        requires_human_gate=False,
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
        requires_human_gate=False,
    ),
    "screenplay": StageContract(
        key="screenplay",
        version="6.0.0",
        input_types=["novel_source", "episode_mapping", "character_portraits", "scene_references"],
        output_type="episode_prep_pack",
        invariants=[
            "every indexed source segment is covered by the event chain as delivered, merged, "
            "retained as context, or linked to a proven duplicate; any uncovered segment blocks publish",
            "every event carries source evidence as an explicit segment_index paired with a quote "
            "that deterministically aligns to that segment's text",
            "every character or scene appearing in the event chain resolves to an existing "
            "portrait_id or scene_reference_id, or to a deterministic generic-extra class",
            "the episode hook is non-empty and grounds to one or more events in the event chain",
            "episode scope is taken from the deterministic episode_mapping chapter assignment; "
            "no model call determines episode scope",
        ],
        max_iterations=2,
        requires_human_gate=False,
    ),
    "storyboard": StageContract(
        key="storyboard",
        # 5.0.0: input switched from episode_screenplay to episode_prep_pack
        # (docs/TRANSFORM_FREEZE_PLAN.md P1 -- screenplay stage stopped
        # producing episode_screenplay at contract 6.0.0; storyboard's
        # declared input must follow its actual producer). Same precedent as
        # the screenplay contract's own 5.1.0 -> 6.0.0 bump for its output
        # type change: a breaking change to a declared input/output type is a
        # major version bump, not a patch. The storyboard stage itself is
        # unchanged this round -- it still consumes the legacy
        # EpisodeScreenplay shape, now populated by a deterministic
        # projection (app.production.screenplay_authority.
        # project_prep_pack_to_screenplay) instead of the retired heavy
        # blueprint pipeline.
        version="5.0.0",
        input_types=["episode_prep_pack"],
        output_type="storyboard",
        invariants=[
            "the model selects each shot duration as an integer from 5 through 15 seconds",
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
        requires_human_gate=False,
    ),
    "video": StageContract(
        key="video",
        version="2.0.0",
        input_types=["storyboard", "compiled_prompt", "shot_reference_set"],
        output_type="shot_video",
        invariants=[
            "video is decodable",
            "duration approximately matches the storyboard-selected value between 5 and 15 seconds",
        ],
        max_iterations=2,
        requires_human_gate=False,
    ),
}


def get_contract(key: str) -> StageContract:
    try:
        return _CONTRACTS[key].model_copy(deep=True)
    except KeyError as exc:
        raise KeyError(f"unknown stage contract: {key}") from exc
