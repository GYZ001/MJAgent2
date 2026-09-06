"""机器水位闸：/proc 采样、阈值判定、磁盘 IO 利用率按两次采样之差算（2026-09-05 用户拍板并发以机器性能为上限）。"""
from __future__ import annotations

import pytest

from app.observability import machine_watermark as mw

MEMINFO = "MemTotal:       32403100 kB\nMemFree:        17161164 kB\nMemAvailable:   27902520 kB\n"
DISKSTATS = (
    " 253       0 vda 1 2 3 4 5 6 7 8 9 4986576 11\n"
    " 253       2 vda2 1 2 3 4 5 6 7 8 9 4985548 11\n"
)


def test_parsers_read_the_documented_proc_fields() -> None:
    assert mw.memory_pct_from_meminfo(MEMINFO) == 13.9
    assert mw.io_ticks_from_diskstats(DISKSTATS, 253, 2) == 4985548
    assert mw.io_ticks_from_diskstats(DISKSTATS, 8, 0) is None
    assert mw.cpu_load_ratio_from_loadavg("1.13 1.17 1.00 1/543 3377097", 8) == 0.14
    assert mw.memory_pct_from_meminfo("garbage") is None
    assert mw.cpu_load_ratio_from_loadavg("", 8) is None


def _sampler(monkeypatch, *, meminfo=MEMINFO, loadavg="1.13 1.17 1.00 1/543 1", ticks=(1000, 1000)):
    sampler = mw.MachineWatermark(data_dir="/tmp")
    calls = {"n": 0}

    def fake_read(path: str):
        if path == mw._MEMINFO:
            return meminfo
        if path == mw._LOADAVG:
            return loadavg
        if path == mw._DISKSTATS:
            calls["n"] += 1
            value = ticks[min(calls["n"] - 1, len(ticks) - 1)]
            return f" 253       2 vda2 1 2 3 4 5 6 7 8 9 {value} 11\n"
        return None

    monkeypatch.setattr(sampler, "_read", fake_read)
    monkeypatch.setattr(sampler, "_device", lambda: (253, 2))
    monkeypatch.setattr(mw, "get_setting", lambda key: None)
    return sampler


def test_disk_io_utilisation_is_the_busy_time_delta_over_wall_time(monkeypatch) -> None:
    sampler = _sampler(monkeypatch, ticks=(1000, 1000 + 1500))
    first = sampler.sample(now_ts=100.0)
    assert first.disk_io_pct == 0.0 and not first.throttled
    second = sampler.sample(now_ts=102.0)  # 2 秒里忙了 1.5 秒
    assert second.disk_io_pct == 75.0
    assert second.throttled and "磁盘 IO 75%" in second.reasons[0]
    assert sampler.sample(now_ts=103.0) is second  # 2 秒缓存


def test_memory_and_cpu_thresholds_use_settings(monkeypatch) -> None:
    hot_mem = "MemTotal:       1000 kB\nMemAvailable:   200 kB\n"
    sampler = _sampler(monkeypatch, meminfo=hot_mem, loadavg="7.2 5 5 1/1 1")
    monkeypatch.setattr(mw.os, "cpu_count", lambda: 8)
    wm = sampler.sample(now_ts=1.0)
    assert wm.memory_pct == 80.0 and wm.cpu_load_ratio == 0.9
    assert [r[:4] for r in wm.reasons] == ["内存占用", "CPU "]
    monkeypatch.setattr(mw, "get_setting", lambda key: {"admission_memory_pct": "90", "admission_cpu_load_ratio": "1.5"}.get(key))
    wm2 = sampler.sample(now_ts=10.0)
    assert not wm2.throttled


def test_unreadable_proc_means_no_check_not_health(monkeypatch) -> None:
    sampler = mw.MachineWatermark(data_dir="/tmp")
    monkeypatch.setattr(sampler, "_read", lambda path: None)
    monkeypatch.setattr(sampler, "_device", lambda: None)
    monkeypatch.setattr(mw, "get_setting", lambda key: None)
    wm = sampler.sample(now_ts=1.0)
    assert wm.memory_pct is None and wm.disk_io_pct is None and wm.cpu_load_ratio is None
    assert not wm.throttled


@pytest.mark.asyncio
async def test_wait_until_admitted_polls_until_clear(monkeypatch) -> None:
    state = {"n": 2}

    def reason():
        state["n"] -= 1
        return "x" if state["n"] > 0 else None

    monkeypatch.setattr(mw, "throttle_reason", reason)
    await mw.wait_until_admitted(poll_s=0.01)
    assert state["n"] == 0
