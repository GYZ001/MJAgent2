"""文件系统目录浏览（本机部署，供导出目录选择器使用）。

从 ``app/system_api.py`` 外移（2026-09-23，为声音生成模型接线腾出行数基线
余量）：这组函数与模型库/系统设置无关，纯粹是"路径在不在允许访问的目录树
下"这一件事，且没有测试按 ``system_api.<name>`` 之外的路径打桩过
（``app/capabilities/handlers/system.py`` 引用的 ``system_api.make_dir``
在外移后仍然可用，见 ``app/system_api.py`` 顶部的 re-import）。两个 FastAPI
路由（``GET /system/browse``、``POST /system/mkdir``）留在 ``app/system_api.py``
原地不动，只是内部调用这里的实现，因此路由挂的鉴权依赖
（``app.main`` 的 ``_PROJECT_OWNER_DEPS``）完全不受影响。
"""
from __future__ import annotations

import os
import re
import string
from pathlib import Path

from fastapi import HTTPException

from app import config

_BLOCKED_BROWSE_PREFIXES = (
    "/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/var/log",
    "/private/etc", "/private/var",
)


def _is_blocked_fs_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    text = str(resolved)
    lowered = text.lower()
    if any(text == prefix or text.startswith(prefix + os.sep) for prefix in _BLOCKED_BROWSE_PREFIXES):
        return True
    # 隐藏敏感家目录内容
    parts = {p.lower() for p in resolved.parts}
    if parts & {".ssh", ".gnupg", ".aws", ".kube", ".docker"}:
        return True
    if lowered.endswith(".env") or "/.env/" in lowered.replace("\\", "/"):
        return True
    return False


def _builtin_directory_roots() -> list[Path]:
    """默认可浏览/建目录根：仅项目与数据目录，不开放家目录枚举（Todolist T5）。"""
    return [config.PROJECTS_DIR.resolve(), config.DATA_DIR.resolve()]


def allowed_directory_roots() -> list[Path]:
    """可浏览/建目录的根路径集合。

    人工目录授权（``directory_grants`` 设置项、``POST /api/system/directory-
    grants``）已于 2026-09-01 退场：写入路由零调用点、零测试覆盖，且
    ``browse_dir`` 本就有独立的内置根（项目/数据目录）与敏感路径黑名单兜底，
    不依赖用户额外授权才能安全工作——见 CLAUDE.md「Retiring Features」。现在
    只剩内置根，浏览范围收紧到 Todolist T5 的默认安全边界。
    """
    return list(_builtin_directory_roots())


def assert_path_under_directory_grant(path: Path) -> Path:
    """路径必须落在内置根（项目/数据目录）之下。"""
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise HTTPException(400, f"路径不可解析：{path}") from exc
    if _is_blocked_fs_path(resolved):
        raise HTTPException(403, f"不允许访问系统敏感目录：{resolved}")
    for root in allowed_directory_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise HTTPException(403, f"路径不在允许访问的目录范围内：{resolved}")


def _list_drives() -> list[str]:
    if os.name != "nt":
        return []
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


def make_dir(body: dict) -> dict:
    """领域实现：仅允许在已授权 directory_grant 下创建子目录。"""
    parent = (body.get("path") or "").strip()
    name = (body.get("name") or "").strip()
    if not parent or not name:
        raise HTTPException(422, "缺少父目录或文件夹名")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(422, '文件夹名含非法字符（不能包含 \\ / : * ? " < > |）')
    if name in {".", ".."}:
        raise HTTPException(422, "文件夹名非法")
    parent_path = assert_path_under_directory_grant(Path(parent))
    if not parent_path.exists() or not parent_path.is_dir():
        raise HTTPException(404, f"父目录不存在：{parent}")
    dest = parent_path / name
    # 新建目标也必须仍在同一 grant 树下
    assert_path_under_directory_grant(dest)
    try:
        dest.mkdir(parents=False, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"创建失败：{e}")
    return {"path": str(dest)}
