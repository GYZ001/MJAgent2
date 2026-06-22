#!/usr/bin/env python3
"""凭错误ID（ERR-...）从后端日志查报错根因。

用法：
    python scripts/find_error.py ERR-20260617-a3f9c1     # 查单条全文
    python scripts/find_error.py --list                  # 列最近 30 条
    python scripts/find_error.py --list 100              # 列最近 100 条

前端只展示 错误码+分类+错误ID；原始报错、堆栈、请求动作上下文都留在这张 error_logs 表里。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "manju.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_json(raw: str | None) -> str:
    if not raw:
        return "（无）"
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        return raw


def show(error_id: str) -> int:
    row = _conn().execute("SELECT * FROM error_logs WHERE id=?", (error_id,)).fetchone()
    if not row:
        print(f"未找到错误ID：{error_id}")
        return 1
    import datetime
    ts = datetime.datetime.fromtimestamp(row["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    print(f"错误ID    : {row['id']}")
    print(f"时间      : {ts}")
    print(f"分类      : {row['category_label']} ({row['category']})  技术类={bool(row['is_technical'])}")
    print(f"报错码    : {row['code']}    HTTP={row['http_status']}    异常类型={row['exc_type']}")
    print(f"请求动作  : {row['action']}")
    print("-" * 70)
    print("请求上下文:")
    print(_fmt_json(row["context_json"]))
    print("-" * 70)
    print("原始报错  :")
    print(row["message"] or "（无）")
    print("-" * 70)
    print("堆栈      :")
    print(row["traceback"] or "（无）")
    meta = _fmt_json(row["meta_json"])
    if meta and meta != "（无）" and meta != "{}":
        print("-" * 70)
        print(f"元信息    : {meta}")
    return 0


def list_recent(limit: int = 30) -> int:
    rows = _conn().execute(
        """SELECT id, ts, category_label, code, http_status, action
           FROM error_logs ORDER BY ts DESC LIMIT ?""", (limit,)).fetchall()
    if not rows:
        print("error_logs 表暂无记录。")
        return 0
    import datetime
    for r in rows:
        ts = datetime.datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
        print(f"{r['id']}  {ts}  [{r['code']:<8}] {r['category_label']}  "
              f"HTTP={r['http_status']}  {r['action']}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] in ("--list", "-l"):
        limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
        return list_recent(limit)
    return show(args[0])


if __name__ == "__main__":
    raise SystemExit(main())
