"""片段发声与群演复核 API：候选生成、人工修订和原子保存。"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import get_conn
from app.evidence import repository
from app.harness.types import EvidenceArtifact
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
    return result


@router.post("/shots/{shot_id}/identity-review/regenerate")
async def regenerate_identity(shot_id: str, body: IdentityRevisionBody):
    conn = get_conn()
    try:
        review = review_identity_workspace(conn, shot_id)
        if review["baseline"] != body.baseline:
            raise ValueError("片段已更新，请重新打开复核")
        _row, episode, payload, _segment = load_identity_workspace(conn, shot_id)
        project = conn.execute("SELECT * FROM projects WHERE id=?", (episode["project_id"],)).fetchone()
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
