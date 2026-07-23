"""旧关键帧分支已下线：所有可用入口必须收敛到参考图视频链路。"""
import asyncio
import json
import sqlite3

import pytest

from app import api, worker


def test_keyframe_http_routes_are_removed() -> None:
    paths = {route.path for route in api.router.routes}

    assert "/api/shots/{shot_id}/scene" not in paths
    assert "/api/shots/{shot_id}/scene/approve" not in paths
    assert "/api/scenes/{scene_id}" not in paths
    assert "/api/episodes/{episode_id}/scenes-all" not in paths


def test_legacy_keyframe_enqueue_is_rejected_with_video_guidance() -> None:
    with pytest.raises(ValueError, match="参考图视频入口"):
        worker.enqueue_scene("legacy-shot")


def test_legacy_mode_plan_is_upgraded_before_video_generation() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE shots(id TEXT PRIMARY KEY, mode_plan TEXT)")
    conn.execute(
        "INSERT INTO shots(id, mode_plan) VALUES(?, ?)",
        ("shot-8", json.dumps({"mode": "FIRST_LAST_FRAME_MODE"})),
    )

    asyncio.run(api._ensure_shot_mode_plan(conn, "shot-8"))

    stored = json.loads(
        conn.execute("SELECT mode_plan FROM shots WHERE id='shot-8'").fetchone()["mode_plan"]
    )
    assert stored["mode"] == "REFERENCE_IMAGE_MODE"
