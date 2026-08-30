"""Fail CI when frozen product contracts drift across active surfaces."""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = {
    "app/config.py": [
        "VIDEO_DURATION_MIN_S = 5",
        "VIDEO_DURATION_MAX_S = 15",
        "STORYBOARD_SHOT_MAX_TOKENS",
        "TEXT_PROVIDER_MAX_RETRIES",
    ],
    "app/planning.py": ["one episode", "without an LLM"],
    "app/harness/contracts.py": [
        "one chapter maps to exactly one episode",
        "the model selects each shot duration as an integer from 5 through 15 seconds",
        "shot count has no product ceiling",
        "detailed shots are generated and checkpointed as complete scene packs",
        "every shot has a motivated shot-size, camera-angle, and camera-movement triple",
        "functional extras use deterministic generic labels",
    ],
    "app/compiler.py": ["shot.duration_s not in config.ALLOWED_DURATIONS", "--dur {shot_dur}"],
    # app/validators.py (6,289 lines) was split into the app.validators package
    # on 2026-08-29 (docs/coupling_review_2026-08-29.md C5); these two tokens
    # moved with the code that carries them, not deleted.
    "app/validators/storyboard_core.py": [
        "shot.duration_s not in config.ALLOWED_DURATIONS",
        "spoken_chars_from_shot(shot)",
    ],
    "app/validators/outline_validate.py": [
        # camera_motivation/context_requirement_ids used to be guarded via
        # app/stages.py's narrative_plan-driven per-shot generation contract
        # (generate_storyboard_outline/_next_shot). That pipeline was deleted
        # once storyboard 2.0.0 (app/production/storyboard_pack.py) became
        # the only path prep_pack episodes ever reach -- see
        # app/storyboard_supervisor.py::run_storyboard_supervisor's
        # unconditional early return. Both fields still gate real, live
        # narrative-authority shot validation here, so the surface check
        # moved with them instead of just disappearing.
        "camera_motivation",
        "context_requirement_ids",
    ],
    "app/production/storyboard_pack.py": [
        # storyboard 2.0.0's generation entry point, replacing the deleted
        # app/stages.py::generate_storyboard_scene_pack for prep_pack
        # episodes (docs/STORYBOARD_PROMPT_IR_DESIGN.md).
        "async def generate_storyboard_pack(",
        # Segment count is decided by the model's own beat/narrative-unit
        # judgement, not mechanically capped or divided -- the 2.0.0
        # equivalent of the old "镜头数量不设软上限或硬上限" invariant
        # (also guarded in English as "shot count has no product ceiling"
        # in app/harness/contracts.py's storyboard contract).
        "段落数量由节拍的叙事单元数量决定，不是按原文段数或时长机械平分",
    ],
    "docs/PROMPT_SPEC.md": [
        "确定性剧集映射（非 Agent 阶段）",
        "duration_s 全部为 5~15 秒整数",
        "镜头数量不设产品上限",
        "按场景批量生成",
        "景别 + 角度 + 运动",
        "上下文建立窗口",
    ],
}
FORBIDDEN = {
    # app/stages.py (single file) became the app.stages package on 2026-08-29;
    # a directory pathspec is scanned file-by-file (see _target_files), so a
    # future re-split inside app/stages doesn't require touching this dict.
    "app/stages": [
        "hiagent.chat", "_run_with_repair", "滚动摘要", "duration_s 全部为 10",
        "duration_s 固定为 5", "固定 5 秒视频分镜",
    ],
    # app/auto.py was removed in the slimdown; keep the legacy token listed only if the
    # file reappears so CI fails when old auto-pipeline state machines return.
    # app/portraits.py became the app.portraits package on 2026-08-29; same
    # directory-scan treatment as app/stages above.
    "app/portraits": ["hiagent.chat"],
    "app/scenes.py": ["hiagent.chat"],
    # app/video_modes.py (4,081 lines) was split into the app.video_modes package
    # on 2026-08-29; the forbidden token must follow every submodule the body
    # moved into, or a deleted single path would silently stop being checked
    # (see main()'s `if not path.exists(): continue` for FORBIDDEN entries).
    "app/video_modes/__init__.py": ["hiagent.chat"],
    "app/video_modes/mode_selection.py": ["hiagent.chat"],
    "app/video_modes/asset_lookup.py": ["hiagent.chat"],
    "app/video_modes/keyframe_contract.py": ["hiagent.chat"],
    "app/video_modes/reference_prompt.py": ["hiagent.chat"],
    "app/video_modes/reference_generate.py": ["hiagent.chat"],
    "app/video_modes/reference_assemble.py": ["hiagent.chat"],
    "app/video_modes/reference_generate_legacy.py": ["hiagent.chat"],
    "app/video_modes/continuity_tail.py": ["hiagent.chat"],
    "app/video_modes/seedance_pack.py": ["hiagent.chat"],
    "docs/PROMPT_SPEC.md": [
        "duration_s 全部为 10", "自动生成总时长不超过 90s", "单镜台词+旁白总口播必须在 15s",
        "本次只规划前", "target_duration_s ∈", "正好 N 拍", "每集 40~90 秒",
    ],
}

