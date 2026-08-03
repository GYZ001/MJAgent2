import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app import db
from app.continuity import (
    apply_shot_contract,
    shot_contract_dict,
    structured_boundary_issues,
    sync_shot_continuity_fields,
)
from app.final_edit import (
    _font_path,
    boundary_report,
    render_episode_final_edit,
    render_text_card,
    transition_spec,
)
from app.schemas import ContinuityState, RequiredOnScreenText, Shot


def _shot(shot_no: int, **changes) -> Shot:
    payload = {
        "shot_no": shot_no,
        "duration_s": 5,
        "shot_size": "中景",
        "camera_move": "固定",
        "scene_name": "山门",
        "scene_setting": "白日，山门",
        "characters": ["林风"],
        "action_desc": "林风站在山门前握住令牌。",
        "first_frame_desc": "林风站在山门前，右手握住令牌。",
        "last_frame_desc": "林风仍站在山门前，右手握住令牌。",
        "source_excerpt": "林风握住令牌站在山门前，望向门内的石阶。",
        "continuity_mode": "same_scene_cut",
    }
    payload.update(changes)
    return Shot(**payload)


def _state(*, prop_form: str = "完整", axis: str = "axis-main") -> ContinuityState:
    return ContinuityState.model_validate({
        "scene": {
            "scene_revision_id": "scene-gate-v1",
            "time_of_day": "day",
            "lighting_state": "warm-side",
            "axis_id": axis,
            "landmarks": {"gate": "background-center"},
        },
        "characters": {
            "林风": {
                "look_revision_id": "lin-v1",
                "outfit_revision_id": "robe-v1",
                "screen_side": "left",
                "right_hand": "prop-token",
            },
        },
        "props": {
            "prop-token": {
                "canonical_name": "青铜令牌",
                "revision_id": "token-v1",
                "owner": "林风",
                "location": "right-hand",
                "form": prop_form,
                "visibility": "required",
                "required": True,
            },
        },
    })


def test_structured_state_roundtrip_and_inheritance_only_changes_explicit_fields() -> None:
    previous = _shot(1, continuity_state_out=_state())
    current = _shot(
        2,
        continuity_state_out=ContinuityState.model_validate({
            "props": {"prop-token": {"form": "两半"}},
        }),
    )

    sync_shot_continuity_fields(current, previous)

    assert current.continuity_state_in.props["prop-token"].revision_id == "token-v1"
    assert current.continuity_state_in.props["prop-token"].form == "完整"
    assert current.continuity_state_out.props["prop-token"].form == "两半"
    assert current.continuity_state_out.props["prop-token"].owner == "林风"
    assert structured_boundary_issues(previous, current) == []

    restored = _shot(2)
    apply_shot_contract(restored, shot_contract_dict(current))
    assert restored.continuity_state_out == current.continuity_state_out


def test_structured_boundary_reports_prop_identity_state_and_axis_without_blocking() -> None:
    previous = _shot(1, continuity_state_out=_state())
    changed = _state(prop_form="两半", axis="axis-reversed")
    changed.props["prop-token"].revision_id = "token-v2"
    current = _shot(2, continuity_state_in=changed)

    issues = structured_boundary_issues(previous, current)
    codes = {item["code"] for item in issues}

    assert {"BOUNDARY_CAMERA_AXIS", "BOUNDARY_PROP_IDENTITY", "BOUNDARY_PROP_FORM"} <= codes
    assert all(item["runtime_blocking"] is False for item in issues)


def test_scene_change_keeps_identity_but_drops_old_composition_geometry() -> None:
    previous = _shot(1, continuity_state_out=_state())
    current = _shot(
        2,
        scene_name="内殿",
        scene_setting="夜，内殿",
        continuity_mode="scene_change",
    )

    sync_shot_continuity_fields(current, previous)

    character = current.continuity_state_in.characters["林风"]
    assert character.look_revision_id == "lin-v1"
    assert character.outfit_revision_id == "robe-v1"
    assert character.right_hand == "prop-token"
    assert character.screen_side == ""
    assert character.pose == ""
    assert not any(current.continuity_state_in.scene.model_dump().values())


