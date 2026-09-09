"""片段发声与群演复核 API：候选生成、人工修订和原子保存。"""
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import get_conn
from app.evidence import repository
from app.harness.types import EvidenceArtifact
from app.harness.text_provider_scope import stage_text_provider
from app.model_registry import resolve_stage_text_provider
from app.domain.common import _media_url, _project_bible_or_placeholder
from app.production.storyboard_identity_regenerate import regenerate_identity_candidate
from .identity_workspace import (
    load_identity_workspace, prepare_identity_candidate, review_identity_workspace, save_identity_candidate,
)

router = APIRouter(prefix="/api")


class IdentityRevisionBody(BaseModel):
    baseline: str = Field(min_length=1)
    candidate: dict = Field(default_factory=dict)


@router.get("/shots/{shot_id}/identity-review")
def identity_review(shot_id: str):
    try:
        result = review_identity_workspace(get_conn(), shot_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    for version in result["versions"]:
        version["video_url"] = _media_url(version.pop("video_path", ""))
        version["observations"] = [json.loads(row["content_json"]) for row in get_conn().execute(
            "SELECT content_json FROM artifacts WHERE type='segment_identity_observation' AND scope_type='shot_version' AND scope_id=? ORDER BY created_at", (version["id"],))]
        meta = json.loads(version.pop("image_inputs", "") or "{}")
        version["reference_images"] = _review_reference_images(meta)
    return result


def _review_reference_images(meta: dict) -> list[dict]:
    """优先按提交时标签排序；素材候选顺序不能冒充供应商图片编号。"""
    refs = [r for r in meta.get("reference_images") or [] if isinstance(r, dict)]
    labels = meta.get("_seedance_image_input_labels")
    if isinstance(labels, list):
        return [_submitted_reference(refs, label, i + 1) for i, label in enumerate(labels) if isinstance(label, dict)]
    images = []
    for entry in refs:
        if entry.get("deleted") or entry.get("selectedForSeedance") is False:
            continue
        path = entry.get("image_path") or entry.get("path") or entry.get("url")
        name = entry.get("entity_name") or entry.get("name") or entry.get("label") or "未命名"
        images.append({"label":f"留存素材 · {name}（提交顺序未记录）", "url":_media_url(path) if path else None})
    return images


def _submitted_reference(refs: list[dict], label: dict, position: int) -> dict:
    """引用须由唯一槽位或身份匹配；无法唯一定位时保留编号并说明缺失。"""
    matches = [r for r in refs if r.get("slot_key") == label["slot_key"]] if label.get("slot_key") else [
        r for r in refs if label.get("entity_name") and r.get("entity_name") == label["entity_name"]
        and (not label.get("type") or r.get("type") == label["type"])
    ]
    name = label.get("label") or label.get("entity_name") or "未命名"
    path = None
    if len(matches) == 1:
        entry = matches[0]
        path = entry.get("image_path") or entry.get("path") or entry.get("url")
    return {"label":f"参考图 {position} · {name}" + ("（原图记录无法唯一定位）" if not path else ""),
            "url":_media_url(path) if path else None}


@router.post("/shots/{shot_id}/identity-review/regenerate")
async def regenerate_identity(shot_id: str, body: IdentityRevisionBody):
    conn = get_conn()
    try:
        review = review_identity_workspace(conn, shot_id)
        if review["baseline"] != body.baseline:
            raise ValueError("片段已更新，请重新打开复核")
        _row, episode, payload, _segment = load_identity_workspace(conn, shot_id)
        project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
        with stage_text_provider(resolve_stage_text_provider(dict(project).get("board_text_provider"))):
            candidate = await regenerate_identity_candidate(conn, episode=episode, shot_id=shot_id, payload=payload, bible=_project_bible_or_placeholder(project))
        return {"baseline":body.baseline,"candidate":candidate}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/shots/{shot_id}/identity-review/preview")
def preview_identity(shot_id: str, body: IdentityRevisionBody):
    try:
        current = review_identity_workspace(get_conn(), shot_id)
        if current["baseline"] != body.baseline:
            raise ValueError("片段已更新，请重新打开复核")
        candidate = prepare_identity_candidate(get_conn(), shot_id=shot_id, candidate=body.candidate)
        return {"baseline":body.baseline,"candidate":candidate}
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/shots/{shot_id}/identity-review/apply")
def apply_identity(shot_id: str, body: IdentityRevisionBody):
    try:
        return save_identity_candidate(get_conn(), shot_id=shot_id, baseline=body.baseline, candidate=body.candidate)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/shots/{shot_id}/identity-review/observation")
def record_identity_observation(shot_id: str, body: dict):
    """成片人工视听证据不自动触发付费重抽；按视频版本记录，避免套到新片段。"""
    conn = get_conn()
    version = conn.execute("SELECT id FROM shot_versions WHERE id=? AND shot_id=?", (body.get("version_id"),shot_id)).fetchone()
    if not version or not str(body.get("notes") or "").strip():
        raise HTTPException(422,"请选择当前片段的视频版本，并填写听到的说话人、看到的人物或不确定原因")
    observation = {"version_id":version["id"],"notes":str(body["notes"]),"automatic_retake":False,
                   "speech_observation":body.get("speech_observation"),"visual_observation":body.get("visual_observation")}
    artifact = repository.create_artifact(EvidenceArtifact(type="segment_identity_observation",scope_type="shot_version",scope_id=version["id"],status="candidate",trust_level="T0",content=observation))
    return {"artifact_id":artifact["id"],"message":"复核记录已保存；不会自动重抽视频"}