# --- Version-bump discipline (WARN-only, never SystemExit) -----------------
#
# RCA (2026-08-23): three consecutive fix commits (77481f8, e40978a, 975fa3a)
# changed the screenplay narrative-blueprint/envelope/scene-shard generation
# prompts and validation logic (including a brand-new ending_hook_is_grounded
# gate) without bumping any of the version constants that decide whether a
# historical artifact may be silently reused instead of regenerated. Old,
# unaudited artifacts kept being served after the fix shipped.
#
# This check is a coarse, text-diff-based approximation: it cannot tell
# "changed behavior" apart from "changed a comment/docstring/log message", so
# it only flags a *candidate* miss and never fails the build by itself. Treat
# a hit as "go re-check whether the matching version constant needs to move",
# not as proof that it does.
#
# Known false positives: any non-comment line change in a guard file trips
# this, including pure refactors, log-message rewording inside an f-string,
# renamed local variables, or added type hints -- none of which change what
# gets accepted/produced. Expect noise on unrelated refactors of these files.
#
# Known false negatives: (1) this only looks at the *working tree vs HEAD*
# diff (mirroring scripts/verify.py's own change-detection), so it cannot
# catch drift that was already committed -- exactly how the original bug
# shipped across three commits with nobody re-running this check in between;
# it only helps if run before each commit. (2) a guard file is cleared as
# soon as *any* of its paired anchors moves, even if the actual change only
# affects a narrower slice of that file's logic than the anchor implies (no
# attempt is made to correlate which lines changed to which anchor). (3) a
# brand-new (untracked-but-uncommitted) guard file is not diffed against
# anything, so its first-ever content is not checked.
REUSE_GUARD_ANCHORS: dict[str, list[tuple[str, list[str]]]] = {
    # Directory pathspecs below: app/stages.py, app/screenplay_scene_shards.py
    # and app/validators.py each became a package on 2026-08-29 (docs/
    # coupling_review_2026-08-29.md C5). `Path.exists()` and `git diff --
    # <path>` both work the same on a directory as on a single file, so
    # pointing the guard/anchor at the directory keeps tracking every
    # submodule without hardcoding one entry per file inside it.
    "app/stages": [
        # app/narrative_blueprint.py became the app.narrative_blueprint package on
        # 2026-08-29; both version constants now live in constants.py.
        ("app/narrative_blueprint/constants.py", ["BLUEPRINT_VERSION =", "BLUEPRINT_PROMPT_VERSION ="]),
        ("app/stages", ["SCREENPLAY_BASELINE_PROMPT_VERSION ="]),
    ],
    "app/screenplay_scene_shards": [
        (
            "app/screenplay_scene_shards",
            ["SCREENPLAY_ENVELOPE_VERSION =", "SCREENPLAY_SCENE_SHARD_VERSION ="],
        ),
    ],
    "app/validators": [
        ("app/harness/contracts.py", ['version="']),
        (
            "app/screenplay_scene_shards",
            ["SCREENPLAY_ENVELOPE_VERSION =", "SCREENPLAY_SCENE_SHARD_VERSION ="],
        ),
    ],
    "app/production/publish.py": [("app/harness/contracts.py", ['version="'])],
    "app/production/screenplay_document.py": [("app/harness/contracts.py", ['version="'])],
    # app/production/screenplay_repair.py became the
    # app.production.screenplay_repair package (its own directory pathspec,
    # same reasoning as app/stages and app/validators above).
    "app/production/screenplay_repair": [("app/harness/contracts.py", ['version="'])],
    "app/production/screenplay_authority.py": [("app/harness/contracts.py", ['version="'])],
}


