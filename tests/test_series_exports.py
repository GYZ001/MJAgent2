"""连播任务台导出（``app.domain.series_ops.exports``）的回归。

不依赖真实 ffmpeg：直接在磁盘上伪造 ``film.mp4`` + ``film.report.json``（形状
照 ``merge.build_series_film`` 真实写出的那份），因为 exports.py 只关心「有没有
成片文件、report 里的 duration_s/chapters 是什么」，不关心视频内容本身是否
可播放——真实 ffmpeg 合并链路已经在 tests/test_series_film_merge.py 覆盖过。
"""
from __future__ import annotations

import json
import os
import time

import pytest
from fastapi import HTTPException

from app import config, db
from app.domain.series_ops import exports, merge, tasks


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "series-exports.db")
    monkeypatch.setattr(db._local, "conn", None, raising=False)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    db.init_db()
    conn = db.get_conn()
    conn.execute("INSERT INTO projects(id,name,created_at) VALUES('p1','P',0)")
    conn.executemany(
        "INSERT INTO episodes(id,project_id,episode_no,title,status,created_at) VALUES(?,?,?,?, 'planned', 0)",
        [(f"p1-ep{no}", "p1", no, f"第{no}集") for no in range(1, 11)],
    )
    conn.commit()
    return "p1"


def _make_task(project_id: str, episode_from: int, episode_to: int) -> str:
    conn = db.get_conn()
    tasks.generate_tasks(conn, project_id, {"ranges": [{"episode_from": episode_from, "episode_to": episode_to}]})
    row = conn.execute(
        "SELECT id FROM series_tasks WHERE project_id=? AND episode_from=? AND episode_to=?",
        (project_id, episode_from, episode_to),
    ).fetchone()
    return row["id"]


def _write_fake_film(project_id: str, episode_from: int, episode_to: int, *, duration_s: float = 12.5) -> None:
    out_dir = merge.series_film_dir(project_id, episode_from, episode_to)
    out_dir.mkdir(parents=True, exist_ok=True)
    film_path = out_dir / "film.mp4"
    film_path.write_bytes(b"fake-mp4-" + os.urandom(64))
    report = {
        "episode_from": episode_from, "episode_to": episode_to,
        "chapters": [{"episode_no": no, "start_s": 0.0, "duration_s": duration_s}
                     for no in range(episode_from, episode_to + 1)],
        "duration_s": duration_s, "size_bytes": film_path.stat().st_size,
        "created_at": time.time(), "input_fingerprints": [],
        "ffmpeg_command_summary": "fake-for-test",
    }
    (out_dir / "film.report.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def test_create_export_hardlinks_source_film_and_writes_manifest(project) -> None:
    project_id = project
    task1 = _make_task(project_id, 1, 3)
    task2 = _make_task(project_id, 4, 5)
    _write_fake_film(project_id, 1, 3, duration_s=30.0)
    _write_fake_film(project_id, 4, 5, duration_s=20.0)

    export = exports.create_export(project_id, [task1, task2])

    assert export["item_count"] == 2
    assert export["skipped"] == []
    assert export["total_size_bytes"] == sum(item["size_bytes"] for item in export["items"])

    export_dir = config.PROJECTS_DIR / project_id / "series" / "exports" / export["export_id"]
    assert (export_dir / "manifest.json").is_file()
    assert (export_dir / "下载清单.txt").is_file()

    source1 = merge.series_film_dir(project_id, 1, 3) / "film.mp4"
    linked1 = export_dir / export["items"][0]["file_name"]
    assert linked1.is_file()
    # 硬链接：同一 inode，st_nlink >= 2（源文件 + 导出目录里的这一份）。
    assert linked1.stat().st_ino == source1.stat().st_ino
    assert linked1.stat().st_nlink >= 2

    manifest_on_disk = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest_on_disk["export_id"] == export["export_id"]
    assert manifest_on_disk["item_count"] == 2

    list_lines = (export_dir / "下载清单.txt").read_text(encoding="utf-8").strip().splitlines()
    assert len(list_lines) == 2
    assert all(line for line in list_lines)


def test_create_export_file_names_are_zero_padded_and_range_aware(project) -> None:
    project_id = project
    task_multi = _make_task(project_id, 1, 3)
    task_single = _make_task(project_id, 4, 4)
    _write_fake_film(project_id, 1, 3)
    _write_fake_film(project_id, 4, 4)

    export = exports.create_export(project_id, [task_multi, task_single])
    names = {item["task_id"]: item["file_name"] for item in export["items"]}
    assert names[task_multi] == "第001-003集.mp4"
    assert names[task_single] == "第004集.mp4"


def test_create_export_skips_tasks_without_film_with_reason(project) -> None:
    project_id = project
    has_film = _make_task(project_id, 1, 2)
    no_film = _make_task(project_id, 3, 4)
    _write_fake_film(project_id, 1, 2)

    export = exports.create_export(project_id, [has_film, no_film])
    assert export["item_count"] == 1
    assert export["skipped"] == [{"task_id": no_film, "reason": "成片不存在，无法导出"}]


def test_create_export_unknown_task_id_skipped_with_reason(project) -> None:
    project_id = project
    has_film = _make_task(project_id, 1, 2)
    _write_fake_film(project_id, 1, 2)

    export = exports.create_export(project_id, [has_film, "st_does_not_exist"])
    assert export["item_count"] == 1
    assert export["skipped"] == [{"task_id": "st_does_not_exist", "reason": "任务不存在"}]


def test_create_export_all_unavailable_raises_422(project) -> None:
    project_id = project
    no_film = _make_task(project_id, 1, 2)

    with pytest.raises(HTTPException) as exc:
        exports.create_export(project_id, [no_film])
    assert exc.value.status_code == 422


def test_list_exports_returns_most_recent_first_and_caps_count(project, monkeypatch) -> None:
    project_id = project
    monkeypatch.setattr(exports, "_MAX_LISTED_EXPORTS", 2)

    export_ids = []
    for i in range(3):
        task_id = _make_task(project_id, i * 2 + 1, i * 2 + 2)
        _write_fake_film(project_id, i * 2 + 1, i * 2 + 2)
        result = exports.create_export(project_id, [task_id])
        export_ids.append(result["export_id"])
        time.sleep(0.01)  # 确保 created_at 严格递增，排序判据不依赖同一时间戳的稳定性

    listed = exports.list_exports(project_id)
    assert len(listed) == 2
    assert [e["export_id"] for e in listed] == list(reversed(export_ids))[:2]


def test_list_exports_empty_when_no_exports_dir(project) -> None:
    assert exports.list_exports(project) == []
