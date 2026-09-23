"""项目级设置（改编强度档位 / 画幅 / AI 标识）的读写实现——只读写 ``projects`` 表。

``conn`` 一律由调用方传入、无默认值（CLAUDE.md「所有权必须显式」：可选参数是缺陷的
温床）；写函数不 ``commit``，事务边界归调用方。这里只负责存取契约本身，「改编强度
具体怎么影响生成」是后续单元的事，不在本模块范围内。
"""
from __future__ import annotations

from typing import Any

#: 改编强度档位：faithful=忠实原文（存量项目默认，行为零变化）；
#: short_drama=短剧节奏（新建项目默认，2026-09-23 用户拍板）。
ADAPTATION_MODES: tuple[str, ...] = ("faithful", "short_drama")

#: 画幅：存量与新建项目都默认 "9:16"。
ASPECT_RATIOS: tuple[str, ...] = ("9:16", "16:9")

_CANVAS_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}


def canvas_size(aspect_ratio: str) -> tuple[int, int]:
    """画幅 -> 像素画布尺寸 ``(width, height)``；非法画幅 ``ValueError``。"""
    try:
        return _CANVAS_SIZES[aspect_ratio]
    except KeyError:
        raise ValueError(f"不支持的画幅：{aspect_ratio!r}") from None


def resolve_adaptation_mode(conn: Any, project_id: str) -> str:
    """读出项目的改编强度档位。

    项目不存在 -> ``LookupError``；库值不在 ``ADAPTATION_MODES`` 内 -> ``RuntimeError``
    （数据损坏，不是用户输入冲突，不能走全局 ``ValueError``->409 那条口径）。
    """
    row = conn.execute(
        "SELECT adaptation_mode FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    value = row["adaptation_mode"]
    if value not in ADAPTATION_MODES:
        raise RuntimeError(f"项目 {project_id} 的 adaptation_mode 数据损坏：{value!r}")
    return value


def resolve_aspect_ratio(conn: Any, project_id: str) -> str:
    """读出项目的画幅；语义同 ``resolve_adaptation_mode``。"""
    row = conn.execute(
        "SELECT aspect_ratio FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    value = row["aspect_ratio"]
    if value not in ASPECT_RATIOS:
        raise RuntimeError(f"项目 {project_id} 的 aspect_ratio 数据损坏：{value!r}")
    return value


def ai_label_enabled(conn: Any, project_id: str) -> bool:
    """读出项目的 AI 标识开关；项目不存在 -> ``LookupError``。"""
    row = conn.execute(
        "SELECT ai_label_enabled FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    return bool(row["ai_label_enabled"])


def update_project_settings(
    conn: Any,
    project_id: str,
    *,
    adaptation_mode: str | None,
    aspect_ratio: str | None,
    ai_label_enabled: bool | None,
) -> dict:
    """按传入字段部分更新项目设置，只更新非 ``None`` 的字段。

    非法值 -> ``ValueError``（中文 message）；项目不存在 -> ``LookupError``；调用方
    负责 ``commit``，本函数不提交（CLAUDE.md「不得在调用方的连接上隐式提交」）。
    """
    if adaptation_mode is not None and adaptation_mode not in ADAPTATION_MODES:
        raise ValueError(f"不支持的改编强度档位：{adaptation_mode!r}")
    if aspect_ratio is not None and aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"不支持的画幅：{aspect_ratio!r}")
    fields: list[str] = []
    params: list[Any] = []
    if adaptation_mode is not None:
        fields.append("adaptation_mode=?")
        params.append(adaptation_mode)
    if aspect_ratio is not None:
        fields.append("aspect_ratio=?")
        params.append(aspect_ratio)
    if ai_label_enabled is not None:
        fields.append("ai_label_enabled=?")
        params.append(int(ai_label_enabled))
    if fields:
        params.append(project_id)
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", params)
    row = conn.execute(
        "SELECT adaptation_mode, aspect_ratio, ai_label_enabled FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    return {
        "adaptation_mode": row["adaptation_mode"],
        "aspect_ratio": row["aspect_ratio"],
        "ai_label_enabled": bool(row["ai_label_enabled"]),
    }