def _diff_changed_lines(relative: str) -> list[str]:
    """Added/removed source lines for one file, working tree vs HEAD."""
    result = subprocess.run(
        ["git", "diff", "-U0", "--diff-filter=ACMR", "HEAD", "--", relative],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    changed: list[str] = []
    for line in result.stdout.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            changed.append(line[1:])
    return changed


def _is_substantive(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def check_version_bump_discipline() -> list[str]:
    """Warn (never fail) when a reuse-guard file changed without its anchor."""
    misses: list[str] = []
    for guard_file, anchors in REUSE_GUARD_ANCHORS.items():
        if not (ROOT / guard_file).exists():
            continue
        substantive = [line for line in _diff_changed_lines(guard_file) if _is_substantive(line)]
        if not substantive:
            continue
        anchor_moved = any(
            pattern in line
            for anchor_file, patterns in anchors
            for line in _diff_changed_lines(anchor_file)
            for pattern in patterns
        )
        if not anchor_moved:
            anchor_desc = "; ".join(
                f"{anchor_file}:{'/'.join(patterns)}" for anchor_file, patterns in anchors
            )
            misses.append(
                f"{guard_file} changed ({len(substantive)} line(s)) but none of its "
                f"reuse-guard version anchors moved ({anchor_desc})"
            )
    return misses


def _target_files(path: Path) -> list[Path]:
    """Files a REQUIRED/FORBIDDEN entry actually inspects.

    A dict key may name a single file or, for a module that has since been
    split into a package, that package's directory. ``Path.exists()`` and
    ``git diff -- <path>`` already treat those the same (see the
    REUSE_GUARD_ANCHORS comment above), so REQUIRED/FORBIDDEN follow suit:
    scan every ``*.py`` file inside the directory instead of hardcoding one
    dict entry per submodule, which is exactly the kind of enumeration that
    silently stops covering anything the next time a package is reshuffled.
    """
    if path.is_dir():
        return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
    return [path]


def main() -> None:
    errors: list[str] = []
    for relative, tokens in REQUIRED.items():
        path = ROOT / relative
        if not path.exists():
            errors.append(f"{relative}: required contract surface missing")
            continue
        combined_text = "\n".join(
            file.read_text(encoding="utf-8") for file in _target_files(path)
        )
        errors.extend(
            f"{relative}: missing required contract {token!r}"
            for token in tokens if token not in combined_text
        )
    for relative, tokens in FORBIDDEN.items():
        path = ROOT / relative
        if not path.exists():
            # Deleted modules cannot reintroduce forbidden legacy contracts.
            continue
        for file in _target_files(path):
            text = file.read_text(encoding="utf-8")
            file_relative = file.relative_to(ROOT).as_posix()
            errors.extend(
                f"{file_relative}: legacy contract found {token!r}"
                for token in tokens if token in text
            )
    if errors:
        raise SystemExit("Contract surface drift:\n- " + "\n- ".join(errors))
    print(
        "Contract surface OK: one chapter/episode, complete source coverage, "
        "unbounded scene-packed 5-10s shots, motivated camera triples."
    )
    bump_misses = check_version_bump_discipline()
    if bump_misses:
        print("\n" + "!" * 78)
        print("! POSSIBLE MISSING VERSION BUMP (warning only, not blocking the build)")
        print("! A file that gates whether old artifacts get silently reused changed,")
        print("! but its paired version constant did not move in this diff. If this")
        print("! changed generated prompt text or validation/acceptance logic, bump")
        print("! the matching constant so resume/repair on existing episodes cannot")
        print("! silently reuse artifacts produced by the old logic. If this is a")
        print("! comment/refactor-only change, this warning is a false positive --")
        print("! ignore it.")
        for miss in bump_misses:
            print(f"!   - {miss}")
        print("!" * 78)


if __name__ == "__main__":
    main()
