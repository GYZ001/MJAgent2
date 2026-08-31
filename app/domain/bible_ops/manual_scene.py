"""场景库手动新增场景 / 替换场景图（同 manual_character.py 的用户拍板，场景侧
镜像实现——本仓库场景图机制历来"完全复刻 app.portraits 的角色定妆照机制"
（见 app/scenes.py 模块 docstring），本文件延续同一分工，不是重新设计一套）。

去重查既有场景名与别名（``app.scenes._exact_known_scene_name``，与
``resolve_card_owner`` 同一"精确匹配、不猜测"精神，场景一直没有 conflict 三态——
场景名在人物谱里本身唯一，_exact_known_scene_name 只会命中 0 或 1 个）。

落库复用 ``app.scenes._append_scene_to_bible``（bible.scenes 追加）与
``app.scenes.register_initial_scene_ref``（scene_references 首次登记，同生成
路径同一张表）。

替换走独立的归档-新建：没有复用 view_redo.py 的 ``rollback_scene_reference``/
``adopt_scene_candidate``——那一对服务的是 AI 多视角候选采纳流水线，强制要求
``establishing``+``reverse_angle`` 视角齐全（``app.multiview.SCENE_REQUIRED_
VIEWS``）；手动上传天然只有一张主图、从不进入该流水线，套用那道视角门禁会让
"可回滚"的承诺落空（实测：回滚会因视角不全被拒，参见本文件下方 manual-rollback
端点 docstring）。``pack_status`` 复用既有的 ``legacy_partial`` 取值（``app/
domain/bible_ops/scene_assets.py`` 的 ``_scene_asset_state`` 早已把它归类为
"有主图、无多视角整包"的正常态，不是新造第三种状态字）。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import File, Form, HTTPException, UploadFile

from app.atomic_io import atomic_write_bytes
from app.db import get_conn, new_id, now
from app.domain.common import _media_url, _project_or_404, router
from app.refs import SCENE_CANONICAL_MAX_CHARS, SCENE_CANONICAL_MIN_CHARS, _safe_name
from app.scenes import _append_scene_to_bible, _exact_known_scene_name, _scene_dir, register_initial_scene_ref
from app.schemas import Bible, Scene
from app.validators import validate_bible

from .manual_upload import (
    DOWNSTREAM_STALE_NOTICE,
    MANUAL_STYLE_WARNING,
    MANUAL_UPLOAD_PROMPT_MARKER,
    length_gap_message,
    read_manual_image_upload,
)

# 不用 "legacy_partial"：那个取值语义是"待多视角整包补齐"的过渡态（见 scene_
# assets.py::_scene_asset_state），会被批量补齐任务当缺口重新出图，也会被角色
# 侧同款判据的既有 rollback 端点判据排除在外（manual_character.py 实测过这个
# 坑，同一教训搬到场景侧）。手动上传是用户显式选定的最终场景图，标 ready 才
# 准确——不是"AI 质检通过"，是"已被人工确认为当前生产用图"。
_MANUAL_SCENE_PACK_STATUS = "ready"


def _new_scene_image_path(project_id: str, name: str) -> str:
    return str(Path(_scene_dir(project_id)) / f"{_safe_name(name)}__manual__{new_id('scene')}.jpg")


def _load_bible_or_409(p: dict) -> Bible:
    if not p.get("bible_json"):
        raise HTTPException(409, "请先生成角色圣经（世界观），才能手动添加场景")
    return Bible.model_validate(json.loads(p["bible_json"]))


def _archive_current_scene_ref(conn, project_id: str, name: str, current_id: str) -> None:
    """把当前场景图行压入负数 ``ep_start`` 历史槽位（同 ``app.portraits.
    portrait_io._set_current_portrait`` 的 ``ep_start<=0`` 归档手法，
    ``scene_references`` 表结构不同不能共用同一份 SQL，但棘轮判据同构）。"""
    minimum = conn.execute(
        "SELECT MIN(ep_start) AS value FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=0",
        (project_id, name),
    ).fetchone()
    history_start = int(minimum["value"] if minimum and minimum["value"] is not None else 0) - 1
    conn.execute(
        "UPDATE scene_references SET ep_start=?, ep_end=0 WHERE id=?",
        (history_start, current_id),
    )


def _sync_bible_scene_ref_image(conn, project_id: str, name: str, image_path: str) -> None:
    prow = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not prow or not prow["bible_json"]:
        return
    bible_data = json.loads(prow["bible_json"])
    for scene_entry in bible_data.get("scenes", []):
        if scene_entry.get("name") == name:
            scene_entry["ref_image_path"] = image_path
            break
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(bible_data, ensure_ascii=False), project_id),
    )


def _replace_current_scene_ref(
    conn, project_id: str, name: str, image_path: str, scene_canonical: str,
    bible_version: int, *, reason: str,
) -> dict:
    """把 ``image_path`` 设为该场景的新当前场景图；已有当前版本先归档
    （``_archive_current_scene_ref``）。手动上传是同步单张图片，没有 AI 流水线
    "候选暂存-异步 QA-验收提升"的中间态，因此这里是单事务直接落定，不是省略了
    安全检查。

    回滚（见下方 ``manual-rollback`` 端点）复用同一个函数：拿历史行的
    image_path/scene_canonical 再调一次本函数，产生一条新的当前行并把"回滚前
    的当前版本"继续归档——回滚可以再被回滚，不需要为回滚单独写一条不同的路径。
    """
    current = conn.execute(
        "SELECT id FROM scene_references WHERE project_id=? AND scene_name=? "
        "AND ep_end IS NULL ORDER BY ep_start DESC LIMIT 1",
        (project_id, name),
    ).fetchone()
    scene_id = new_id("scene")
    with conn:
        if current:
            _archive_current_scene_ref(conn, project_id, name, current["id"])
        conn.execute(
            "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, "
            "scene_canonical, prompt, image_path, qa_json, base_scene_id, bible_version, "
            "artifact_id, pack_status, change_json, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                scene_id, project_id, name, 1, None, scene_canonical,
                MANUAL_UPLOAD_PROMPT_MARKER, image_path, "{}",
                current["id"] if current else None, bible_version, None,
                _MANUAL_SCENE_PACK_STATUS,
                json.dumps({
                    "change_type": "manual_replace", "adoption_reason": reason,
                    "decided_at": now(),
                }, ensure_ascii=False),
                now(),
            ),
        )
        _sync_bible_scene_ref_image(conn, project_id, name, image_path)
    return {
        "scene_reference_id": scene_id, "scene_name": name, "ep_start": 1,
        "previous_scene_reference_id": current["id"] if current else None,
    }


def _validate_manual_scene_fields(name: str, scene_canonical: str) -> tuple[str, str]:
    """去空白 + 长度校验；越界时精确报出差多少字，不许笼统报错。"""
    name = name.strip()
    if not name:
        raise HTTPException(422, "请填写场景名称")
    scene_canonical = scene_canonical.strip()
    gap = length_gap_message(
        "scene_canonical（场景锚点）", scene_canonical,
        SCENE_CANONICAL_MIN_CHARS, SCENE_CANONICAL_MAX_CHARS,
    )
    if gap:
        raise HTTPException(422, gap)
    return name, scene_canonical


def _scene_owner_conflict_or_none(bible: Bible, name: str) -> HTTPException | None:
    owner = _exact_known_scene_name(name, bible.scenes)
    if not owner:
        return None
    return HTTPException(409, {
        "code": "SCENE_ALREADY_EXISTS",
        "owner": owner,
        "message": f"「{name}」已经是场景库里「{owner}」的名称或别名，不能重复新建；"
                   f"如需替换该场景的场景图，请使用替换图片入口。",
    })


def _build_validated_scene(bible: Bible, name: str, scene_canonical: str, image_path: str) -> Scene:
    """构造新场景对象并整份过 validate_bible——手动录入同样不能绕过这道闸。"""
    scene = Scene(name=name, scene_canonical=scene_canonical, ref_image_path=image_path)
    candidate_bible = bible.model_copy(update={"scenes": [*bible.scenes, scene]})
    errors = validate_bible(candidate_bible)
    if errors:
        raise HTTPException(422, "；".join(errors))
    return scene


def _register_manual_scene_ref_or_500(
    conn, project_id: str, name: str, image_path: str,
    scene_canonical: str, bible_version: int,
) -> str:
    """场景已经写入场景库之后再登记 scene_references；失败必须如实告知补救
    路径（替换图片重试），不能让场景库和场景图登记状态无声不一致。"""
    try:
        scene_id = register_initial_scene_ref(
            conn, project_id, name, image_path, scene_canonical,
            MANUAL_UPLOAD_PROMPT_MARKER, {}, bible_version,
        )
        conn.execute(
            "UPDATE scene_references SET pack_status=? WHERE id=?",
            (_MANUAL_SCENE_PACK_STATUS, scene_id),
        )
        conn.commit()
        return scene_id
    except Exception as exc:  # noqa: BLE001
        conn.rollback()  # 回滚必须是异常处理器第一条语句
        raise HTTPException(
            500,
            f"「{name}」已加入场景库，但场景图登记失败：{exc}；请通过「替换图片」重新上传",
        ) from exc


@router.post("/projects/{project_id}/scenes/manual")
async def add_manual_scene(
    project_id: str,
    name: str = Form(...),
    scene_canonical: str = Form(...),
    image: UploadFile = File(...),
):
    """用户手写场景名 + 场景锚点串 + 上传一张场景图，完全不走模型。"""
    p = _project_or_404(project_id)
    bible = _load_bible_or_409(p)
    name, scene_canonical = _validate_manual_scene_fields(name, scene_canonical)
    conflict = _scene_owner_conflict_or_none(bible, name)
    if conflict:
        raise conflict

    image_path = _new_scene_image_path(project_id, name)
    scene = _build_validated_scene(bible, name, scene_canonical, image_path)

    raw = await read_manual_image_upload(image)
    atomic_write_bytes(image_path, raw)

    conn = get_conn()
    appended = _append_scene_to_bible(conn, project_id, scene.model_dump(mode="json"))
    if not appended:
        Path(image_path).unlink(missing_ok=True)
        raise HTTPException(409, f"「{name}」新建失败：与并发写入冲突，请重新提交")

    bible_version_row = conn.execute(
        "SELECT bible_version FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    bible_version = int(bible_version_row["bible_version"] or 0) if bible_version_row else 0
    scene_id = _register_manual_scene_ref_or_500(
        conn, project_id, name, image_path, scene_canonical, bible_version,
    )

    return {
        "added": True,
        "name": name,
        "scene_reference_id": scene_id,
        "image_url": _media_url(image_path),
        "style_warning": MANUAL_STYLE_WARNING,
    }


@router.post("/projects/{project_id}/scenes/{scene_name}/image")
async def replace_scene_image(project_id: str, scene_name: str, image: UploadFile = File(...)):
    """用用户上传的图片替换已有场景的场景图；旧图自动归档，可用下方
    manual-rollback 端点回滚。"""
    p = _project_or_404(project_id)
    bible = _load_bible_or_409(p)
    scene = next((s for s in bible.scenes if s.name == scene_name), None)
    if scene is None:
        raise HTTPException(404, f"场景不存在：{scene_name}")

    raw = await read_manual_image_upload(image)
    image_path = _new_scene_image_path(project_id, scene_name)
    atomic_write_bytes(image_path, raw)

    conn = get_conn()
    bible_version = int(p.get("bible_version") or 0)
    try:
        result = _replace_current_scene_ref(
            conn, project_id, scene_name, image_path, scene.scene_canonical,
            bible_version, reason="用户手动上传图片替换场景图",
        )
    except Exception:
        # _replace_current_scene_ref 用 `with conn:` 做单事务，异常时归档/新建/
        # bible 同步已经整体回滚，这里的 conn.rollback() 是防御性兜底；没有任何
        # 行提交过，只需清掉刚落盘的孤儿文件。
        conn.rollback()
        Path(image_path).unlink(missing_ok=True)
        raise

    previous_id = result.get("previous_scene_reference_id")
    return {
        "replaced": True,
        "scene_reference_id": result["scene_reference_id"],
        # manual-rollback 的 URL 参数是"要恢复到哪个历史版本"，不是"当前版本"
        # ——与既有 rollback_scene_reference 同一约定，见该端点 docstring。
        "previous_scene_reference_id": previous_id,
        "image_url": _media_url(image_path),
        "style_warning": MANUAL_STYLE_WARNING,
        "downstream_notice": DOWNSTREAM_STALE_NOTICE,
        "rollback_url": (
            f"/api/projects/{project_id}/scenes/{scene_name}"
            f"/refs/{previous_id}/manual-rollback"
        ) if previous_id else None,
    }


@router.post("/projects/{project_id}/scenes/{scene_name}/refs/{scene_reference_id}/manual-rollback")
async def rollback_manual_scene_image(
    project_id: str, scene_name: str, scene_reference_id: str, body: dict | None = None,
):
    """把手动替换前的历史场景图重新设为当前版本。

    与既有 ``rollback_scene_reference``（``view_redo.py``）分工：那一个服务
    AI 多视角候选采纳流水线，强制要求 ``establishing``+``reverse_angle`` 视角
    齐全才允许回滚；手动上传的场景图从未进入该流水线、天然缺齐全的多视角包，
    套用那道视角门禁会让本该可行的回滚被拒。本端点判据收窄为「历史图片文件
    是否还在」，不做视角齐全性检查。
    """
    _project_or_404(project_id)
    conn = get_conn()
    target = conn.execute(
        "SELECT * FROM scene_references WHERE id=? AND project_id=? AND scene_name=?",
        (scene_reference_id, project_id, scene_name),
    ).fetchone()
    if not target:
        raise HTTPException(404, "场景历史版本不存在")
    if not target["image_path"] or not Path(target["image_path"]).is_file():
        raise HTTPException(409, {
            "code": "SCENE_FILE_UNAVAILABLE", "message": "历史场景图文件不可用",
        })
    current = conn.execute(
        "SELECT id FROM scene_references WHERE project_id=? AND scene_name=? AND ep_end IS NULL "
        "ORDER BY ep_start DESC LIMIT 1",
        (project_id, scene_name),
    ).fetchone()
    if current and current["id"] == scene_reference_id:
        return {"rolled_back": True, "idempotent_replay": True, "scene_reference_id": scene_reference_id}
    reason = str((body or {}).get("reason") or "回滚到历史场景图").strip()
    result = _replace_current_scene_ref(
        conn, project_id, scene_name, target["image_path"],
        target["scene_canonical"] or "", int(target["bible_version"] or 0), reason=reason,
    )
    return {"rolled_back": True, "from_scene_reference_id": scene_reference_id, **result}
