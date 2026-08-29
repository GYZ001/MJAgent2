#!/usr/bin/env python3
"""十集**并发**跑完整链路（映射台→分镜台→生成台→成片台），压并发能力。

与 `scripts/run_first10_videos.py`（同一套阶段实现，串行逐集）的区别只有一个：
这里十集同时开跑，用来观察后端的并发承载——队列争用、供应商侧并发上限、
以及各阶段在争用下的真实耗时。阶段逻辑**直接复用**串行驱动的函数，不另写
一份，避免两份实现漂移（那份已修好三个缺陷：写死项目 ID、状态竞态、固定
幂等键吃掉重试）。

失败策略沿用串行驱动：某集某阶段失败只记录该集，不影响其它集继续跑。

用法：
    py scripts/run_first10_concurrent.py                 # 十集并发
    py scripts/run_first10_concurrent.py --workers 5     # 限制并发度
    py scripts/run_first10_concurrent.py --project 王六郎
"""
from __future__ import annotations

import argparse
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import run_first10_videos as serial

ROOT = Path(__file__).resolve().parent.parent
serial.LOG = ROOT / "logs" / "first10_concurrent.log"

# serial.log() 从多个线程同时写同一个文件，加锁避免行交错。
_LOG_LOCK = threading.Lock()
_raw_log = serial.log


def _locked_log(msg: str) -> None:
    with _LOG_LOCK:
        _raw_log(msg)


serial.log = _locked_log
log = _locked_log

# 每集每阶段的真实耗时，用来看并发下哪一阶段被拖慢。
_TIMING_LOCK = threading.Lock()
TIMINGS: dict[str, dict[str, float]] = {}


def _record(name: str, stage: str, elapsed: float) -> None:
    with _TIMING_LOCK:
        TIMINGS.setdefault(name, {})[stage] = round(elapsed, 1)


def run_episode(name: str, eid: str) -> tuple[bool, str]:
    """逐阶段跑一集并记录耗时；沿用串行驱动的阶段实现与失败语义。"""
    since = time.time()
    for label, fn in serial.STAGES:
        started = time.time()
        try:
            fn(name, eid)
        except serial.StageFailure as exc:
            _record(name, label, time.time() - started)
            evidence = serial.failure_evidence(eid, since)
            log(f"{name} ✗ 在【{label}】阶段失败：{exc}")
            if evidence:
                for line in evidence.splitlines()[:8]:
                    log(f"    {name} 证据 {line}")
            return False, f"{label}阶段：{exc}"
        except Exception as exc:  # noqa: BLE001 - 未预期异常如实记录，不静默
            _record(name, label, time.time() - started)
            log(f"{name} ✗ 在【{label}】阶段抛出未预期异常：{exc!r}")
            return False, f"{label}阶段未预期异常：{exc!r}"
        _record(name, label, time.time() - started)
        log(f"{name} ✓ {label} 完成（{time.time() - started:.0f}s）")
    log(f"{name} ✅ 全链路完成（{time.time() - since:.0f}s）")
    return True, "ready"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=serial.PROJECT_NAME)
    parser.add_argument("--workers", type=int, default=10,
                        help="并发集数，默认 10（即全部并发）")
    parser.add_argument("--from", dest="ep_from", type=int, default=1,
                        help="起始集号（含），默认 1")
    parser.add_argument("--to", dest="ep_to", type=int, default=10,
                        help="结束集号（含），默认 10")
    args = parser.parse_args()

    project_id = serial.resolve_project_id(args.project)
    episodes = serial.resolve_first10(project_id, args.ep_from, args.ep_to)
    started_at = time.time()
    log(f"=== CONCURRENT START（project={args.project}/{project_id}，"
        f"EP{args.ep_from}-EP{args.ep_to}，{len(episodes)} 集，"
        f"并发度 {args.workers}，attempt={serial.ATTEMPT}）===")

    results: dict[str, tuple[bool, str]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(run_episode, name, eid): name
            for name, eid in episodes
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                results[name] = (False, f"线程异常：{exc!r}")

    wall = time.time() - started_at
    success = [n for n, (ok, _d) in results.items() if ok]
    log("=== CONCURRENT DONE ===")
    log(f"总墙钟 {wall / 60:.1f} 分钟；成功 {len(success)}/{len(episodes)}")
    if success:
        log("成功集：" + "、".join(sorted(success, key=lambda x: int(x[2:]))))
    for name, (ok, detail) in sorted(results.items(), key=lambda kv: int(kv[0][2:])):
        if not ok:
            log(f"失败集 {name}：{detail}")
    log("--- 各集分阶段耗时（秒）---")
    for name in sorted(TIMINGS, key=lambda x: int(x[2:])):
        row = "  ".join(f"{k}={v}" for k, v in TIMINGS[name].items())
        log(f"  {name}: {row}")
    return 0 if len(success) == len(episodes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
