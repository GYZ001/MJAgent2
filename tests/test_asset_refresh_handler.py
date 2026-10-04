"""``video.asset_refresh_regenerate`` Handler：部分段落被闸门拦住时不能把
已经排队成功的段也判成整体失败——否则 ``raise_if_failed`` 在 ``data`` 没有
``code`` 键时只回传一句纯文本 ``summary``，前端按 ``result.errors`` 渲染逐段
详情、按 ``result.queued`` 刷新已成功段落的逻辑永远执行不到（2026-10-03 复
现，见 ``app/capabilities/handlers/video_asset_refresh.py``）。
"""
from __future__ import annotations

import app.domain.video_ops.asset_refresh as asset_refresh_module
from app.capabilities.handlers.video_asset_refresh import asset_refresh_regenerate
from app.capabilities.inputs_asset_refresh import VideoAssetRefreshRegenerateInput
from app.capabilities.schemas import CommandStatus


def _input(**overrides) -> VideoAssetRefreshRegenerateInput:
    base = {"episode_id": "e", "idempotency_key": "batch-1"}
    base.update(overrides)
    return VideoAssetRefreshRegenerateInput(**base)


async def test_regenerate_handler_full_block_still_reports_structured_errors(monkeypatch) -> None:
    """一段都没排上（全部被闸门拦住）：仍判 FAILED，但 data 必须带着
    ``code`` 字段，否则 ``raise_if_failed`` 不会把 queued/errors 结构塞进
    HTTP detail，前端会只收到一句摘要文本。"""
    async def fake_core(episode_id, body):
        return {
            "episode_id": episode_id, "queued": [],
            "errors": [{"shot_id": "s2", "status_code": 409, "detail": "场景状态图未就绪"}],
            "message": "0 段提交重生成，1 段被拦住",
        }

    monkeypatch.setattr(asset_refresh_module, "_asset_refresh_regenerate_core", fake_core)
    result = await asset_refresh_regenerate(_input())
    assert result.status == CommandStatus.FAILED
    assert result.data.get("code")
    assert result.data["errors"] == [{"shot_id": "s2", "status_code": 409, "detail": "场景状态图未就绪"}]
    assert result.data["queued"] == []


async def test_regenerate_handler_partial_success_returns_succeeded_with_errors(monkeypatch) -> None:
    """一部分段排队成功、一部分被拦住：必须是 SUCCEEDED（200），errors 原样
    带在 data 里供前端展示"段 X 被拦住"，不能把已排队成功的那部分也判失败。"""
    async def fake_core(episode_id, body):
        return {
            "episode_id": episode_id, "queued": ["s1"],
            "errors": [{"shot_id": "s2", "status_code": 409, "detail": "道具卡待补"}],
            "message": "已为 1 段提交重生成，1 段被拦住",
        }

    monkeypatch.setattr(asset_refresh_module, "_asset_refresh_regenerate_core", fake_core)
    result = await asset_refresh_regenerate(_input())
    assert result.status == CommandStatus.SUCCEEDED
    assert result.data["queued"] == ["s1"]
    assert result.data["errors"] == [{"shot_id": "s2", "status_code": 409, "detail": "道具卡待补"}]


async def test_regenerate_handler_full_success_has_no_errors(monkeypatch) -> None:
    """既有行为不能回归：全部排队成功时仍是 SUCCEEDED、errors 为空。"""
    async def fake_core(episode_id, body):
        return {"episode_id": episode_id, "queued": ["s1", "s2"], "errors": [], "message": "已为 2 段提交重生成"}

    monkeypatch.setattr(asset_refresh_module, "_asset_refresh_regenerate_core", fake_core)
    result = await asset_refresh_regenerate(_input())
    assert result.status == CommandStatus.SUCCEEDED
    assert result.data["queued"] == ["s1", "s2"]
    assert result.data["errors"] == []
