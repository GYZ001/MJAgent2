"""单镜生成必须校验参考图已就绪——出图解耦到后台之后新增的安全网。"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.domain.video_ops import confirmation_gate as cg


class _Conn:
    def execute(self, *_a, **_k):
        return self

    def fetchall(self):
        return [{"id": "s1"}]


def _stub_episode_lookup(monkeypatch) -> None:
    monkeypatch.setattr(cg, "get_conn", lambda: _Conn())
    monkeypatch.setattr(
        cg, "_episode_or_404", lambda *_a, **_k: {"id": "e", "episode_no": 1,
                                                 "project_id": "p"},
    )


def test_shot_gate_blocks_when_assets_missing(monkeypatch) -> None:
    """缺图时 409，并且给出等后台或手动补图两条出路。"""
    _stub_episode_lookup(monkeypatch)
    monkeypatch.setattr(cg, "_assert_storyboard_generation_gate", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cg, "_assert_episode_reference_assets_ready",
        lambda *_a, **_k: (_ for _ in ()).throw(HTTPException(409, {
            "code": "REFERENCE_ASSETS_NOT_READY",
            "message": "人物「许师姐」的参考图还没生成好；图片正在后台生成，"
                       "稍等片刻再试；也可以在人物谱/场景库手动上传。",
            "recovery_action": "等待后台出图完成，或在人物谱/场景库手动补图",
            "episode_id": "e",
        })),
    )

    with pytest.raises(HTTPException) as excinfo:
        cg._assert_shot_generation_gate("e")

    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert detail["code"] == "REFERENCE_ASSETS_NOT_READY"
    assert "后台" in detail["message"] and "手动上传" in detail["message"]


def test_shot_gate_runs_the_shared_gate_first(monkeypatch) -> None:
    """公共闸门先跑：它不过就没必要再扫素材，也不该把它的结论盖掉。"""
    order: list[str] = []
    monkeypatch.setattr(
        cg, "_assert_storyboard_generation_gate",
        lambda *_a, **_k: (_ for _ in ()).throw(HTTPException(409, "本集尚无分镜")),
    )
    monkeypatch.setattr(
        cg, "_assert_episode_reference_assets_ready",
        lambda *_a, **_k: order.append("assets"),
    )

    with pytest.raises(HTTPException) as excinfo:
        cg._assert_shot_generation_gate("e")

    assert excinfo.value.detail == "本集尚无分镜"
    assert order == []


def test_assert_episode_reference_assets_ready_passes_project_bible(monkeypatch) -> None:
    """判断"要不要参考图"现在挂人物谱/场景库有没有这张卡（见 app.multiview.
    _storyboard_pack_asset_dependencies），分镜包段落分支的这条判据必须查
    真实 bible——漏传 bible 会在那里当场炸 ValueError，比放行更糟（回归锁：
    app.domain.video_ops.confirmation_gate._assert_episode_reference_assets_
    ready 曾经不传 bible 给 scan_episode_reference_asset_gaps，靠"分镜包分支
    从不读 bible"这个已经被本次改动打破的旧前提侥幸没炸）。这里锁死它真的
    把本集项目的 bible 传下去了，不是留着默认的 None。
    """
    import app.multiview as multiview_module
    import app.domain.storyboard_ops as storyboard_ops_pkg
    from app import db
    from app.domain.video_ops import confirmation_gate as cg
    from app.schemas import Bible, Character, World

    conn = db.get_conn()
    project_id = "proj-shot-gate-bible"
    bible = Bible(
        characters=[Character(name="许师姐", role="配角", appearance_canonical="许师姐，淡蓝衣衫")],
        world=World(visual_style_canonical="写实"),
    )
    conn.execute(
        "INSERT INTO projects(id,name,bible_json,created_at) VALUES(?,?,?,?)",
        (project_id, "shot gate fixture", bible.model_dump_json(), db.now()),
    )
    conn.commit()
    episode = {"id": "ep-1", "episode_no": 1, "project_id": project_id}
    rows = [{"id": "shot-1"}]

    monkeypatch.setattr(
        storyboard_ops_pkg, "_board_from_shot_rows",
        lambda *_a, **_k: type("_Board", (), {"shots": []})(),
    )
    captured: dict = {}

    def _fake_scan(**kwargs):
        captured.update(kwargs)
        return {"characters": [], "scenes": [], "blockers": []}

    monkeypatch.setattr(multiview_module, "scan_episode_reference_asset_gaps", _fake_scan)

    cg._assert_episode_reference_assets_ready(conn, episode, rows)

    assert captured.get("bible") is not None
    assert [c.name for c in captured["bible"].characters] == ["许师姐"]


def test_shared_gate_itself_is_unchanged_for_the_other_entrances() -> None:
    """公共闸门被四个付费入口共用，其中"补齐到全片"会自己先补素材——
    素材校验不能加进公共部分，否则把整集入口 409 时指的出路一起堵死。"""
    import inspect

    source = inspect.getsource(cg._assert_storyboard_generation_gate)

    assert "_assert_episode_reference_assets_ready" not in source
