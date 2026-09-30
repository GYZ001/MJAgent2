"""项目级设置（改编强度档位 / 画幅 / AI 标识 / 统一配乐及预留开关 / 旁白固定音色）
的读写实现——只读写 ``projects`` 表。

``conn`` 一律由调用方传入、无默认值（CLAUDE.md「所有权必须显式」：可选参数是缺陷的
温床）；写函数不 ``commit``，事务边界归调用方。这里只负责存取契约本身，「改编强度
具体怎么影响生成」是后续单元的事，不在本模块范围内。

2026-09-28 新增三个布尔开关：``enhance_music_bed``（统一配乐，本次由分镜台消费，见
``app.production.storyboard_music_bed``）、``enhance_teaser``（片头预告）、
``enhance_monologue``（主角内心独白）——后两项本次只加开关本身，暂无消费方，为
下一个任务预留同一处存取契约，避免它再动这段代码；三项都默认关闭。

2026-09-28 再新增 ``narrator_voice_character``（旁白固定音色角色，真实回归
《顾念长安》第 1 集驱动：旁白每段随机配声音，前后不一致）：值必须是本项目人物谱
（``projects.bible_json`` 的 ``characters[].name``）里实际存在的正名，空串＝保持
现状（旁白不挂固定参考音频，行为逐字不变）。合法值校验直接解析 ``bible_json`` 原始
JSON（不经 ``app.schemas.Bible``），保持本模块「零 app.* 依赖」的既有约束——见
``app.LAYERS.toml`` 对 ``app.project_settings`` 的层号注释。
"""
from __future__ import annotations

import json
from typing import Any

#: 改编强度档位：faithful=忠实原文（存量项目默认，行为零变化）；
#: short_drama=短剧节奏（新建项目默认，2026-09-23 用户拍板）。
ADAPTATION_MODES: tuple[str, ...] = ("faithful", "short_drama")

#: 画幅：存量与新建项目都默认 "9:16"。
ASPECT_RATIOS: tuple[str, ...] = ("9:16", "16:9")

#: 定妆照着装模式：baked=常规（服装写进定妆照，存量项目默认，行为零变化）；
#: neutral=中性（定妆照只保留体貌，服装/表情按每镜文字正面描述，2026-09-30
#: 新增，见 app.portraits.neutral_identity）。项目级标记只读，写入由
#: mark_portrait_costume_mode_neutral 在首个角色采纳中性定妆照后设置。
PORTRAIT_COSTUME_MODES: tuple[str, ...] = ("baked", "neutral")

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


#: 写进生图提示词的画幅短语；"9:16" 与改造前写死的文字逐字相同（存量竖屏项目的
#: 场景图提示词与幂等指纹不变）。
_CANVAS_PHRASES: dict[str, str] = {"9:16": "9:16 竖屏", "16:9": "16:9 横屏"}


def canvas_phrase(aspect_ratio: str) -> str:
    """画幅 -> 生图提示词里的画幅短语；非法画幅 ``ValueError``（与 ``canvas_size`` 同口径）。"""
    try:
        return _CANVAS_PHRASES[aspect_ratio]
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


