#!/usr/bin/env python3
"""把项目清回「小说已导入、分集已规划」的状态，供全链路重跑。

清理范围的判据刻意反着写：不列「要删什么」，而是列「要留什么」，其余一律清掉。
产出字段会随功能增加，列删除清单意味着每加一个字段就要记得同步一次，忘了就是
上一轮的残留混进这一轮，看起来像新产出。列保留清单则相反，新字段默认被清。

同理，表也必须逐张归类。出现未归类的新表时直接退出，不猜它属于哪边——猜错的
两个方向一个是漏清、一个是删掉真数据，都比停下来问一句贵。

用法：
    py scripts/reset_pipeline_data.py --dry-run     # 预览，不改任何东西
    py scripts/reset_pipeline_data.py               # 执行
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import config  # noqa: E402


# 整表清空：全部是流水线产出或单次运行的记录，重跑会重新长出来。
PURGE_TABLES = {
    # 世界书与视觉资产
    "character_payment_quotes",
    "character_portrait_views",
    "character_portraits",
    "reference_assets",
    "reference_sets",
    "scene_reference_views",
    "scene_references",
    "visual_entity_merges",
    # 映射包与分镜
    "screenplay_drafts",
    "shot_scenes",
    "shot_versions",
    "shots",
    "storyboard_action_previews",
    "storyboard_edit_sessions",
    "storyboard_source_bindings",
    "storyboard_workspace_state",
    # 视频生成与交付
    "concat_operation_receipts",
    "delivery_operation_receipts",
    "delivery_packages",
    "episode_video_budget_authorities",
    "episode_video_generation_plans",
    "episode_video_publish_leases",
    "media_cleanup_outbox",
    "media_task_dependencies",
    "media_tasks",
    "provider_media_publications",
    "provider_video_budget_claims",
    "shot_video_generation_plans",
    "video_boundary_assets",
    "video_budget_authority_ledger",
    "video_budget_authorization_receipts",
    "video_command_operation_receipts",
    "video_generation_attempts",
    "video_plan_dependencies",
    "video_version_archives",
    # 编排、闸门与凭据
    "artifacts",
    "budget_reservations",
    "command_idempotency",
    "completion_certificates",
    "completion_grants",
    "gate_decisions",
    "jobs",
    "production_grants",
    "production_revisions",
    "run_events",
    "step_runs",
    "workflow_runs",
    # Agent 会话与评审记录
    "agent_approvals",
    "agent_conversations",
    "agent_messages",
    "agent_tool_calls",
    "agent_turn_events",
    "agent_turns",
    "benchmark_runs",
    "customer_feedback",
    "error_logs",
    "evaluations",
    "review_action_audit",
}

# 原样保留。三类：小说原文、运维配置、跨轮次的观测。
# provider_calls 尤其不能清——model_runtime_profile 靠它推导每个模型的思考上界，
# 清掉等于把自适应预算打回全局默认值，而那正是分镜台此前失败的配置。
KEEP_TABLES = {
    "chapters",
    "mcp_tokens",
    "monitor_audit",
    "novel_import_receipts",
    # 账号域，不是流水线产出：payment_orders 是真金白银的支付订单，quota_ledger
    # 是按 user_id 记账的会员配额（两张表都不含 project_id，清不出项目粒度）。
    # 清掉前者等于毁支付记录，清掉后者等于凭空发配额——重跑时配额真的不够，
    # 那是要报出来的产品信号，不是靠清账本绕过去的。
    "payment_orders",
    "quota_ledger",
    "provider_calls",
    "provider_video_capability_snapshots",
    "settings",
    "sqlite_sequence",
    "user_sessions",
    "users",
}

# 保留行、逐字段回落到建表默认值。值是「不算产出」的字段，其余全部重置。
RESET_TABLES: dict[str, set[str]] = {
    "projects": {
        "id",
        "name",
        "novel_chars",
        "created_at",
        "owner_user_id",
        "harness_engine_enabled",
        "status",
        # 分集规划不重跑：1616 集的切分与本次要验的四个环节无关。
        "plan_status",
        "plan_error",
        "key_timeline",
        # 画风与各环节选用的模型属于运维配置，清掉会让重跑跑在默认模型上。
        "bible_style_name",
        "bible_text_provider",
        "script_text_provider",
        "board_text_provider",
        "refs_resume",
    },
    "episodes": {
        "id",
        "project_id",
        "episode_no",
        "title",
        "hook",
        "cliffhanger",
        "synopsis",
        "source_chapters",
        "target_duration_s",
        "planning_target_duration_s",
        "planning_duration_source",
        "target_duration_authority",
        "created_at",
        "video_completion_mode",
        "target_video_model",
    },
}


def _column_defaults(conn: sqlite3.Connection, table: str) -> dict[str, object]:
    """列名到建表默认值的映射；没写默认值的按 NULL 算。"""
    defaults: dict[str, object] = {}
    for row in conn.execute(f"PRAGMA table_info({table})"):
        name, raw = row[1], row[4]
        if raw is None:
            defaults[name] = None
            continue
        text = str(raw)
        if text.upper() == "NULL":
            defaults[name] = None
        elif len(text) >= 2 and text[0] == text[-1] == "'":
            defaults[name] = text[1:-1]
        else:
            try:
                defaults[name] = int(text)
            except ValueError:
                try:
                    defaults[name] = float(text)
                except ValueError:
                    defaults[name] = text
    return defaults


def _classify(conn: sqlite3.Connection) -> list[str]:
    """返回未归类的表名。归类清单与库里实际的表对不上就是有人加了表没管这里。"""
    live = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    known = PURGE_TABLES | KEEP_TABLES | set(RESET_TABLES)
    return sorted(live - known)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只预览，不修改")
    parser.add_argument(
        "--db", default=str(config.DB_PATH), help="数据库路径，默认取 config.DB_PATH"
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    unknown = _classify(conn)
    if unknown:
        print("以下表未归类，先决定它们属于清空/保留/重置再跑：")
        for name in unknown:
            print(f"  - {name}")
        return 2

    print(f"数据库: {db_path}")
    print()
    print("== 保留（不动）==")
    for table in sorted(KEEP_TABLES):
        if table == "sqlite_sequence":
            continue
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"  {table}: {n} 行")

    print()
    print("== 清空 ==")
    total_purged = 0
    for table in sorted(PURGE_TABLES):
        n = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        total_purged += n
        if n:
            print(f"  {table}: {n} 行")

    print()
    print("== 保留行、重置产出字段 ==")
    reset_plan: dict[str, dict[str, object]] = {}
    for table, keep_cols in RESET_TABLES.items():
        defaults = _column_defaults(conn, table)
        missing = keep_cols - set(defaults)
        if missing:
            print(f"  {table}: 保留清单里有库中不存在的列 {sorted(missing)}，先对齐再跑")
            return 2
        targets = {c: defaults[c] for c in defaults if c not in keep_cols}
        reset_plan[table] = targets
        rows = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        print(f"  {table}: {rows} 行，重置 {len(targets)} 个字段，保留 {len(keep_cols)} 个")

    project_ids = [r[0] for r in conn.execute("SELECT id FROM projects")]
    media_root = REPO_ROOT / "projects"
    media_dirs = [media_root / pid for pid in project_ids]
    media_dirs = [d for d in media_dirs if d.exists()]
    orphan_dirs = []
    if media_root.exists():
        orphan_dirs = [
            d
            for d in media_root.iterdir()
            if d.is_dir() and d.name not in set(project_ids)
        ]

    print()
    print("== 磁盘产物 ==")
    for d in media_dirs + orphan_dirs:
        tag = "（孤儿目录，库里已无此项目）" if d in orphan_dirs else ""
        n = sum(1 for _ in d.rglob("*") if _.is_file())
        print(f"  {d}: {n} 个文件{tag}")
    if not media_dirs and not orphan_dirs:
        print("  无")

    if args.dry_run:
        print()
        print("--dry-run：以上都没有执行。")
        return 0

    print()
    print("执行中…")
    # 删除顺序跟着引用方向走：先删引用别人的，再删被引用的。库里 foreign_keys
    # 当前是关的，顺序不会被强制，但一旦将来打开，反序就会直接失败。
    ordered = [
        "evaluations",
        "artifacts",
        "run_events",
        "step_runs",
        "workflow_runs",
    ]
    remaining = sorted(PURGE_TABLES - set(ordered))
    with conn:
        for table in ordered + remaining:
            conn.execute(f'DELETE FROM "{table}"')
        for table, targets in reset_plan.items():
            if not targets:
                continue
            assignments = ", ".join(f'"{c}" = ?' for c in targets)
            conn.execute(
                f'UPDATE "{table}" SET {assignments}', list(targets.values())
            )
    conn.execute("VACUUM")
    conn.close()

    for d in media_dirs + orphan_dirs:
        shutil.rmtree(d)
        print(f"  已删目录 {d}")

    print(f"完成：清空 {total_purged} 行，重置 {len(reset_plan)} 张表的产出字段。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
