"""恢复演练：从备份恢复到临时目录，跑一组只读断言，输出实测 RPO/RTO。

判据挂"恢复得回来"，不挂"备份脚本跑过"（PRD/enterprise/EP-06 §4）：本脚本
真的把 ``.db.gz``/``.db`` 解压/复制、用独立只读连接打开、跑
``PRAGMA integrity_check`` + 核心表计数 + 最近一条审计时间，任何一步失败都是
非零退出码，不是"有备份文件"这件事本身。

用法：
    .venv/bin/python scripts/restore_drill.py --from /var/backups/mjagent2/db --to /tmp/mjagent2-drill
    .venv/bin/python scripts/restore_drill.py --from manju-20260101-030000.db.gz --to /tmp/drill --live-db data/manju.db

RPO 两个口径都报：
  - elapsed_since_backup_s：备份点到现在的挂钟耗时（标准"最坏情况丢多久数据"口径）；
  - row_deltas：给了 --live-db 时，逐表算恢复库与当前库的行数差（更直观的"丢了多少行"）。
"""
from __future__ import annotations

import argparse
import gzip
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts import backup_manju_db  # noqa: E402  -- 复用 verify_backup()，见模块 docstring

READONLY_ASSERTIONS: tuple[tuple[str, str], ...] = (
    ("projects", "SELECT COUNT(*) FROM projects"),
    ("episodes", "SELECT COUNT(*) FROM episodes"),
    ("users", "SELECT COUNT(*) FROM users"),
    ("models", "SELECT COUNT(*) FROM models"),
)


def resolve_source(from_arg: Path) -> Path:
    if from_arg.is_dir():
        latest = from_arg / "manju-latest.db.gz"
        if latest.exists():
            return latest
        raise FileNotFoundError(f"{from_arg} 下没有 manju-latest.db.gz，请显式指定备份文件")
    return from_arg


def resolve_backup_timestamp(path: Path) -> float:
    """优先从文件名 ``manju-<ts>.db.gz`` 解析出真实备份时刻；解析不出（比如
    调用方传了个自定义文件名）才退回文件 mtime。"""
    real_path = path.resolve() if path.is_symlink() else path
    match = backup_manju_db.TS_RE.match(real_path.name)
    if match:
        return datetime.strptime(match.group(1), backup_manju_db.TS_FMT).timestamp()
    return path.stat().st_mtime


def restore_to(src: Path, dest_dir: Path) -> Path:
    """解压/复制备份到 ``dest_dir/manju.db``（临时名 + rename，原子落地）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_db = dest_dir / "manju.db"
    tmp = dest_dir / "manju.db.restoring"
    if src.suffix == ".gz":
        with gzip.open(src, "rb") as f_in, open(tmp, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out, length=16 * 1024 * 1024)
    else:
        shutil.copyfile(src, tmp)
    tmp.replace(dest_db)
    return dest_db


def run_readonly_assertions(db_path: Path) -> dict[str, int | float | None]:
    """独立只读连接（``mode=ro``）：不复用恢复时写入用的连接，也不碰调用方的
    任何连接——CLAUDE.md「验证要有独立观察点」。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        results: dict[str, int | float | None] = {}
        for label, sql in READONLY_ASSERTIONS:
            try:
                results[label] = conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                results[label] = None
        try:
            results["latest_audit_ts"] = conn.execute("SELECT MAX(ts) FROM operation_audit").fetchone()[0]
        except sqlite3.OperationalError:
            results["latest_audit_ts"] = None
        return results
    finally:
        conn.close()


def scan_for_plaintext_keys(db_path: Path, known_keys: dict[str, str]) -> dict[str, int]:
    """把当前进程实际配置的供应商 Key 逐个在恢复出的库文件原始字节里数命中次
    数——EP-05 加密落地后应该全部为 0；见 PRD §8「备份文件中 grep 已知 Key
    前缀零命中」。只在调用方显式传入非空 Key 时才检查那一条，避免对着空字符
    串数出误报的"命中"。"""
    raw = db_path.read_bytes()
    hits: dict[str, int] = {}
    for label, key in known_keys.items():
        if not key:
            continue
        hits[label] = raw.count(key.encode("utf-8"))
    return hits


