"""写事务跨 await 静态闸门：字面量写、经辅助函数泄漏的写、提交后再 await 的合法形态。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_write_across_await", ROOT / "scripts" / "check_write_across_await.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["check_write_across_await"] = mod
spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _scan(tmp_path: Path, source: str) -> list[str]:
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "m.py").write_text(source, encoding="utf-8")
    old_root = mod.ROOT
    mod.ROOT = tmp_path
    try:
        return [item.split("::")[1] for item in mod.scan_tree(pkg)]
    finally:
        mod.ROOT = old_root


def test_literal_write_then_await_before_commit_is_flagged(tmp_path: Path) -> None:
    src = """
async def bad(conn):
    conn.execute("UPDATE t SET a=1")
    await something()
    conn.commit()

async def good(conn):
    conn.execute("UPDATE t SET a=1")
    conn.commit()
    await something()

async def read_only(conn):
    conn.execute("SELECT 1")
    await something()
"""
    assert _scan(tmp_path, src) == ["bad"]


def test_write_leaked_through_helper_is_flagged(tmp_path: Path) -> None:
    src = """
def mark(conn, x):
    conn.execute("INSERT INTO t(a) VALUES(?)", (x,))

def mark_and_commit(conn, x):
    conn.execute("INSERT INTO t(a) VALUES(?)", (x,))
    conn.commit()

async def leaky(conn):
    mark(conn, 1)
    await something()

async def fine(conn):
    mark_and_commit(conn, 1)
    await something()

async def closed_before_await(conn):
    mark(conn, 1)
    conn.commit()
    await something()
"""
    assert _scan(tmp_path, src) == ["leaky"]


def test_baseline_entries_still_exist() -> None:
    """基线里的每一条都必须仍是真实违规：修好了就要从基线删掉（棘轮只减不增）。"""
    found = set(mod.scan_tree())
    baseline = set(mod.BASELINE.read_text(encoding="utf-8").split())
    assert baseline <= found, sorted(baseline - found)
