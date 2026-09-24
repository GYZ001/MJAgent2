"""角色固定音色 U3：MiniMax H3 未接入 ``reference_audio``，收到非空
``audio_urls`` 必须在创建供应商任务前明确拒绝（不静默丢弃、不发起真实调用）。

正常流程里这种情况不会发生——``app.voice.segment_refs.resolve_segment_
reference_audios`` 在解析阶段就已经按能力快照（H3 的 ``supports_reference_
audio=False``）把所有说话人判定为跳过，``reference_audios`` 恒为空列表。这
里单独验证适配器自己的兜底：即便上游哪天出错传了非空列表，H3 也不会把它
悄悄吞掉或转发给供应商。
"""
from __future__ import annotations

import asyncio

import pytest

from app.hiagent import ProviderError
from app.minimax_h3 import MiniMaxH3Adapter


def test_nonempty_audio_urls_raises_before_any_provider_call() -> None:
    adapter = MiniMaxH3Adapter()

    with pytest.raises(ProviderError, match="MiniMax H3 未接入 reference_audio"):
        asyncio.run(
            adapter.create_video_task(
                "镜头1：固定远景。",
                image_urls=[("https://img.example.test/a.jpg", "reference_image")],
                audio_urls=[("data:audio/wav;base64,AAAA", "reference_audio")],
            )
        )


def test_empty_audio_urls_does_not_raise_the_audio_guard(monkeypatch) -> None:
    """空/未传 audio_urls 时不触发音频拒绝分支，走既有的图片提交路径。"""
    from app import minimax_h3 as minimax_h3_module

    async def fake_create_video_task(prompt_text, *, image_urls=None, video_urls=None, call_meta=None,
                                      connection=None):
        return "h3-task-1"

    monkeypatch.setattr(minimax_h3_module, "create_video_task", fake_create_video_task)
    adapter = MiniMaxH3Adapter()

    task_id = asyncio.run(
        adapter.create_video_task(
            "镜头1：固定远景。",
            image_urls=[("https://img.example.test/a.jpg", "reference_image")],
            audio_urls=None,
        )
    )

    assert task_id == "h3-task-1"
