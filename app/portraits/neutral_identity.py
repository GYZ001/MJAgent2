"""中性身份定妆照（默认关闭，2026-09-30）——同一张脸的第二套定妆照：只锁体貌，
不烧服装/表情，供分镜生成时按每镜文字正面描述当前穿着，解决「参考图服装压过
正文」的问题（根因见 app.video_modes.seedance_reference_notes 模块 docstring）。

设计取自只读调查结论（2026-09-30）：
①中性定妆照提示词 = 同一套画风锁（``app.refs.character_visual_style_lock``）+
体貌专用锚点（模型申报、``app.production.storyboard_physical_anchor.
physical_anchor_verified`` 代码核验，核验不过直接报错，不兜底）+ 一段正面描述
的低显著度素衣 + 中性表情；以角色当前正面定妆照为种子图生图，保持同一张脸
（同 ``app.portraits.portrait_io._redraw_portrait`` 的调用形态，不改
``portrait_io.py`` 本身——该文件行数棘轮零余量，见其模块 docstring）。
②③ 预检 -> 暂存（``STAGED_INITIAL_EP_START`` 候选槽位）-> 采纳三段式，沿用
``app.domain.bible_ops.precheck.compute_refs_precheck`` 的「预检返回指纹 ->
确认时回传同一指纹，漂移则拒绝」模式，但不复用它的 ``character_payment_quotes``
持久化表——本功能的确认窗口极短（用户在同一次交互里看完预检就点采纳),
无状态指纹重算/比对已经能做到「范围变化即拒绝」，不需要额外一张跨请求持久化
的凭证表。
采纳把旧行用 ``portrait_coverage_guard._close_portrait_segment`` 收到
``from_episode-1``，新行开区间生效；``appearance`` 列在整个流程中保持人物谱
完整外观文本不变——中性只影响定妆照本身（``prompt``/``image_path``）与
``costume_mode`` 标记，不影响其它系统读到的角色外观权威文本。

生成侧接线（2026-09-30 补）：``costume_mode`` 从 ``character_portraits`` 一路带到
真实生成请求——``app.video_modes.asset_lookup.character_reference_assets`` 查询
时一并带出，``app.video_modes.mode_selection.ReferenceImageAsset`` 新增同名字段
随 ``public_dict()`` 透传，最终喂给 ``seedance_reference_notes`` 切换文案；此前
只改了 ``seedance_reference_notes`` 本身的文案分支，没接上产线唯一的参考图构造
入口，中性定妆照在真实生成时形同虚设（只读复核已实测复现并修复）。

不实现（本次范围外，按 CLAUDE.md「明确本次不实现的功能」）：前端入口；对存量
项目自动批量迁移。侧视角单独重做（``app.multiview.regenerate_character_view``）
不支持中性分支，命中时直接报错（不静默按常规着装合同重做，CLAUDE.md「缺失要有
可见信号」）——真正支持中性分支是独立入口，留作后续。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app import config, hiagent
from app.db import new_id, now
from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.multiview import CHARACTER_REQUIRED_VIEWS, ensure_character_multiview_pack
from app.production.storyboard_physical_anchor import physical_anchor_verified
from app.project_settings import mark_portrait_costume_mode_neutral
from app.refs import character_visual_style_lock, normalize_prompt_text, production_appearance_anchor

from ._db_probe import _has_column
from .constants import STAGED_INITIAL_EP_START
from .portrait_coverage_guard import _close_portrait_segment, _warn_if_portrait_coverage_lost
from .portrait_io import _new_portrait_path, _open_portrait, _save_image_item

_NEUTRAL_WARDROBE = (
    "上身穿纯浅灰色圆领长袖针织打底衫，下身穿深灰色直筒长裤，脚穿素色平底鞋，"
    "颈部与手腕自然裸露，不佩戴任何配饰"
)
_NEUTRAL_EXPRESSION = "面部肌肉自然放松，双唇轻轻闭合，呈中性表情，目光平视前方"


def _ensure_costume_mode_column(conn) -> None:
    """懒迁移，幂等；不在调用方连接上隐式 commit（同 portrait_lookup.py
    ``_ensure_anchor_key_column`` 的写法，CLAUDE.md「不得在调用方的连接上
    隐式提交」）。"""
    if _has_column(conn, "character_portraits", "costume_mode"):
        return
    conn.execute(
        "ALTER TABLE character_portraits ADD COLUMN costume_mode TEXT NOT NULL DEFAULT 'baked'"
    )


class _NeutralPhysicalAnchor(BaseModel):
    name: str = Field(min_length=1)
    physical_description: str = Field(min_length=1)


class _NeutralPhysicalAnchors(BaseModel):
    anchors: list[_NeutralPhysicalAnchor] = Field(default_factory=list)


def _physical_anchor_prompt(appearance_by_name: dict[str, str]) -> str:
    lines = [f"角色「{n}」完整外观锚点：{a}" for n, a in appearance_by_name.items()]
    return (
        "为下列每个角色申报一份体貌专用锚点 physical_description：只保留脸型/五官"
        "轮廓、发型发色、体型身高、年龄区间这几类不随场次改变的体貌特征，必须从"
        "完整外观锚点原文里逐字摘取、按原有先后顺序删减而成（只删不改、不新增一个"
        "字，可以跳过中间不要的部分），服装/配饰款式颜色、默认表情都不属于体貌"
        "特征，不要保留。\n\n" + "\n".join(lines) + "\n\n只输出一个 JSON 对象："
        '{"anchors": [{"name": "角色名", "physical_description": "..."}]}'
    )


def _physical_anchor_nomination_errors(
    draft: _NeutralPhysicalAnchors, appearance_by_name: dict[str, str],
) -> list[str]:
    nominated = {a.name: a.physical_description for a in draft.anchors}
    errors = [f"缺少角色「{n}」的体貌专用锚点申报" for n in appearance_by_name if n not in nominated]
    for name, anchor in appearance_by_name.items():
        description = nominated.get(name)
        if description and not physical_anchor_verified(description, anchor):
            errors.append(f"角色「{name}」的体貌专用锚点不是完整外观锚点的有效删减")
    return errors


async def _nominate_physical_anchors(appearance_by_name: dict[str, str]) -> dict[str, str]:
    """模型申报体貌专用锚点，代码核验（``physical_anchor_verified``），核验不过
    直接把错误抛给用户，不兜底（CLAUDE.md「不得兜底填充」）。"""
    fingerprint = hashlib.sha256(
        json.dumps(appearance_by_name, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    try:
        draft = await model_gateway.chat_structured(
            [
                {"role": "system", "content": "你是人物设定助手。只输出符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释。"},
                {"role": "user", "content": _physical_anchor_prompt(appearance_by_name)},
            ],
            model_type=_NeutralPhysicalAnchors,
            validate=lambda value: _physical_anchor_nomination_errors(value, appearance_by_name),
            operation_id=f"neutral_identity_physical_anchor_{fingerprint}",
            max_tokens=1200,
            temperature=0.1,
            call_meta={"stage_key": "neutral_identity_physical_anchor", "call_role": "neutral_identity"},
        )
    except model_gateway.StructuredOutputError as exc:
        raise ContentGenerationError(
            f"体貌专用锚点核验失败，请人工检查角色外观锚点后重试：{exc}"
        ) from exc
    return {a.name: a.physical_description for a in draft.anchors}


def neutral_portrait_prompt(visual_style: str, appearance_canonical: str, physical_description: str) -> str:
    """中性定妆照提示词：画风锁 + 体貌专用锚点（核验不过 ``ValueError``，不兜底）
    + 低显著度素衣 + 中性表情；不包含原锚点里的服装款式或默认表情文本。
    """
    if not physical_anchor_verified(physical_description, appearance_canonical):
        raise ValueError(f"体貌专用锚点核验失败，不是完整外观锚点的有效删减：{physical_description!r}")
    body = production_appearance_anchor(physical_description)
    style = character_visual_style_lock(visual_style)
    return normalize_prompt_text(
        f"{style}。单角色中性定妆照：{body}。"
        f"着装：{_NEUTRAL_WARDROBE}。表情：{_NEUTRAL_EXPRESSION}。"
        "完整遵循体貌锚点声明的实体形态与空间关系，采用正面中性展示姿态。"
        "纯浅米色背景，全身完整可见。头顶、肩臂和鞋底均不得贴边或出画，"
        "主体四周保留至少 8% 安全边距。"
        "不得添加以上体貌、着装与表情描述之外的服装、配饰或视觉元素"
    )


def _affected_segments(conn, project_id: str, name: str, from_episode: int) -> list[int]:
    """``from_episode`` 起已生成分镜且提到该角色的集号——只读展示用，判据是
    ``shots.characters``（JSON 文本）按带引号的整词包含，不驱动任何写路径的
    失效判定（真正的素材陈旧判定见 app.domain.storyboard_ops.staleness）。"""
    quoted = json.dumps(name, ensure_ascii=False)
    rows = conn.execute(
        "SELECT DISTINCT e.episode_no FROM shots s JOIN episodes e ON e.id = s.episode_id "
        "WHERE e.project_id=? AND e.episode_no>=? AND s.characters LIKE '%' || ? || '%' "
        "ORDER BY e.episode_no",
        (project_id, from_episode, quoted),
    ).fetchall()
    return [int(r["episode_no"]) for r in rows]


def _precheck_fingerprint(project_id: str, from_episode: int, characters: dict[str, str]) -> str:
    material = json.dumps(
        {"project_id": project_id, "from_episode": from_episode, "characters": characters},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _bible_characters(conn, project_id: str) -> dict[str, dict]:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        raise LookupError(f"项目不存在：{project_id}")
    bible = json.loads(row["bible_json"] or "{}")
    return {str(c.get("name") or ""): c for c in bible.get("characters") or [] if c.get("name")}


def precheck_neutral_identity(conn, project_id: str, names: list[str], from_episode: int) -> dict:
    """只读预检：要生成的图片张数、``from_episode`` 起哪些已采用段会被标记
    资产变更，带指纹——沿用「预检 -> 确认」模式，见模块 docstring。
    """
    by_name = _bible_characters(conn, project_id)
    appearance_by_name: dict[str, str] = {}
    characters: list[dict[str, Any]] = []
    for name in names:
        character = by_name.get(name)
        anchor = str((character or {}).get("appearance_canonical") or "").strip()
        if not character or not anchor:
            raise ValueError(f"角色「{name}」不在本项目人物谱中，或没有可用的外观锚点")
        appearance_by_name[name] = anchor
        characters.append({
            "name": name, "image_count": len(CHARACTER_REQUIRED_VIEWS),
            "affected_segments": _affected_segments(conn, project_id, name, from_episode),
        })
    return {
        "project_id": project_id, "from_episode": from_episode, "characters": characters,
        "image_count": sum(c["image_count"] for c in characters),
        "fingerprint": _precheck_fingerprint(project_id, from_episode, appearance_by_name),
    }


async def _generate_neutral_front_image(
    project_id: str, character_name: str, prompt: str, base_path: str | None,
) -> str:
    """图生图：以角色当前正面定妆照为种子，只按中性提示词重绘（同
    ``portrait_io._redraw_portrait`` 的调用形态，不改该文件本身——它的行数
    棘轮零余量，见其模块 docstring）。返回落盘路径。"""
    image_inputs = None
    if base_path and Path(base_path).exists():
        image_inputs = [hiagent.data_url_from_file(base_path)]
    item = await hiagent.generate_image(
        prompt, size=config.REF_IMAGE_SIZE, image_inputs=image_inputs,
        call_meta={
            "asset_kind": "portrait", "character_name": character_name,
            "portrait_mode": "neutral_identity",
        },
    )
    dest = _new_portrait_path(project_id, character_name, STAGED_INITIAL_EP_START)
    await _save_image_item(item, dest)
    return dest


def _project_bible_version(conn, project_id: str) -> int:
    row = conn.execute("SELECT bible_version FROM projects WHERE id=?", (project_id,)).fetchone()
    return int(row["bible_version"] or 0) if row and row["bible_version"] is not None else 0


def _insert_staged_neutral_row(
    conn, project_id: str, name: str, appearance: str, prompt: str, image_path: str,
    *, base_portrait_id: str | None, bible_version: int,
) -> str:
    """暂存候选：复用 ``STAGED_INITIAL_EP_START`` 哨兵槽位（同
    ``portrait_io.stage_initial_portrait`` 的形态），先清掉该角色既有的暂存候选
    （不影响当前已采用行）；不 commit，调用方负责事务边界。"""
    conn.execute(
        "DELETE FROM character_portraits WHERE project_id=? AND character_name=? AND ep_start=?",
        (project_id, name, STAGED_INITIAL_EP_START),
    )
    portrait_id = new_id("portrait")
    cols = ["id", "project_id", "character_name", "ep_start", "ep_end", "appearance", "prompt",
            "image_path", "base_portrait_id", "bible_version", "costume_mode", "created_at"]
    vals: list[Any] = [portrait_id, project_id, name, STAGED_INITIAL_EP_START, None, appearance,
                        prompt, image_path, base_portrait_id, bible_version, "neutral", now()]
    if _has_column(conn, "character_portraits", "pack_status"):
        cols.append("pack_status")
        vals.append("legacy_partial")
    placeholders = ",".join("?" * len(vals))
    conn.execute(f"INSERT INTO character_portraits({','.join(cols)}) VALUES({placeholders})", vals)
    return portrait_id


async def _stage_one_character(conn, project_id: str, name: str, style: str, appearance: str) -> dict:
    physical = (await _nominate_physical_anchors({name: appearance}))[name]
    prompt = neutral_portrait_prompt(style, appearance, physical)
    base_row = _open_portrait(conn, project_id, name)
    base_path = base_row["image_path"] if base_row else None
    image_path = await _generate_neutral_front_image(project_id, name, prompt, base_path)
    portrait_id = _insert_staged_neutral_row(
        conn, project_id, name, appearance, prompt, image_path,
        base_portrait_id=base_row["id"] if base_row else None,
        bible_version=_project_bible_version(conn, project_id),
    )
    conn.commit()
    pack = await ensure_character_multiview_pack(
        project_id=project_id, portrait_id=portrait_id, character_name=name,
        appearance=appearance, visual_style=style, portrait_prompt=prompt,
        ep_start=STAGED_INITIAL_EP_START, base_portrait_id=base_row["id"] if base_row else None,
        primary_qa={}, costume_mode="neutral",
    )
    return {"name": name, "portrait_id": portrait_id, "image_path": image_path, "pack_status": pack.get("status")}


async def stage_neutral_identity(
    conn, project_id: str, names: list[str], from_episode: int, *, fingerprint: str,
) -> dict:
    """预检确认后暂存：逐个角色生成中性定妆照 + 多视角包，写入
    ``STAGED_INITIAL_EP_START`` 候选槽位；不提升为当前造型（见
    ``adopt_neutral_identity``），不影响任何一集实际会用到的定妆照。"""
    current = precheck_neutral_identity(conn, project_id, names, from_episode)
    if current["fingerprint"] != fingerprint:
        raise ValueError("范围预检已过期或范围已变化，请重新调用 precheck 后再暂存")
    _ensure_costume_mode_column(conn)
    by_name = _bible_characters(conn, project_id)
    proj = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    style = (json.loads(proj["bible_json"] or "{}").get("world") or {}).get("visual_style_canonical") or ""
    staged = [
        await _stage_one_character(conn, project_id, name, style, by_name[name]["appearance_canonical"])
        for name in names
    ]
    return {"project_id": project_id, "from_episode": from_episode, "staged": staged}


def _delete_row_superseded_by_open_start(
    conn, *, character_name: str, row, from_episode: int, reason: str,
) -> None:
    """物理删除 ``row``——不经 ``portrait_coverage_guard._delete_portrait_segment``
    （它的文档字符串明确要求「删除前该行已不覆盖任何集」，调用方是事故复盘后立的
    安全契约，不能借用）。这里的安全前提不同但同样成立：调用方保证会有一张新行
    从 ``from_episode`` 起开区间（``ep_end`` 恒为 NULL）生效；只要
    ``row["ep_start"]>=from_episode``，新行的覆盖范围就是 ``row`` 整段区间的
    严格超集，物理删除不产生覆盖缺口。前提不成立直接报错，不静默继续（同样复用
    ``_warn_if_portrait_coverage_lost`` 产出可见信号）。"""
    if int(row["ep_start"] or 0) < from_episode:
        raise ValueError(
            f"内部不变量被破坏：定妆照分段 ep_start={row['ep_start']!r} 早于新行起点 "
            f"{from_episode}，不能按「整段被新行取代」处理"
        )
    conn.execute("DELETE FROM character_portraits WHERE id=?", (row["id"],))
    _warn_if_portrait_coverage_lost(
        character_name=character_name, portrait_id=row["id"],
        before_ep_start=row["ep_start"], before_ep_end=row["ep_end"],
        after_ep_start=None, after_ep_end=None, deleted=True,
        caller_episode_no=from_episode, reason=reason,
    )


def adopt_neutral_identity(conn, project_id: str, character_name: str, from_episode: int) -> dict:
    """把暂存的中性定妆候选转正：旧行收到 ``from_episode-1``，候选行从
    ``from_episode`` 起开区间生效；``appearance`` 沿用暂存时写入的人物谱完整
    外观文本，不做任何改写。首次为该项目采纳时把项目级 ``portrait_costume_mode``
    标记翻到 neutral（见 app.project_settings）。

    ``from_episode`` 可能撞上该角色任意一行（当前开区间行，或任意一段已关闭的
    历史分段）的 ``ep_start``——不只检查当前开区间行：历史分段一样会撞
    ``UNIQUE(project_id,character_name,ep_start)``。两种情况处理方式相同（见
    ``_delete_row_superseded_by_open_start``），确认预检未覆盖、不兜底吞掉。"""
    if from_episode < 1:
        raise ValueError("from_episode 必须是真实集号（>=1）")
    _ensure_costume_mode_column(conn)
    staged = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND ep_start=?",
        (project_id, character_name, STAGED_INITIAL_EP_START),
    ).fetchone()
    if not staged or staged["costume_mode"] != "neutral":
        raise ValueError(f"角色「{character_name}」没有待采纳的中性定妆候选，请先调用 stage")
    if not staged["image_path"] or not Path(staged["image_path"]).exists():
        raise ValueError(f"角色「{character_name}」的中性定妆候选图片文件不可用")
    current = _open_portrait(conn, project_id, character_name)
    current_id = current["id"] if current else None
    historical_conflict = conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? "
        "AND ep_start=? AND id IS NOT ?",
        (project_id, character_name, from_episode, current_id),
    ).fetchone()
    if historical_conflict is not None:
        _delete_row_superseded_by_open_start(
            conn, character_name=character_name, row=historical_conflict, from_episode=from_episode,
            reason="neutral_identity_adopt_ep_start_collision",
        )
    if current and int(current["ep_start"] or 0) >= from_episode:
        # 旧行的 ep_start 落在新行起点之后（含相等）：收窄会把 ep_end 压到比
        # ep_start 还小、且与新行撞上 UNIQUE(project_id,character_name,ep_start)；
        # 旧行整段被新行完全取代，直接删除。
        _delete_row_superseded_by_open_start(
            conn, character_name=character_name, row=current, from_episode=from_episode,
            reason="neutral_identity_adopt_superseded",
        )
    elif current:
        _close_portrait_segment(
            conn, character_name=character_name, portrait_id=current["id"],
            before_ep_start=current["ep_start"], before_ep_end=current["ep_end"],
            new_ep_end=from_episode - 1, caller_episode_no=from_episode,
            reason="neutral_identity_adopt",
        )
    conn.execute(
        "UPDATE character_portraits SET ep_start=?, ep_end=NULL WHERE id=?",
        (from_episode, staged["id"]),
    )
    mark_portrait_costume_mode_neutral(conn, project_id)
    conn.commit()
    return {"project_id": project_id, "character_name": character_name,
            "portrait_id": staged["id"], "ep_start": from_episode}
