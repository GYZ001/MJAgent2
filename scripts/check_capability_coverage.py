"""CI：未分类 mutating endpoint 使构建失败；写出 data/reports/capability-coverage.json。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.capabilities.coverage import (
    assert_full_coverage,
    find_always_confirm_routes_without_gate,
    find_confirmation_policy_mismatches,
    validate_catalog_integrity,
    write_coverage_json,
)


def main() -> None:
    integrity = validate_catalog_integrity()
    if integrity:
        raise SystemExit("Capability catalog integrity errors:\n- " + "\n- ".join(integrity))
    # 产品规则闸门（2026-08-30 拍板：除了删除资源，否则不需要弹窗）：catalog 的
    # confirmation 必须与「是不是删除资源」双向一致，否则要么删资源不拦，要么
    # 非删除操作登记了对浏览器用户从不生效的 ALWAYS——两者都是空头承诺。
    mismatches = find_confirmation_policy_mismatches()
    if mismatches:
        raise SystemExit(
            "Confirmation policy does not match the resource-deletion rule:\n- "
            + "\n- ".join(mismatches)
        )
    # ALWAYS 声明的能力，真实 REST 路径必须有等价确认机制（Command Bus 调用或
    # 本地二段式），否则风险登记只是宣称、请求路径上没有任何东西在拦。
    ungated = find_always_confirm_routes_without_gate()
    if ungated:
        raise SystemExit(
            "ALWAYS-confirmation capabilities without a real REST confirmation gate:\n- "
            + "\n- ".join(ungated)
        )
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
