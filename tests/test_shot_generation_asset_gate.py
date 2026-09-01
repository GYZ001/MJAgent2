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


def test_shared_gate_itself_is_unchanged_for_the_other_entrances() -> None:
    """公共闸门被四个付费入口共用，其中"补齐到全片"会自己先补素材——
    素材校验不能加进公共部分，否则把整集入口 409 时指的出路一起堵死。"""
    import inspect

    source = inspect.getsource(cg._assert_storyboard_generation_gate)

    assert "_assert_episode_reference_assets_ready" not in source