def test_scene_only_boundary_state_is_verified_and_scene_change_is_not_false_positive() -> None:
    previous = _shot(1, continuity_state_out=_state())
    changed = _state()
    changed.scene.scene_revision_id = "scene-hall-v1"
    current = _shot(
        2,
        scene_name="内殿",
        scene_setting="夜，内殿",
        continuity_mode="",
        continuity_state_in=ContinuityState(scene=changed.scene),
    )

    codes = {item["code"] for item in structured_boundary_issues(previous, current)}
    assert "BOUNDARY_SCENE_REVISION" not in codes
    assert "BOUNDARY_TIME_OF_DAY" not in codes
    report = boundary_report([
        _shot(1, continuity_state_out=ContinuityState(scene=_state().scene)),
        _shot(2, continuity_state_in=ContinuityState(scene=_state().scene)),
    ])
    assert report["unverified_boundaries"] == []


def test_reference_transition_mapping_uses_short_bounded_overlaps() -> None:
    assert transition_spec("声音延续+叠化").edit_type == "dissolve"
    assert transition_spec("闪白").ffmpeg_name == "fadewhite"
    assert transition_spec("硬切").duration_s <= 0.12


def test_deterministic_cjk_text_card_has_fixed_dimensions(tmp_path: Path) -> None:
    try:
        _font_path()
    except RuntimeError:
        pytest.skip("test host has no configured CJK font")
    destination = tmp_path / "text.png"

    report = render_text_card("天门已开", "青铜古碑", destination)

    with Image.open(destination) as image:
        assert image.size == (720, 1280)
        assert image.format == "PNG"
    assert report["exact_text"] == "天门已开"
    assert len(report["sha256"]) == 64


def _database_with_shots(shots: list[Shot]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for statement in db.MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p','P',0)")
    conn.execute(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) "
        "VALUES('e','p',1,'E','confirmed',0)"
    )
    for shot in shots:
        conn.execute(
            """INSERT INTO shots(
                   id,episode_id,shot_no,duration_s,shot_size,camera_move,scene_setting,
                   scene_name,characters,action_desc,source_excerpt,dialogues,transition,
                   continuity_from_prev,continuity_mode,shot_contract_json
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"s{shot.shot_no}", "e", shot.shot_no, shot.duration_s, shot.shot_size,
                shot.camera_move, shot.scene_setting, shot.scene_name,
                json.dumps(shot.characters, ensure_ascii=False), shot.action_desc,
                shot.source_excerpt, "[]", shot.transition, int(shot.continuity_from_prev),
                shot.continuity_mode, json.dumps(shot_contract_dict(shot), ensure_ascii=False),
            ),
        )
    conn.commit()
    return conn


def _source_clip(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=180x320:r=24:d=0.9",
            "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t", "0.9", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg unavailable")
def test_final_edit_smoke_renders_text_and_uses_incoming_transition(tmp_path: Path) -> None:
    try:
        _font_path()
    except RuntimeError:
        pytest.skip("test host has no configured CJK font")
    first = _shot(
        1,
        continuity_state_out=_state(),
        required_text=RequiredOnScreenText(
            surface="令牌特写",
            exact_text="天门已开",
            delivery_owner_shot_no=1,
            appear_start_s=0.1,
            stable_until_s=0.7,
        ),
    )
    second = _shot(
        2,
        transition="闪白",
        continuity_state_in=_state(),
        continuity_state_out=_state(),
    )
    conn = _database_with_shots([first, second])
    one, two = tmp_path / "one.mp4", tmp_path / "two.mp4"
    _source_clip(one, "red")
    _source_clip(two, "blue")
    destination = tmp_path / "final.mp4"

    report = render_episode_final_edit(
        conn,
        "e",
        [(1, str(one), 1.0), (2, str(two), 1.0)],
        destination,
        tmp_path / "work",
    )

    assert destination.is_file() and destination.stat().st_size > 0
    assert report["ok"] is True
    assert report["text_inserts"][0]["exact_text"] == "天门已开"
    assert report["transitions"][0]["edit_type"] == "dip_white"
    assert report["transitions"][0]["from_shot_no"] == 1
    assert report["transitions"][0]["to_shot_no"] == 2
    assert report["boundary_report"]["runtime_blocking"] is False
