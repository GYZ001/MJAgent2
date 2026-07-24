"""CI：未分类 mutating endpoint 使构建失败；写出 data/reports/capability-coverage.json。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.capabilities.coverage import assert_full_coverage, validate_catalog_integrity, write_coverage_json


def main() -> None:
    integrity = validate_catalog_integrity()
    if integrity:
        raise SystemExit("Capability catalog integrity errors:\n- " + "\n- ".join(integrity))
    report = assert_full_coverage()
    out = write_coverage_json(ROOT / "data" / "reports" / "capability-coverage.json")
    counts = report["prd_section5_checklist"]
    print(
        "Capability coverage OK: "
        f"{report['covered']} covered, {report['exempted']} exempted, "
        f"{counts['domain_commands']} commands, {counts['resources']} resources, "
        f"{counts['ui_intents']} ui, {counts['human_only']} human-only → {out}"
    )


if __name__ == "__main__":
    main()
