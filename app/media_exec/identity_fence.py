"""付费提交边界再次核对片段合同，防止队列等待期间的身份修订被旧任务穿透。"""
import json

from app.media_exec.fences import ReviewDependencyFence
from app.production.storyboard_identity_contract import identity_contract_fingerprint
from app.production.storyboard_identity_submission import segment_submission_errors


def assert_identity_revision(conn, *, shot_id: str, meta: dict, write_point: str) -> None:
    """已有供应商任务继续收取结果；新提交和复用必须基于同一片段身份。"""
    expected = meta.get("segment_identity_fingerprint")
    if not expected or write_point not in {"worker_start", "provider_input_adoption", "provider_submit"}:
        return
    row = conn.execute("SELECT shot_contract_json, source_excerpt FROM shots WHERE id=?", (shot_id,)).fetchone()
    segment = (json.loads(row["shot_contract_json"] or "{}") if row else {}).get("storyboard_pack_segment") or {}
    if identity_contract_fingerprint(segment) != expected:
        raise ReviewDependencyFence("[STORYBOARD_IDENTITY_STALE] 本片段的发声或人物合同已更新，请使用该片段最新版本重新生成")
    errors = segment_submission_errors(segment, source_text=str(row["source_excerpt"] or ""))
    if errors:
        raise ReviewDependencyFence("[STORYBOARD_IDENTITY_CONFLICT] " + "；".join(errors))
