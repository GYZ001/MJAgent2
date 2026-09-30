"""画面人数与身份闸门标定：对一批已知答案的视频跑线上同款判定函数，报混淆矩阵。

不经数据库、不写 ``qa_json``——直接从清单里给出的「允许人数 + 出镜角色外观要点」
构造输入，调用与生产完全相同的抽帧（``app.media_exec.subtitle_gate.sample_frames``）
+ VLM（``app.media_exec.character_count_gate.detect_character_count``）+ 代码核验
（``evaluate_headcount_and_duplication``），不触碰 shots/shot_versions/
character_portraits 等业务表。

清单格式（JSON 数组，每项）::

    {
      "video_path": "/path/to/video.mp4",
      "allowed_headcount": 2,
      "roster": [{"name": "温念", "appearance": "20多岁女性，长发……"},
                 {"name": "顾屿", "appearance": "20多岁男性，短发……"}],
      "expected": "positive"   // 或 "negative"：positive=本视频确实人数超额/角色重复
    }

``allowed_headcount`` 可为 ``null``（对应无限定人数场景，人数超额条不参与判定，
仍会核验角色重复）。

用法（B 上隔离沙箱，模型调用需要真实的供应商凭据环境，不需要真实的业务数据库）::

    MANJU_TEST_PROFILE=isolated MANJU_TEST_SANDBOX=/tmp/ccg_calib \\
        /root/MJAgent2/.venv/bin/python scripts/calibrate_character_count_gate.py \\
        --manifest /path/to/manifest.json

退出码：0 全部跑完（含命中率/误报率不理想，仍是 0——标定不是闸门，只报数据）；
1 清单为空或没有任何一条成功解析。不改库、不打印任何密钥/令牌。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError("清单必须是 JSON 数组")
    return items


async def _judge_one(item: dict[str, Any]) -> dict[str, Any]:
    from app.media_exec import character_count_gate as gate

    roster = [
        {"name": str(r.get("name") or ""), "appearance": str(r.get("appearance") or "")}
        for r in item.get("roster") or []
    ]
    started = time.monotonic()
    try:
        parsed = await gate.detect_character_count(
            item["video_path"], roster,
            call_meta={"purpose": "character_count_gate_calibration"},
        )
    except Exception as exc:  # noqa: BLE001 标定只记录，不中断整批
        return {
            "video_path": item["video_path"], "actual": "unchecked",
            "error": f"{type(exc).__name__}: {exc}"[:200],
            "elapsed_s": round(time.monotonic() - started, 1),
        }
    judged = gate.evaluate_headcount_and_duplication(
        parsed, allowed_headcount=item.get("allowed_headcount"),
        roster_names=[r["name"] for r in roster],
    )
    actual = "positive" if (judged["headcount_exceeded"] or judged["character_duplicated"]) else "negative"
    return {
        "video_path": item["video_path"], "actual": actual,
        "headcount_exceeded": judged["headcount_exceeded"],
        "character_duplicated": judged["character_duplicated"],
        "duplicated_names": [d["name"] for d in judged["duplicated_characters"]],
        "frames_checked": parsed["frames_checked"], "frames_reported": parsed["frames_reported"],
        "elapsed_s": round(time.monotonic() - started, 1),
    }


def _confusion(results: list[dict[str, Any]], expected_by_video: dict[str, str]) -> dict[str, int]:
    counts = {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "unchecked": 0}
    for result in results:
        expected = expected_by_video.get(result["video_path"])
        actual = result["actual"]
        if actual == "unchecked":
            counts["unchecked"] += 1
        elif expected == "positive" and actual == "positive":
            counts["tp"] += 1
        elif expected == "positive" and actual == "negative":
            counts["fn"] += 1
        elif expected == "negative" and actual == "positive":
            counts["fp"] += 1
        elif expected == "negative" and actual == "negative":
            counts["tn"] += 1
    return counts


def _print_report(results: list[dict[str, Any]], expected_by_video: dict[str, str]) -> None:
    for result in results:
        expected = expected_by_video.get(result["video_path"], "?")
        mark = "OK" if result["actual"] == expected else ("未判定" if result["actual"] == "unchecked" else "MISS")
        detail = result.get("error") or (
            f"headcount_exceeded={result.get('headcount_exceeded')} "
            f"character_duplicated={result.get('character_duplicated')} "
            f"重复角色={result.get('duplicated_names')}"
        )
        print(f"[{mark}] {result['video_path']}  期望={expected} 实际={result['actual']}  "
              f"{result['elapsed_s']}s  {detail}")
    counts = _confusion(results, expected_by_video)
    hit_denominator = counts["tp"] + counts["fn"]
    fp_denominator = counts["fp"] + counts["tn"]
    hit_rate = counts["tp"] / hit_denominator if hit_denominator else float("nan")
    false_positive_rate = counts["fp"] / fp_denominator if fp_denominator else float("nan")
    print(
        f"\n混淆矩阵：TP={counts['tp']} FN={counts['fn']} FP={counts['fp']} TN={counts['tn']} "
        f"未判定={counts['unchecked']}"
    )
    print(f"命中率（TP/(TP+FN)）={hit_rate:.2f}  误报率（FP/(FP+TN)）={false_positive_rate:.2f}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    items = _load_manifest(args.manifest)
    if not items:
        print("清单为空")
        return 1
    expected_by_video = {str(item["video_path"]): str(item.get("expected") or "") for item in items}
    results = asyncio.run(_run_all(items))
    _print_report(results, expected_by_video)
    return 0 if any(r["actual"] != "unchecked" for r in results) else 1


async def _run_all(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [await _judge_one(item) for item in items]


if __name__ == "__main__":
    raise SystemExit(main())
