"""人物谱手动新增角色 / 替换定妆照（2026-08-31 用户拍板：「还是继续放在人物谱和
场景库吧，给用户一个动手添加的能力，图像描述都让用户自己填写上传」）。

比既有的提名入口（``nominate.py``）更进一步——name / appearance_canonical /
period_costume_canonical / 定妆照全部由用户提供，不走模型评估、不检索原文证据。
但去重判据不允许绕开：新增前必须先过 ``app.portraits.card_owner.
resolve_card_owner`` 这唯一权威解析器，命中已有角色就报出归属者、不新建，命中
歧义就 fail closed 列出候选（同 nominate.py 的三态路由语义，本文件不新写第二套
匹配逻辑）。

落库复用两条既有机制，不新造平行实现：
- 新增：``app.portraits._append_character_to_bible``（bible.characters 追加 +
  版本号推进 + artifact 血缘）与 ``app.portraits.register_initial_portrait``
  （character_portraits 首次登记，同生成路径同一张表同一查询形状）。
- 替换：``app.portraits.stage_initial_portrait`` 暂存候选 + ``app.portraits.
  promote_staged_initial_portrait``（已有成熟形态：旧记录压进负数 ``ep_start``
  归档，绕开 ``UNIQUE(project_id, character_name, ep_start)`` 且永不被真实
  集号命中）原地提升为当前版本。**不经过** ``portrait_candidates.py`` 的
  ``_adopt_portrait_by_id``——那个函数的 ``_set_current_portrait`` 分支判据是
  「候选行的 ``ep_start`` 是不是真实集号」，而 ``stage_initial_portrait`` 恒把
  候选行插在哨兵值 ``STAGED_INITIAL_EP_START``，套用会把哨兵值当成集号误判成
  "候选就是当前"，实测直接导致归档失败（旧记录 ``ep_start`` 原地未动）。促成
  当前版本后，既有的 ``POST .../portraits/{id}/rollback`` 端点原样可用（判据
  只看 ``base_portrait_id`` 链与 ``pack_status``，与走哪条晋升路径无关），不
  另开一套回滚端点。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import File, Form, HTTPException, UploadFile

from app.atomic_io import atomic_write_bytes
from app.bible_store import mutate_bible_json
from app.db import get_conn
from app.domain.common import _media_url, _project_or_404, router
from app.portraits import (
    _append_character_to_bible,
    _new_portrait_path,
    promote_staged_initial_portrait,
    register_initial_portrait,
    stage_initial_portrait,
)
from app.portraits.card_owner import resolve_card_owner
from app.refs import PRODUCTION_APPEARANCE_MAX_CHARS, PRODUCTION_APPEARANCE_MIN_CHARS
from app.schemas import Bible, Character
from app.validators import validate_bible

from .manual_upload import (
    DOWNSTREAM_STALE_NOTICE,
    MANUAL_STYLE_WARNING,
    MANUAL_UPLOAD_PROMPT_MARKER,
    length_gap_message,
    read_manual_image_upload,
)

# 与 register_initial_portrait 落库时硬编码的 "legacy_partial" 不同：手动上传是
# 用户显式选定的最终定妆照，不是"待多视角整包补齐"的过渡态——标 ready 才不会被
# 批量补齐任务当成缺口重新出图，也才能被既有 rollback 端点的
# `pack_status IS NULL OR pack_status='ready'` 判据选中（见 replace_character_
# portrait_image 与下方 _promote_manual_portrait）。
_MANUAL_PORTRAIT_PACK_STATUS = "ready"


def _load_bible_or_409(p: dict) -> Bible:
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经（世界观），才能手动添加角色")
    return Bible.model_validate(json.loads(p["bible_json"]))


def _owner_conflict_or_none(bible: Bible, name: str) -> HTTPException | None:
    status, value = resolve_card_owner(bible, name)
    if status == "owner":
        return HTTPException(409, {
            "code": "CHARACTER_ALREADY_EXISTS",
            "owner": value,
            "message": f"「{name}」已经是人物谱里「{value}」的本名或别名，不能重复新建；"
                       f"如需替换该角色定妆照，请使用替换图片入口。",
        })
    if status == "conflict":
        return HTTPException(409, {
            "code": "CHARACTER_NAME_AMBIGUOUS",
            "owners": value,
            "message": f"「{name}」在人物谱中同时命中 {'、'.join(value)}，无法安全判定唯一归属，"
                       f"需要人工判断后再决定新建还是登记为别名。",
        })
    return None


def _validate_manual_character_fields(
    name: str, appearance_canonical: str, period_costume_canonical: str,
) -> tuple[str, str, str]:
    """去空白 + 长度校验；越界时精确报出差多少字，不许笼统报错。"""
    name = name.strip()
    if not name:
        raise HTTPException(422, "请填写角色名称")
    appearance_canonical = appearance_canonical.strip()
    period_costume_canonical = period_costume_canonical.strip()
    gap = length_gap_message(
        "appearance_canonical（外观锚点）", appearance_canonical,
        PRODUCTION_APPEARANCE_MIN_CHARS, PRODUCTION_APPEARANCE_MAX_CHARS,
    )
    if gap:
        raise HTTPException(422, gap)
    if not period_costume_canonical:
        raise HTTPException(422, "period_costume_canonical（年代服饰）不能为空")
    return name, appearance_canonical, period_costume_canonical


def _build_validated_character(
    bible: Bible, name: str, appearance_canonical: str,
    period_costume_canonical: str, image_path: str,
) -> Character:
    """构造新角色对象并整份过 validate_bible——手动录入同样不能绕过这道闸。"""
    character = Character(
        name=name, role="", appearance_canonical=appearance_canonical,
        period_costume_canonical=period_costume_canonical,
        presence_status="onstage", appearance_status="grounded",
        portrait_eligible=True, ref_image_path=image_path,
    )
    candidate_bible = bible.model_copy(update={"characters": [*bible.characters, character]})
    errors = validate_bible(candidate_bible)
    if errors:
        raise HTTPException(422, "；".join(errors))
    return character


def _register_manual_portrait_or_500(
    conn, project_id: str, name: str, image_path: str,
    appearance_canonical: str, bible_version: int,
) -> str:
    """角色已经写入人物谱之后再登记 character_portraits；失败必须如实告知
    补救路径（替换图片重试），不能让人物谱和定妆照登记状态无声不一致。"""
    try:
        portrait_id = register_initial_portrait(
            conn, project_id, name, image_path, appearance_canonical,
            MANUAL_UPLOAD_PROMPT_MARKER, bible_version,
        )
        conn.execute(
            "UPDATE character_portraits SET pack_status=? WHERE id=?",
            (_MANUAL_PORTRAIT_PACK_STATUS, portrait_id),
        )
        conn.commit()
        return portrait_id
    except Exception as exc:  # noqa: BLE001
        conn.rollback()  # 回滚必须是异常处理器第一条语句，见函数体上方说明
        raise HTTPException(
            500,
            f"「{name}」已加入人物谱，但定妆照登记失败：{exc}；"
            f"请通过「替换图片」重新上传该角色的定妆照",
        ) from exc


@router.post("/projects/{project_id}/characters/manual")
async def add_manual_character(
    project_id: str,
    name: str = Form(...),
    appearance_canonical: str = Form(...),
    period_costume_canonical: str = Form(...),
    image: UploadFile = File(...),
):
    """用户手写角色卡三要素 + 上传一张定妆照，完全不走模型。"""
    p = _project_or_404(project_id)
    bible = _load_bible_or_409(p)
    name, appearance_canonical, period_costume_canonical = _validate_manual_character_fields(
        name, appearance_canonical, period_costume_canonical,
    )
    conflict = _owner_conflict_or_none(bible, name)
    if conflict:
        raise conflict

    image_path = _new_portrait_path(project_id, name, 1)
    character = _build_validated_character(
        bible, name, appearance_canonical, period_costume_canonical, image_path,
    )

    raw = await read_manual_image_upload(image)
    atomic_write_bytes(image_path, raw)

    conn = get_conn()
    appended = _append_character_to_bible(conn, project_id, character.model_dump(mode="json"))
    if not appended:
        Path(image_path).unlink(missing_ok=True)
        # resolve_card_owner 在写锁内被 _append_character_to_bible 复查后仍失败，
        # 说明并发写入已抢先占用了这个名字——不是本次请求的技术故障。
        raise HTTPException(409, f"「{name}」新建失败：与并发写入冲突，请重新提交")

    bible_version_row = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    bible_version = int(bible_version_row["bible_version"] or 0) if bible_version_row else 0
    portrait_id = _register_manual_portrait_or_500(
        conn, project_id, name, image_path, appearance_canonical, bible_version,
    )

    return {
        "added": True,
        "name": name,
        "portrait_id": portrait_id,
        "image_url": _media_url(image_path),
        "style_warning": MANUAL_STYLE_WARNING,
    }


def _promote_manual_portrait(conn, project_id: str, name: str, portrait_id: str, image_path: str) -> None:
    """把暂存候选提升为当前定妆照。复用 ``promote_staged_initial_portrait``
    （已有成熟形态：旧记录压进负数 ``ep_start``、``ep_end=0`` 归档，绕开
    ``UNIQUE(project_id, character_name, ep_start)`` 且永不被真实集号命中）
    做归档主体，再补它刻意不做的两件事：同步 ``bible_json`` 的
    ``ref_image_path``（原本只有 ``_set_current_portrait`` 会做，那条路径要求
    候选行带真实 ``ep_start`` 才能正确归档，与 ``stage_initial_portrait`` 恒用
    哨兵值 ``ep_start`` 不匹配，见 ``replace_character_portrait_image`` 早前的
    实测教训）与标记 ``pack_status='ready'``（见模块顶部常量注释）。"""
    promote_staged_initial_portrait(conn, project_id, name, portrait_id)
    conn.execute(
        "UPDATE character_portraits SET pack_status=? WHERE id=?",
        (_MANUAL_PORTRAIT_PACK_STATUS, portrait_id),
    )
    mutate_bible_json(conn, project_id, lambda data: _set_ref_image(data.get("characters", []), name, image_path))
    conn.commit()


def _set_ref_image(entries: list, name: str, image_path: str) -> bool:
    for entry in entries:
        if entry.get("name") == name:
            entry["ref_image_path"] = image_path
            return True
    return False


@router.post("/projects/{project_id}/characters/{character_name}/portrait-image")
async def replace_character_portrait_image(
    project_id: str, character_name: str, image: UploadFile = File(...),
):
    """用用户上传的图片替换已有角色的定妆照；旧图自动归档，可用既有 rollback 端点回滚。"""
    p = _project_or_404(project_id)
    bible = _load_bible_or_409(p)
    character = next((c for c in bible.characters if c.name == character_name), None)
    if character is None:
        raise HTTPException(404, f"角色不存在：{character_name}")

    raw = await read_manual_image_upload(image)
    image_path = _new_portrait_path(project_id, character_name, 1)
    atomic_write_bytes(image_path, raw)

    conn = get_conn()
    bible_version = int(p.get("bible_version") or 0)
    portrait_id: str | None = None
    try:
        portrait_id = stage_initial_portrait(
            conn, project_id, character_name, image_path,
            character.appearance_canonical, MANUAL_UPLOAD_PROMPT_MARKER, bible_version,
        )
        _promote_manual_portrait(conn, project_id, character_name, portrait_id, image_path)
    except Exception:
        # 回滚必须是异常处理器第一条语句：中途失败旧图必须原封不动。stage_
        # initial_portrait 已提交的候选行连同新图一并撤销复用
        # app.rejected_media.purge_character_portrait（既有清理机制，不新写
        # 一套）；候选行还没来得及写入时只需删掉刚落盘的孤儿文件。
        conn.rollback()
        if portrait_id:
            from app.rejected_media import purge_character_portrait
            purge_character_portrait(conn, portrait_id)
        else:
            Path(image_path).unlink(missing_ok=True)
        raise

    return {
        "replaced": True,
        "portrait_id": portrait_id,
        "image_url": _media_url(image_path),
        "style_warning": MANUAL_STYLE_WARNING,
        "downstream_notice": DOWNSTREAM_STALE_NOTICE,
        "rollback_url": f"/api/projects/{project_id}/characters/{character_name}"
                         f"/portraits/{portrait_id}/rollback",
    }
