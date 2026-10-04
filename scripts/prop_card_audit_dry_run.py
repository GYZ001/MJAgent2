#!/usr/bin/env python3
"""道具卡「按现行规则复核」dry-run：对一个项目的全部（或点名的）道具卡只跑
模型判定 + 代码核验，输出拟删子句/别名——不写 ``prop_card_rule_audits``
表、不改世界书、不重新出图。

用途：下一阶段要在 B 沙箱对全部存量卡（2026-10-03 实测 7 个项目共 123 张）
做真实模型验证，验证规则改动后的判定结果是否符合预期，再决定要不要真的
跑一遍写库版本（``app.props.card_audit.audit_one_prop_card(..., dry_run=
False)``，或道具库页面的「按现行规则复核」按钮）。

**不做任何网络之外的副作用保证**：dry_run 路径本身在应用层已经保证不写
``prop_card_rule_audits``、不 mutate 世界书、不触发出图（见
``app.props.card_audit.audit_one_prop_card`` 的 ``dry_run`` 分支）；本脚本
只是命令行外壳，不额外加一层只读连接限制——按 CLAUDE.md「生产库只读」的
约定，脚本应当指向 B 的沙箱副本而不是生产库本体，由运行者负责把
``app.config``/``app.db`` 的 DB_PATH 指到沙箱。

用法：
    py scripts/prop_card_audit_dry_run.py --project proj_ca86b15ab7d7
    py scripts/prop_card_audit_dry_run.py --project proj_xxx --prop-name 浅灰色卫衣 --prop-name 绿萝
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.props import card_audit  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="项目 id（projects.id）")
    parser.add_argument(
        "--prop-name", action="append", default=None,
        help="只对指定道具名跑（可重复传多次）；不传则对项目全部道具卡跑",
    )
    return parser.parse_args()


async def _run(project_id: str, prop_names: list[str] | None) -> dict:
    if prop_names:
        results = []
        for name in prop_names:
            try:
                results.append({"prop_name": name, **await card_audit.audit_one_prop_card(
                    project_id, name, dry_run=True,
                )})
            except ValueError as exc:
                results.append({"prop_name": name, "error": str(exc)})
        return {"project_id": project_id, "dry_run": True, "results": results}
    return await card_audit.audit_project_prop_cards(project_id, dry_run=True)


def main() -> int:
    args = _parse_args()
    try:
        output = asyncio.run(_run(args.project, args.prop_name))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    changed = sum(1 for r in output["results"] if r.get("appearance_changed") or r.get("removed_aliases"))
    failed = sum(1 for r in output["results"] if r.get("failed") or r.get("error"))
    # doubts：两次独立判定不一致/归属没有自己的卡/模型自述拿不准——沙箱验证要单独
    # 看这一类，它们不在 changed 里（没有被自动删除），不可见就等于白跑一轮模型调用。
    doubted = sum(1 for r in output["results"] if r.get("doubts"))
    print(
        f"# 共 {len(output['results'])} 张卡：{changed} 张有拟删改动，{failed} 张判定失败/出错，"
        f"{doubted} 张有待人工确认的存疑",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
