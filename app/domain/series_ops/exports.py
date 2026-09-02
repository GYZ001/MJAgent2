"""打包导出：勾选已完成任务 → 硬链接 + manifest + 下载清单，不打 zip。

不打 zip 的理由见 docs/series_task_console_plan.md「导出为什么不打 zip」：
单集成片 40-400MB，zip 会把磁盘占用翻倍且 mp4 已压缩、zip 压缩率≈0。导出目录
``projects/{pid}/series/exports/{export_id}/`` 下是硬链接（同一文件系统零拷贝）+
``manifest.json``（Export 对象持久化形态）+ ``下载清单.txt``（每行一个可下载
URL，能直接喂给 aria2/wget -i）。

已知限制：``下载清单.txt`` 是**生成那一刻**写死的静态文件，里面的 ``mt=`` 票据
按天分桶（``app.media_urls``，只认今天与昨天两桶）。接口返回的 ``items[].url``
每次读取都重签（见 ``_resign_urls``），这个 txt 里的不会——它的用法是「导出后
立刻下载、马上喂给下载工具」，隔天再用需要重新点一次导出。之所以不改成动态
生成的接口：``<a href>`` 下载不会带 ``X-Manju-Session`` 头，`/api` 路由服务不了
这个场景，URL 里带票据的 `/media` 静态文件才行。等 ``MJ_MEDIA_REQUIRE_TICKET``
真正打开时，这一条是要一起处理的。
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import uuid
from pathlib import Path

from fastapi import HTTPException

from app import config
from app.db import get_conn, now
from app.media_urls import build_media_url

from . import merge, tasks

_MAX_LISTED_EXPORTS = 20


def _exports_root(project_id: str) -> Path:
    return config.PROJECTS_DIR / project_id / "series" / "exports"


def _new_export_id() -> str:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _file_name(episode_from: int, episode_to: int) -> str:
    if episode_from == episode_to:
        return f"第{episode_from:03d}集.mp4"
    return f"第{episode_from:03d}-{episode_to:03d}集.mp4"


def _collect_items(conn, project_id: str, task_ids: list[str]) -> tuple[list[tuple[dict, dict]], list[dict]]:
    items: list[tuple[dict, dict]] = []
    skipped: list[dict] = []
    for task_id in dict.fromkeys(task_ids):
        row = tasks.get_task_row(conn, project_id, task_id)
        if row is None:
            skipped.append({"task_id": task_id, "reason": "任务不存在"})
            continue
        film = merge.film_for_range(project_id, row["episode_from"], row["episode_to"])
        if film is None:
            skipped.append({"task_id": task_id, "reason": "成片不存在，无法导出"})
            continue
        items.append((row, film))
    return items, skipped


def _link_one(export_dir: Path, row: dict, film: dict) -> dict:
    source = config.PROJECTS_DIR / film["path"]
    file_name = _file_name(row["episode_from"], row["episode_to"])
    target = export_dir / file_name
    os.link(str(source), str(target))
    stat = target.stat()
    url = build_media_url(str(target), version=f"{stat.st_mtime_ns}-{stat.st_size}")
    return {
        "task_id": row["id"],
        "title": row["title"] or (
            f"第 {row['episode_from']} 集" if row["episode_from"] == row["episode_to"]
            else f"第 {row['episode_from']}-{row['episode_to']} 集"
        ),
        "episode_from": row["episode_from"], "episode_to": row["episode_to"],
        "file_name": file_name, "url": url,
        "size_bytes": int(stat.st_size), "duration_s": float(film.get("duration_s") or 0.0),
    }


def _build_export_dir(
    project_id: str, export_id: str, items: list[tuple[dict, dict]],
) -> tuple[Path, list[dict], int]:
    export_dir = _exports_root(project_id) / export_id
    export_dir.mkdir(parents=True, exist_ok=False)
    try:
        built = [_link_one(export_dir, row, film) for row, film in items]
    except OSError as exc:
        shutil.rmtree(export_dir, ignore_errors=True)
        raise HTTPException(500, f"导出失败（硬链接创建失败）：{exc}") from exc
    total_size = sum(item["size_bytes"] for item in built)
    return export_dir, built, total_size


def create_export(project_id: str, task_ids: list[str]) -> dict:
    conn = get_conn()
    items, skipped = _collect_items(conn, project_id, task_ids)
    if not items:
        raise HTTPException(422, "勾选的任务都没有可导出的成片")
    export_id = _new_export_id()
    export_dir, built_items, total_size = _build_export_dir(project_id, export_id, items)
    manifest_path = export_dir / "manifest.json"
    list_path = export_dir / "下载清单.txt"
    export = {
        "export_id": export_id, "created_at": now(),
        "total_size_bytes": total_size, "item_count": len(built_items),
        "manifest_url": build_media_url(str(manifest_path)),
        "list_url": build_media_url(str(list_path)),
        "items": built_items, "skipped": skipped,
    }
    manifest_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")
    list_path.write_text("\n".join(item["url"] for item in built_items) + "\n", encoding="utf-8")
    # manifest_url/list_url 里的 mtime 版本号依赖文件已经落盘，写完文件后重取一次。
    export["manifest_url"] = build_media_url(
        str(manifest_path), version=f"{manifest_path.stat().st_mtime_ns}",
    )
    export["list_url"] = build_media_url(str(list_path), version=f"{list_path.stat().st_mtime_ns}")
    return export


def _resign_urls(export_dir: Path, export: dict) -> dict:
    """按盘上现状重签这个导出包里的全部 ``/media`` URL。

    ``manifest.json`` 里持久化的 URL 带的是**生成那天**的 ``mt=`` 票据，而
    ``app.media_urls`` 的票据按天分桶、只认今天与昨天两桶——直接把持久化的那份
    返给前端，导出包放过一天就是一串过期票据，等 ``MJ_MEDIA_REQUIRE_TICKET``
    打开就整包 403。URL 必须在**读取时**按路径重新签发，不能当成静态数据存。
    """
    items = []
    for item in export.get("items") or []:
        path = export_dir / str(item.get("file_name") or "")
        version = None
        if path.is_file():
            stat = path.stat()
            version = f"{stat.st_mtime_ns}-{stat.st_size}"
        items.append({**item, "url": build_media_url(str(path), version=version)})
    export["items"] = items
    export["manifest_url"] = build_media_url(str(export_dir / "manifest.json"))
    export["list_url"] = build_media_url(str(export_dir / "下载清单.txt"))
    return export


def list_exports(project_id: str) -> list[dict]:
    root = _exports_root(project_id)
    if not root.is_dir():
        return []
    entries = []
    for child in root.iterdir():
        manifest_path = child / "manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            entries.append(_resign_urls(child, json.loads(manifest_path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    entries.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
    return entries[:_MAX_LISTED_EXPORTS]
