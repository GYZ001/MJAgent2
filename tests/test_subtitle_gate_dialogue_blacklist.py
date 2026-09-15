"""字幕闸门只拦台词（2026-09-15 用户拍板：「字幕的底层逻辑就是视频生成中的台词作为黑名单」）。"""
from __future__ import annotations

import asyncio

from app.media_exec import subtitle_gate

LINES = ["对面那家连锁，出双倍租金，要你这个门面。", "这是受了重伤啊。", "这是一个强大的宝物。", "喵。"]


def _frames(*texts: str) -> list[dict]:
    return [{"index": i + 1, "text_seen": t, "where": "画面底部"} for i, t in enumerate(texts)]


def test_dialogue_text_is_subtitle_and_art_text_is_not() -> None:
    verdict = {"checked": True, "frames_checked": 10, "frames_reported": 10, "subtitle_overlay": True,
               "overlay_frames": _frames("距续约30天", "宝物", "这是受了重山啊", "出双倍租金，要你这个门面", "喵")}
    out = subtitle_gate.apply_dialogue_blacklist(verdict, LINES)
    assert out["subtitle_overlay"] is True
    assert [f["text_seen"] for f in out["overlay_frames"]] == ["宝物", "这是受了重山啊", "出双倍租金，要你这个门面", "喵"]
    assert [f["text_seen"] for f in out["diegetic_frames"]] == ["距续约30天"]


def test_only_art_text_passes_the_gate() -> None:
    verdict = {"checked": True, "frames_checked": 10, "frames_reported": 10, "subtitle_overlay": True,
               "overlay_frames": _frames("距续约30天", "晚安宠物医院")}
    out = subtitle_gate.apply_dialogue_blacklist(verdict, LINES)
    assert out["subtitle_overlay"] is False and out["overlay_frames"] == [] and len(out["diegetic_frames"]) == 2


def test_shot_without_dialogue_cannot_have_subtitles_and_unknown_lines_keep_model_verdict() -> None:
    verdict = {"checked": True, "frames_checked": 10, "frames_reported": 10, "subtitle_overlay": True,
               "overlay_frames": _frames("随便什么字")}
    assert subtitle_gate.apply_dialogue_blacklist(verdict, [])["subtitle_overlay"] is False
    assert subtitle_gate.apply_dialogue_blacklist(verdict, None) is verdict
    unchecked = {"checked": False, "error": "x"}
    assert subtitle_gate.apply_dialogue_blacklist(unchecked, LINES) is unchecked


def test_evaluate_version_consults_shot_dialogue(monkeypatch) -> None:
    written: list[dict] = []
    monkeypatch.setattr(subtitle_gate, "enabled", lambda: True)
    monkeypatch.setattr(subtitle_gate, "write_verdict", lambda version_id, verdict: written.append(verdict))
    monkeypatch.setattr(subtitle_gate, "spoken_lines_for_shot", lambda shot_id: LINES)

    async def detect(path, *, call_meta):
        return {"checked": True, "frames_checked": 10, "frames_reported": 10, "subtitle_overlay": True,
                "overlay_frames": _frames("距续约30天")}

    monkeypatch.setattr(subtitle_gate, "detect_subtitle_overlay", detect)
    result = asyncio.run(subtitle_gate.evaluate_version({"project_id": "p", "shot_id": "s"}, {"id": "v"}, "/tmp/v.mp4"))
    assert result["subtitle_overlay"] is False and result["diegetic_frames"][0]["text_seen"] == "距续约30天"
    assert written == [result]
