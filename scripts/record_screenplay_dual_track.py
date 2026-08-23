#!/usr/bin/env python3
"""Validate and record an attested 30-episode screenplay dual-track run.

Input JSON must contain ``baseline_samples`` and ``candidate_samples``.  Every
sample carries an ``episode_id`` and the metrics defined by the screenplay
full-chain performance plan (removed from ``docs/``; see git history).  This
command records measurements; it deliberately does not invent or simulate
provider executions.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.benchmarks import record_screenplay_benchmark


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--attested-by", required=True)
    parser.add_argument("--note", required=True)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    result = record_screenplay_benchmark(
        project_id=args.project_id,
        baseline_samples=list(payload.get("baseline_samples") or []),
        candidate_samples=list(payload.get("candidate_samples") or []),
        attested_by=args.attested_by,
        attestation_note=args.note,
        baseline_label=str(payload.get("baseline_label") or "screenplay_monolith"),
        candidate_label=str(payload.get("candidate_label") or "screenplay_scene_shards"),
        thresholds=dict(payload.get("thresholds") or {}),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
