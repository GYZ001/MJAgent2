"""画面人数与身份闸门：单帧疑点加密复核（2026-10-01）。

真实形状复现 proj_ca86b15ab7d7 第 1 集第 33 段 ver_153e163b0c1d：稀疏抽样
（``subtitle_gate.FRAME_INTERVAL_S``=1.5 秒一帧）第 6 帧报两个顾屿、相邻第
5/7 帧各自只有一个，``MIN_CONSECUTIVE_FRAMES=2`` 的连续性判据拿不到第二帧
佐证，``character_duplicated`` 永远判 False。本文件覆盖
``app.media_exec.character_count_gate.evaluate_version`` 接上加密复核后的
四种真实路径：确认成立、加密帧不够不拦、首轮全干净不触发、加密调用失败放行。

夹具复用 ``tests/test_character_count_gate.py`` 同款 ``evaluate_version`` 桩法
（``character_roster_for_shot``/``write_verdict``）；VLM 调用不走整条
``detect_character_count``/``run_dense_recheck`` 的桩替身，直接桩到
``app.hiagent.chat`` 与抽帧函数这一层，让 ``suspect_window``/``run_dense_
recheck`` 真实运行，才算验到接线本身。
"""
from __future__ import annotations

import asyncio
import json

from app import hiagent
from app.media_exec import character_count_gate as gate
from app.media_exec import character_count_dense_recheck as dense
from app.media_exec import subtitle_gate


#: ver_153e163b0c1d 真实 qa_json 形状：frame1-3 温念+顾屿两人，frame4-10 只剩
#: 顾屿一人，唯独 frame6 报了两个顾屿（约 7.5 秒，落在实测叠化 7.85-8.8 秒前沿）。
_SPARSE_NAMES = {
    1: ["温念", "顾屿"], 2: ["温念", "顾屿"], 3: ["温念", "顾屿"],
    4: ["顾屿"], 5: ["顾屿"], 6: ["顾屿", "顾屿"], 7: ["顾屿"],
    8: ["顾屿"], 9: ["顾屿"], 10: ["顾屿"],
}


def _sparse_raw() -> str:
    return json.dumps({"frames": [
        {"index": i, "figures": [
            {"figure_no": j + 1, "desc": "一句话特征", "matched_name": name}
            for j, name in enumerate(names)
        ]}
        for i, names in _SPARSE_NAMES.items()
    ]}, ensure_ascii=False)


def _dense_raw(names_by_index: dict[int, list[str]], frame_count: int) -> str:
    return json.dumps({"frames": [
        {"index": i, "figures": [
            {"figure_no": j + 1, "desc": "加密帧特征", "matched_name": name}
            for j, name in enumerate(names_by_index.get(i, ["顾屿"]))
        ]}
        for i in range(1, frame_count + 1)
    ]}, ensure_ascii=False)


def _clean_sparse_raw() -> str:
    """十帧全程温念+顾屿各一位，没有任何单帧疑点。"""
    return json.dumps({"frames": [
        {"index": i, "figures": [
            {"figure_no": 1, "desc": "一句话特征", "matched_name": "温念"},
            {"figure_no": 2, "desc": "一句话特征", "matched_name": "顾屿"},
        ]}
        for i in range(1, 11)
    ]}, ensure_ascii=False)


def _wire(
    monkeypatch, *, dense_names_by_index: dict[int, list[str]] | None, dense_fails: bool,
    sparse_raw_fn=_sparse_raw,
):
    """``dense_names_by_index=None`` 表示首轮不该触发加密复核——加密 VLM 调用
    桩成断言失败，命中就是桩接线本身错了（即使被 run_dense_recheck 的
    except Exception 吞掉，``calls`` 也已经记下这次不该发生的调用）。"""
    calls: list[str] = []
    roster = [{"name": "温念", "appearance": ""}, {"name": "顾屿", "appearance": ""}]
    monkeypatch.setattr(gate, "enabled", lambda: True)
    monkeypatch.setattr(
        gate, "character_roster_for_shot",
        lambda shot_id, prompt: {"roster": roster, "allowed_headcount": 2, "unlimited_reason": ""},
    )
    monkeypatch.setattr(subtitle_gate, "sample_frames", lambda video_path: [b"f"] * 10)
    monkeypatch.setattr(dense, "_sample_window_frames", lambda video_path, *, start_s, frame_count: [b"f"] * frame_count)

    async def chat(messages, *, call_meta=None, **_kwargs):
        calls.append(call_meta.get("kind"))
        if call_meta.get("kind") == "vlm_character_count_gate_dense":
            if dense_names_by_index is None:
                raise AssertionError("首轮没有疑点时不该触发加密复核")
            if dense_fails:
                raise TimeoutError("加密复核 VLM 读超时")
            frame_count = len(messages[1]["content"]) - 1  # text + N 张图
            return _dense_raw(dense_names_by_index, frame_count)
        return sparse_raw_fn()

    monkeypatch.setattr(hiagent, "chat", chat)
    written: list[tuple[str, dict]] = []
    monkeypatch.setattr(gate, "write_verdict", lambda vid, verdict: written.append((vid, verdict)))
    return calls, written


