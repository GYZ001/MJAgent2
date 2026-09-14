"""视频字幕闸门：``app.media_exec.subtitle_gate``（抽帧 + VLM 判定 + 写 qa_json）与
``app.evidence.subtitle_overlay``（把结论并进技术校验）。

产品规则：视频生成只负责画面与声音，字幕由后续功能另做；牌匾、书信等画面文字不在
禁止之列。闸门未判定（关闭 / 抽帧失败 / 模型不可用）一律放行，只留痕。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.evidence import subtitle_overlay
from app.media_exec import run_job_steps, subtitle_gate


def _verdict(overlay: bool) -> dict:
    frames = [{"index": 4, "text_seen": "仙人", "where": "画面下方"}] if overlay else []
    return {"checked": True, "frames_checked": 10, "frames_reported": 10,
            "subtitle_overlay": overlay, "overlay_frames": frames}


# ---------------------------------------------------------------------------
# parse_verdict
# ---------------------------------------------------------------------------

def test_parse_verdict_collects_overlay_frames_in_input_order() -> None:
    raw = json.dumps({"frames": [
        {"index": 1, "overlay_text": False, "text_seen": "", "where": ""},
        {"index": 2, "overlay_text": True, "text_seen": "仙人", "where": "画面下方，人物嘴部位置"},
        {"index": 3, "overlay_text": False},
    ]}, ensure_ascii=False)
    verdict = subtitle_gate.parse_verdict(raw, frames_checked=3)
    assert verdict["subtitle_overlay"] is True
    assert verdict["overlay_frames"] == [{"index": 2, "text_seen": "仙人", "where": "画面下方，人物嘴部位置"}]
    assert verdict["frames_checked"] == 3 and verdict["frames_reported"] == 3 and verdict["checked"] is True


def test_parse_verdict_tolerates_markdown_fence_and_prose() -> None:
    raw = '好的，结果如下：\n```json\n{"frames":[{"index":1,"overlay_text":false}]}\n```'
    verdict = subtitle_gate.parse_verdict(raw, frames_checked=1)
    assert verdict["subtitle_overlay"] is False and verdict["overlay_frames"] == []


def test_parse_verdict_only_trusts_literal_true() -> None:
    """"true" 字符串、1 这类都不算命中——误拦一次就是一次视频额度。"""
    raw = json.dumps({"frames": [{"index": 1, "overlay_text": "true"}, {"index": 2, "overlay_text": 1}]})
    assert subtitle_gate.parse_verdict(raw, frames_checked=2)["subtitle_overlay"] is False


@pytest.mark.parametrize("raw", ["", "没有 JSON", '{"verdict": "ok"}', '{"frames": "yes"}'])
def test_parse_verdict_rejects_unparseable_answers(raw: str) -> None:
    with pytest.raises(ValueError):
        subtitle_gate.parse_verdict(raw, frames_checked=2)


# ---------------------------------------------------------------------------
# enabled()
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stored, expected", [("", True), ("true", True), ("FALSE", False)])
def test_enabled_reads_setting_with_true_default(monkeypatch, stored: str, expected: bool) -> None:
    monkeypatch.setattr(subtitle_gate, "get_setting", lambda key: stored)
    assert subtitle_gate.enabled() is expected


def test_enabled_rejects_garbage_setting(monkeypatch) -> None:
    monkeypatch.setattr(subtitle_gate, "get_setting", lambda key: "maybe")
    with pytest.raises(RuntimeError):
        subtitle_gate.enabled()


# ---------------------------------------------------------------------------
# evaluate_version：永不抛出，结论落 qa_json
# ---------------------------------------------------------------------------

def _run_evaluate(monkeypatch, *, enabled: bool, detect):
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(subtitle_gate, "enabled", lambda: enabled)
    monkeypatch.setattr(subtitle_gate, "detect_subtitle_overlay", detect)
    monkeypatch.setattr(subtitle_gate, "write_verdict", lambda version_id, verdict: written.append((version_id, verdict)))
    job = {"project_id": "proj_1", "shot_id": "shot_1"}
    result = asyncio.run(subtitle_gate.evaluate_version(job, {"id": "ver_1"}, "/tmp/v1.mp4"))
    return result, written


def test_evaluate_version_writes_overlay_verdict(monkeypatch) -> None:
    async def detect(path, *, call_meta):
        assert path == "/tmp/v1.mp4" and call_meta["version_id"] == "ver_1"
        return _verdict(True)

    result, written = _run_evaluate(monkeypatch, enabled=True, detect=detect)
    assert result["subtitle_overlay"] is True
    assert written == [("ver_1", result)]


def test_evaluate_version_passes_through_when_model_fails(monkeypatch, caplog) -> None:
    async def detect(path, *, call_meta):
        raise TimeoutError("VLM 读超时")

    with caplog.at_level("WARNING"):
        result, written = _run_evaluate(monkeypatch, enabled=True, detect=detect)
    assert result["checked"] is False and "TimeoutError" in result["error"]
    assert written == [("ver_1", result)]
    assert any("[VIDEO_SUBTITLE_GATE][未判定]" in rec.getMessage() for rec in caplog.records)


def test_evaluate_version_skips_when_disabled(monkeypatch) -> None:
    async def detect(path, *, call_meta):
        raise AssertionError("闸门关闭时不该抽帧")

    result, written = _run_evaluate(monkeypatch, enabled=False, detect=detect)
    assert result is None and written == []


def test_run_auto_qa_invokes_gate_before_supervisor_decision(monkeypatch) -> None:
    calls: list[tuple] = []

    async def evaluate(job, version, dest):
        calls.append((job["episode_id"], version["id"], dest))
        return _verdict(False)

    monkeypatch.setattr(subtitle_gate, "evaluate_version", evaluate)
    monkeypatch.setattr(run_job_steps, "get_conn", lambda: _NoEpisodeConn())
    supervisor = asyncio.run(run_job_steps.run_auto_qa({"episode_id": "ep_1"}, {"id": "ver_1"}, "/tmp/v.mp4"))
    assert calls == [("ep_1", "ver_1", "/tmp/v.mp4")]
    assert supervisor is False


class _NoEpisodeConn:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return None


# ---------------------------------------------------------------------------
# technical_with_verdict：并进技术校验
# ---------------------------------------------------------------------------

def _technical(passed: bool = True) -> dict:
    return {"passed": passed, "issues": [], "evidence": {"duration_s": 15.0}}


def test_overlay_verdict_blocks_technically_valid_candidate() -> None:
    merged = subtitle_overlay.technical_with_verdict(_technical(), {"subtitle_gate": _verdict(True)})
    assert merged["passed"] is False
    assert [issue.code for issue in merged["issues"]] == ["subtitle_overlay"]
    assert "仙人" in merged["issues"][0].message and "第 4 帧" in merged["issues"][0].message
    assert merged["evidence"] == {"duration_s": 15.0}


@pytest.mark.parametrize("qa", [None, {}, {"subtitle_gate": _verdict(False)},
                                {"subtitle_gate": {"checked": False, "error": "TimeoutError: 读超时"}},
                                {"subtitle_gate": "garbage"}])
def test_clean_or_unchecked_verdict_leaves_technical_untouched(qa) -> None:
    technical = _technical()
    assert subtitle_overlay.technical_with_verdict(technical, qa) is technical


def test_overlay_verdict_keeps_existing_technical_issues() -> None:
    technical = {"passed": False, "issues": ["文件损坏"], "evidence": {}}
    merged = subtitle_overlay.technical_with_verdict(technical, {"subtitle_gate": _verdict(True)})
    assert merged["issues"][0] == "文件损坏" and merged["issues"][1].code == "subtitle_overlay"
