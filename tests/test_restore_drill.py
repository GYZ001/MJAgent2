"""``scripts/restore_drill.py``：判据挂"恢复得回来"，不挂"备份脚本跑过"
（PRD/enterprise/EP-06 §4）。用一个最小合成 sqlite 库（覆盖
``backup_manju_db.CORE_TABLES`` 的完整性校验所需全部表 + models/
operation_audit/settings）跑真实的 备份 -> 恢复 -> 只读断言 全链路，不打桩
任何一步——判据本身要求"实测值"，打桩会让这份测试失去意义。
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from scripts import backup_manju_db, restore_drill


def _build_source_db(path, *, extra_episodes: int = 0, custom_models_key: str | None = None):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE episodes(id TEXT PRIMARY KEY, project_id TEXT);
        CREATE TABLE shots(id TEXT PRIMARY KEY, episode_id TEXT);
        CREATE TABLE shot_versions(id TEXT PRIMARY KEY, shot_id TEXT);
        CREATE TABLE provider_calls(id INTEGER PRIMARY KEY, kind TEXT);
        CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT);
        CREATE TABLE models(id TEXT PRIMARY KEY, provider TEXT);
        CREATE TABLE operation_audit(id TEXT PRIMARY KEY, ts REAL);
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute("INSERT INTO projects VALUES('p1','项目一')")
    conn.execute("INSERT INTO users VALUES('u1','root')")
    conn.execute("INSERT INTO models VALUES('m1','hiagent')")
    for i in range(3 + extra_episodes):
        conn.execute("INSERT INTO episodes VALUES(?,?)", (f"ep{i}", "p1"))
    conn.execute("INSERT INTO operation_audit VALUES('a1', ?)", (time.time(),))
    if custom_models_key:
        conn.execute(
            "INSERT INTO settings VALUES('custom_models', ?)",
            (f'[{{"provider":"x","api_key":"{custom_models_key}"}}]',),
        )
    conn.commit()
    conn.close()


@pytest.fixture()
def backed_up(tmp_path):
    """真跑一次 backup_manju_db.run_backup()，产出一个真实 .db.gz。"""
    src_db = tmp_path / "source" / "manju.db"
    src_db.parent.mkdir(parents=True)
    _build_source_db(src_db, custom_models_key="sk-plaintext-leak-marker")
    backup_dir = tmp_path / "backups"
    rc = backup_manju_db.run_backup(src_db, backup_dir, retries=1)
    assert rc == 0
    return src_db, backup_dir


def test_run_drill_reports_ok_and_positive_rto(backed_up, tmp_path, monkeypatch):
    _src_db, backup_dir = backed_up
    monkeypatch.setattr(restore_drill, "_known_provider_keys", lambda: {})  # 本用例不测明文扫描分支
    report = restore_drill.run_drill(backup_dir, tmp_path / "dest", scan_keys=False)
    assert report["verify"]["ok"] is True
    assert report["rto_seconds"] > 0
    assert report["restored_counts"]["projects"] == 1
    assert report["restored_counts"]["episodes"] == 3
    assert report["restored_counts"]["users"] == 1
    assert report["restored_counts"]["models"] == 1
    assert report["restored_counts"]["latest_audit_ts"] is not None
    assert report["ok"] is True


def test_run_drill_computes_row_deltas_against_live_db(backed_up, tmp_path):
    src_db, backup_dir = backed_up
    # 备份之后，"生产库"（这里复用 src_db 本身）又新增了 2 集——模拟备份点
    # 之后产生的新数据，row_deltas 必须能照出这个差。
    conn = sqlite3.connect(src_db)
    conn.execute("INSERT INTO episodes VALUES('ep_new1','p1')")
    conn.execute("INSERT INTO episodes VALUES('ep_new2','p1')")
    conn.commit()
    conn.close()

    report = restore_drill.run_drill(backup_dir, tmp_path / "dest", live_db=src_db, scan_keys=False)
    assert report["ok"] is True
    assert report["rpo"]["row_deltas"]["episodes"] == 2
    assert report["rpo"]["row_deltas"]["projects"] == 0
    assert report["rpo"]["elapsed_since_backup_s"] >= 0


def test_run_drill_detects_plaintext_key_leak(backed_up, tmp_path, monkeypatch):
    _src_db, backup_dir = backed_up
    monkeypatch.setattr(
        restore_drill, "_known_provider_keys",
        lambda: {"FAKE_PROVIDER_API_KEY": "sk-plaintext-leak-marker"},
    )
    report = restore_drill.run_drill(backup_dir, tmp_path / "dest", scan_keys=True)
    assert report["plaintext_key_hits"]["FAKE_PROVIDER_API_KEY"] == 1
    assert report["ok"] is False  # 明文命中必须让整体判定失败，不能只是个警告字段没人看


def test_run_drill_clean_when_key_not_present(backed_up, tmp_path, monkeypatch):
    _src_db, backup_dir = backed_up
    monkeypatch.setattr(
        restore_drill, "_known_provider_keys",
        lambda: {"UNRELATED_KEY": "this-string-does-not-appear-anywhere"},
    )
    report = restore_drill.run_drill(backup_dir, tmp_path / "dest", scan_keys=True)
    assert report["plaintext_key_hits"]["UNRELATED_KEY"] == 0
    assert report["ok"] is True


def test_run_drill_fails_closed_on_corrupted_backup(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    bad = backup_dir / "manju-latest.db.gz"
    bad.write_bytes(b"not a real gzip sqlite backup")
    report = restore_drill.run_drill(backup_dir, tmp_path / "dest", scan_keys=False)
    assert report["ok"] is False
    assert report["verify"]["ok"] is False


def test_resolve_source_prefers_latest_symlink_in_dir(backed_up):
    _src_db, backup_dir = backed_up
    resolved = restore_drill.resolve_source(backup_dir)
    assert resolved.name == "manju-latest.db.gz"


def test_resolve_source_raises_when_dir_has_no_latest(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        restore_drill.resolve_source(empty_dir)


def test_main_exit_code_reflects_verdict(backed_up, tmp_path, monkeypatch, capsys):
    _src_db, backup_dir = backed_up
    monkeypatch.setattr(restore_drill, "_known_provider_keys", lambda: {})
    rc = restore_drill.main(["--from", str(backup_dir), "--to", str(tmp_path / "dest2"), "--no-key-scan"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"ok": true' in out
