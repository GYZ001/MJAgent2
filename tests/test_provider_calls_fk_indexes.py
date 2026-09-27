"""provider_calls 自引用外键的子列必须有索引。

``_prune_observability_logs`` 在启动时按 ts 删除过期调用；表上有
supersedes_call_id / superseded_by_call_id 两个指回自身的外键，没有子列索引时
SQLite 每删一行都要对整表扫两遍（这两列排在大字段之后，要读完溢出页），
2026-09-27 B 上一天到期的 4718 行让后端启动卡了 5 分钟以上，定时部署因此判失败。
"""
from app import db


def test_self_referencing_fk_columns_are_indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "fk-index.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    db.init_db()
    conn = db.get_conn()
    indexed = set()
    for row in conn.execute("PRAGMA index_list(provider_calls)").fetchall():
        indexed.update(info[2] for info in conn.execute(f"PRAGMA index_info({row[1]})").fetchall())
    assert {"supersedes_call_id", "superseded_by_call_id"} <= indexed
    plan = " ".join(str(r[3]) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM provider_calls WHERE superseded_by_call_id=?", (1,)
    ).fetchall())
    assert "idx_provider_calls_superseded_by" in plan