def compute_row_deltas(live_db: Path, restored_counts: dict) -> dict[str, int]:
    live_counts = run_readonly_assertions(live_db)
    return {
        label: live_counts[label] - restored_counts[label]
        for label, _ in READONLY_ASSERTIONS
        if isinstance(live_counts.get(label), int) and isinstance(restored_counts.get(label), int)
    }


def _known_provider_keys() -> dict[str, str]:
    from app import config
    return {
        "HIAGENT_API_KEY": config.HIAGENT_API_KEY,
        "OPENROUTER_API_KEY": config.OPENROUTER_API_KEY,
        "BAILIAN_API_KEY": config.BAILIAN_API_KEY,
        "DEEPSEEK_API_KEY": config.DEEPSEEK_API_KEY,
        "ZHIPU_API_KEY": config.ZHIPU_API_KEY,
        "MINIMAX_H3_API_KEY": config.MINIMAX_H3_API_KEY,
    }


def run_drill(
    from_path: Path, to_dir: Path, *, live_db: Path | None = None, scan_keys: bool = True,
) -> dict:
    """完整跑一次演练，返回结构化报告；不打印、不 sys.exit，方便测试直接断言。"""
    src = resolve_source(from_path)
    if not src.exists():
        raise FileNotFoundError(f"备份源不存在：{src}")
    backup_ts = resolve_backup_timestamp(src)

    started = time.monotonic()
    try:
        dest_db = restore_to(src, to_dir)
        verify_report = backup_manju_db.verify_backup(dest_db)
    except (OSError, sqlite3.DatabaseError) as exc:
        # 解压/拷贝阶段本身就能失败（截断的 gzip、根本不是 gzip、磁盘满……）——
        # 判据是"恢复得回来"，这里必须 fail closed 成一份结构化报告，不能让
        # 一个未捕获的异常整个演练直接崩溃退出，那样连"恢复失败"这个结论本身
        # 都不会被记录下来。
        rto_s = time.monotonic() - started
        return {
            "verify": {"ok": False, "error": f"恢复阶段异常：{exc!r}"},
            "rto_seconds": rto_s, "restored_counts": {},
            "rpo": {"elapsed_since_backup_s": time.time() - backup_ts, "row_deltas": None},
            "plaintext_key_hits": None, "ok": False,
        }
    restored_counts = run_readonly_assertions(dest_db) if verify_report["ok"] else {}
    rto_s = time.monotonic() - started

    report: dict = {
        "verify": verify_report,
        "rto_seconds": rto_s,
        "restored_counts": restored_counts,
        "rpo": {"elapsed_since_backup_s": time.time() - backup_ts, "row_deltas": None},
        "plaintext_key_hits": None,
        "ok": bool(verify_report["ok"]),
    }
    if verify_report["ok"] and live_db is not None and live_db.exists():
        report["rpo"]["row_deltas"] = compute_row_deltas(live_db, restored_counts)
    if verify_report["ok"] and scan_keys:
        hits = scan_for_plaintext_keys(dest_db, _known_provider_keys())
        report["plaintext_key_hits"] = hits
        if any(count > 0 for count in hits.values()):
            report["ok"] = False
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="from_path", type=Path, required=True,
                         help="备份文件（.db.gz 或 .db）或备份目录（取 manju-latest.db.gz）")
    parser.add_argument("--to", type=Path, required=True, help="恢复目标临时目录")
    parser.add_argument("--live-db", type=Path, default=None, help="当前生产库路径，用于算行数差 RPO；不给则跳过这项对比")
    parser.add_argument("--no-key-scan", action="store_true", help="跳过明文 Key 扫描（调试用）")
    parser.add_argument("--keep", action="store_true", help="演练结束后保留恢复出的文件（默认清理临时目录）")
    args = parser.parse_args(argv)

    try:
        report = run_drill(
            args.from_path, args.to, live_db=args.live_db, scan_keys=not args.no_key_scan,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if not args.keep and args.to.exists():
            shutil.rmtree(args.to, ignore_errors=True)

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