def _run() -> dict:
    return asyncio.run(gate.evaluate_version(
        {"shot_id": "s33", "project_id": "p"}, {"id": "ver_153e163b0c1d", "prompt_text": ""}, "/tmp/v33.mp4",
    ))


def test_single_frame_suspect_triggers_dense_recheck_and_confirms(monkeypatch) -> None:
    """首轮只有第 6 帧单帧命中 → 加密复核 → 加密帧连续 ≥2 帧命中 → 改判成立。"""
    calls, written = _wire(
        monkeypatch, dense_names_by_index={3: ["顾屿", "顾屿"], 4: ["顾屿", "顾屿"]}, dense_fails=False,
    )
    result = _run()
    assert calls == ["vlm_character_count_gate", "vlm_character_count_gate_dense"]
    assert result["checked"] is True
    assert result["character_duplicated"] is True
    entry = result["duplicated_characters"][0]
    assert entry["name"] == "顾屿"
    assert [f["index"] for f in entry["frames"]] == [3, 4]
    # 加密帧的 seconds 必须是真实加密采样换算，不是稀疏公式——供人工核对时间窗。
    assert result["dense_recheck"]["confirmed"] is True
    assert result["dense_recheck"]["window"][0] < 7.5 < result["dense_recheck"]["window"][1]
    assert written == [("ver_153e163b0c1d", result)]


def test_single_frame_suspect_dense_recheck_not_confirmed_does_not_block(monkeypatch) -> None:
    """加密复核跑了，但加密帧里只有 1 帧命中——不够连续，不拦，原判保留。"""
    calls, written = _wire(monkeypatch, dense_names_by_index={4: ["顾屿", "顾屿"]}, dense_fails=False)
    result = _run()
    assert calls == ["vlm_character_count_gate", "vlm_character_count_gate_dense"]
    assert result["character_duplicated"] is False
    assert result["dense_recheck"]["confirmed"] is False
    assert "window" in result["dense_recheck"]
    assert written == [("ver_153e163b0c1d", result)]


def test_clean_first_pass_never_triggers_dense_recheck(monkeypatch) -> None:
    """首轮全干净（没有任何单帧疑点）——不触发加密复核，VLM 全程只调用一次
    （稀疏首轮），走的是真实 detect_character_count，不是桩替身。"""
    calls, written = _wire(
        monkeypatch, dense_names_by_index=None, dense_fails=False, sparse_raw_fn=_clean_sparse_raw,
    )
    result = _run()
    assert calls == ["vlm_character_count_gate"]
    assert result["character_duplicated"] is False and result["headcount_exceeded"] is False
    assert "dense_recheck" not in result
    assert written == [("ver_153e163b0c1d", result)]


def test_dense_recheck_call_failure_passes_through_with_trace(monkeypatch) -> None:
    """加密复核调用失败（供应商超时等）——按既有「未判定不拦」放行，原判保留，
    但必须在 dense_recheck 里留痕，供人工核对为什么没有加密确认。"""
    calls, written = _wire(
        monkeypatch, dense_names_by_index={3: ["顾屿", "顾屿"], 4: ["顾屿", "顾屿"]}, dense_fails=True,
    )
    result = _run()
    assert calls == ["vlm_character_count_gate", "vlm_character_count_gate_dense"]
    assert result["character_duplicated"] is False, "加密复核失败不得凭空升级成拦截"
    assert "TimeoutError" in result["dense_recheck"]["error"]
    assert "window" in result["dense_recheck"]
    assert written == [("ver_153e163b0c1d", result)]
