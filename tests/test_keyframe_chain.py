"""旧关键帧分支已下线：所有可用入口必须收敛到参考图视频链路。"""
from app import api


def test_keyframe_http_routes_are_removed() -> None:
    paths = {route.path for route in api.router.routes}

    assert "/api/shots/{shot_id}/scene" not in paths
    assert "/api/shots/{shot_id}/scene/approve" not in paths
    assert "/api/scenes/{scene_id}" not in paths
    assert "/api/episodes/{episode_id}/scenes-all" not in paths