def enhance_music_bed_enabled(conn: Any, project_id: str) -> bool:
    """读出项目的统一配乐开关；项目不存在 -> ``LookupError``。语义同 ``ai_label_enabled``。"""
    row = conn.execute(
        "SELECT enhance_music_bed FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    return bool(row["enhance_music_bed"])


def enhance_teaser_enabled(conn: Any, project_id: str) -> bool:
    """读出项目的片头预告开关（本次只加开关，暂无消费方）；项目不存在 -> ``LookupError``。"""
    row = conn.execute(
        "SELECT enhance_teaser FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    return bool(row["enhance_teaser"])


def enhance_monologue_enabled(conn: Any, project_id: str) -> bool:
    """读出项目的主角内心独白开关（本次只加开关，暂无消费方）；项目不存在 -> ``LookupError``。"""
    row = conn.execute(
        "SELECT enhance_monologue FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    return bool(row["enhance_monologue"])


def resolve_narrator_voice_character(conn: Any, project_id: str) -> str:
    """读出项目的旁白固定音色角色正名；空串表示未设置（保持现状）。项目不存在 ->
    ``LookupError``，语义同 ``ai_label_enabled``。"""
    row = conn.execute(
        "SELECT narrator_voice_character FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    return str(row["narrator_voice_character"] or "")


def resolve_portrait_costume_mode(conn: Any, project_id: str) -> str:
    """读出项目的定妆照着装模式；语义同 ``resolve_aspect_ratio``。"""
    row = conn.execute(
        "SELECT portrait_costume_mode FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    value = row["portrait_costume_mode"]
    if value not in PORTRAIT_COSTUME_MODES:
        raise RuntimeError(f"项目 {project_id} 的 portrait_costume_mode 数据损坏：{value!r}")
    return value


def mark_portrait_costume_mode_neutral(conn: Any, project_id: str) -> None:
    """中性定妆照工作流采纳首个角色后把项目级标记翻到 neutral；幂等，调用方提交
    （CLAUDE.md「不得在调用方的连接上隐式提交」）。"""
    conn.execute(
        "UPDATE projects SET portrait_costume_mode='neutral' "
        "WHERE id=? AND portrait_costume_mode<>'neutral'",
        (project_id,),
    )


def _bible_character_names(conn: Any, project_id: str) -> list[str]:
    """本项目人物谱里的正名列表；``bible_json`` 缺失/未生成/解析失败都返回空列表
    （诚实——人物谱还不存在时任何名字都不合法，不是校验被绕过）。只解析裸 JSON，
    不经 ``app.schemas.Bible``（本模块零 app.* 依赖）。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None or not row["bible_json"]:
        return []
    try:
        data = json.loads(row["bible_json"])
    except (TypeError, ValueError):
        return []
    return [
        str(c.get("name") or "").strip()
        for c in (data.get("characters") or [])
        if isinstance(c, dict) and str(c.get("name") or "").strip()
    ]


def update_project_settings(
    conn: Any,
    project_id: str,
    *,
    adaptation_mode: str | None,
    aspect_ratio: str | None,
    ai_label_enabled: bool | None,
    enhance_music_bed: bool | None,
    enhance_teaser: bool | None,
    enhance_monologue: bool | None,
    narrator_voice_character: str | None,
) -> dict:
    """按传入字段部分更新项目设置，只更新非 ``None`` 的字段。

    非法值 -> ``ValueError``（中文 message）；项目不存在 -> ``LookupError``；调用方
    负责 ``commit``，本函数不提交（CLAUDE.md「不得在调用方的连接上隐式提交」）。
    ``narrator_voice_character`` 传空串表示显式清空（恢复「保持现状」）；传非空值
    时必须命中本项目人物谱的正名，否则拒绝写入。
    """
    if adaptation_mode is not None and adaptation_mode not in ADAPTATION_MODES:
        raise ValueError(f"不支持的改编强度档位：{adaptation_mode!r}")
    if aspect_ratio is not None and aspect_ratio not in ASPECT_RATIOS:
        raise ValueError(f"不支持的画幅：{aspect_ratio!r}")
    narrator_name = narrator_voice_character.strip() if narrator_voice_character is not None else None
    if narrator_name and narrator_name not in _bible_character_names(conn, project_id):
        raise ValueError(f"角色「{narrator_name}」不在本项目人物谱中，无法设为旁白音色角色")
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
    if enhance_music_bed is not None:
        fields.append("enhance_music_bed=?")
        params.append(int(enhance_music_bed))
    if enhance_teaser is not None:
        fields.append("enhance_teaser=?")
        params.append(int(enhance_teaser))
    if enhance_monologue is not None:
        fields.append("enhance_monologue=?")
        params.append(int(enhance_monologue))
    if narrator_name is not None:
        fields.append("narrator_voice_character=?")
        params.append(narrator_name)
    if fields:
        params.append(project_id)
        conn.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id=?", params)
    row = conn.execute(
        "SELECT adaptation_mode, aspect_ratio, ai_label_enabled, enhance_music_bed, "
        "enhance_teaser, enhance_monologue, narrator_voice_character FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    return {
        "adaptation_mode": row["adaptation_mode"],
        "aspect_ratio": row["aspect_ratio"],
        "ai_label_enabled": bool(row["ai_label_enabled"]),
        "enhance_music_bed": bool(row["enhance_music_bed"]),
        "enhance_teaser": bool(row["enhance_teaser"]),
        "narrator_voice_character": str(row["narrator_voice_character"] or ""),
        "enhance_monologue": bool(row["enhance_monologue"]),
    }
