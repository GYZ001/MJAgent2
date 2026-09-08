"""片段身份修订工作区：只读复核、候选验证、CAS 保存和历史视频保留。"""
import json
from copy import deepcopy

from app.artifacts import invalidate_episode_delivery_authority, invalidate_episode_final
from app.evidence import repository as evidence_repository
from app.harness.contracts import get_contract
from app.harness.types import Evaluation, EvidenceArtifact
from app.production.storyboard_dialogue_extract import extract_dialogue_targets
from app.production.storyboard_identity_contract import (
    canonical_segment_identities, identity_contract_errors, identity_contract_fingerprint,
    stamp_identity_contract, visible_character_ids,
)
from app.production.storyboard_identity_regenerate import refreshed_required_dialogue
from app.production.storyboard_identity_scope import bind_quote_identities
from app.production.storyboard_identity_submission import segment_submission_errors
from app.production.storyboard_pack import _load_indexed_source_segments, _manifest_speaker_names, _paratext_segment_indexes
from app.production.storyboard_speech_render import render_segment_speech, speech_template_errors
from app.production.storyboard_identity_validation import identity_schema_errors
from .mutation_primitives import _board_from_shot_rows


def load_identity_workspace(conn, shot_id: str) -> tuple[dict, dict, dict, dict]:
    row = conn.execute("SELECT * FROM shots WHERE id=?", (shot_id,)).fetchone()
    if row is None:
        raise ValueError("镜头不存在")
    episode = dict(conn.execute("SELECT * FROM episodes WHERE id=?", (row["episode_id"],)).fetchone())
    payload = json.loads(episode.get("screenplay_json") or "{}")
    segment = json.loads(row["shot_contract_json"] or "{}").get("storyboard_pack_segment")
    if not segment or not payload.get("prep_pack_version"):
        raise ValueError("此入口仅适用于映射包生成的分镜片段，叙事权威分镜请走原有受控修订流程")
    return dict(row), episode, payload, segment


def review_identity_workspace(conn, shot_id: str) -> dict:
    row, episode, payload, segment = load_identity_workspace(conn, shot_id)
    reviewed = deepcopy(segment)
    evidence_errors = []
    try:
        reviewed["required_dialogue"] = _source_requirements(conn, episode, payload, segment)
    except ValueError as exc:
        evidence_errors.append(str(exc))
    errors = [*evidence_errors, *segment_submission_errors(reviewed, source_text=row["source_excerpt"] or "")]
    versions = [dict(r) for r in conn.execute("SELECT id,version_no,status,prompt_text,image_inputs,video_path FROM shot_versions WHERE shot_id=? ORDER BY version_no DESC", (shot_id,))]
    return {"shot_id":shot_id, "baseline":identity_contract_fingerprint(segment), "segment":segment,
            "issues":errors, "versions":versions, "review_status":"需核对" if errors else "合同检查通过；成片仍需视听复核"}


def _source_requirements(conn, episode: dict, payload: dict, segment: dict) -> list[dict]:
    source = _load_indexed_source_segments(conn, episode)
    quotes = extract_dialogue_targets(source, _paratext_segment_indexes(payload), speaker_names=_manifest_speaker_names(payload))
    bind_quote_identities(quotes, payload)
    return refreshed_required_dialogue(segment, quotes)


def prepare_identity_candidate(conn, *, shot_id: str, candidate: dict) -> dict:
    row, episode, payload, original = load_identity_workspace(conn, shot_id)
    result = deepcopy(original)
    # 修改范围固定，客户端不能顺带更改时长、镜头号、来源范围或整集规划。
    for key in ("dialogue", "resources", "speech_template", "speech_dialect", "prompt_text"):
        if key in candidate:
            result[key] = deepcopy(candidate[key])
    errors = identity_schema_errors(result)
    if not errors:
        errors = identity_contract_errors(result)
    if errors:
        raise ValueError("；".join(errors))
    result["resources"]["scenes"] = (original.get("resources") or {}).get("scenes") or []
    result["resources"]["props"] = (original.get("resources") or {}).get("props") or []
    result["required_dialogue"] = _source_requirements(conn, episode, payload, original)
    result["degraded_capabilities"] = [n for n in result.get("degraded_capabilities") or [] if "STORYBOARD_IDENTITY_" not in n]
    result = canonical_segment_identities(result, payload)
    result["prompt_text"] = result.get("speech_template") or result.get("prompt_text") or ""
    errors = [*identity_contract_errors(result, require_explicit=True), *speech_template_errors(result, require_tokens=True)]
    if errors:
        raise ValueError("；".join(errors))
    render_segment_speech(result, dialect=str(result.get("speech_dialect") or ""))
    stamp_identity_contract(result)
    errors = segment_submission_errors(result, source_text=row["source_excerpt"] or "")
    if errors:
        raise ValueError("；".join(errors))
    return result


def _assert_idle_current(conn, shot_id: str, expected: str) -> tuple[dict, dict, dict]:
    row, episode, _payload, segment = load_identity_workspace(conn, shot_id)
    if identity_contract_fingerprint(segment) != expected:
        raise ValueError("片段已更新，请重新打开复核，不能覆盖较新版本")
    active = conn.execute("SELECT 1 FROM shot_versions WHERE shot_id=? AND video_slot_active=1 LIMIT 1", (shot_id,)).fetchone()
    pending = conn.execute("SELECT 1 FROM jobs WHERE shot_id=? AND status IN ('queued','running','waiting_provider','waiting_retry','waiting_human','paused') AND abandoned=0 LIMIT 1", (shot_id,)).fetchone()
    if active or pending:
        raise ValueError("该片段仍有视频任务，请在生成台停止该片段任务或等待结束后再保存修订")
    return row, episode, segment


def _record_identity_revision(conn, row: dict, segment: dict) -> str:
    contract = json.loads(row["shot_contract_json"] or "{}")
    contract["storyboard_pack_segment"] = segment
    names = {c["identity_id"]:c.get("display_name") or c["identity_id"] for c in segment["resources"]["characters"]}
    dialogues = [{"speaker":d["speaker_identity_id"],"line":d["line"],"delivery":d.get("delivery") or "spoken_dialogue","emotion":"平静"} for d in segment["dialogue"]]
    conn.execute("UPDATE shots SET shot_contract_json=?, characters=?, dialogues=?, adopted_version_id=NULL WHERE id=?", (json.dumps(contract,ensure_ascii=False),json.dumps([names[i] for i in visible_character_ids(segment)],ensure_ascii=False),json.dumps(dialogues,ensure_ascii=False),row["id"]))
    saved = conn.execute("SELECT * FROM shots WHERE id=?", (row["id"],)).fetchone()
    shot = _board_from_shot_rows([saved], 1).shots[0]
    artifact = evidence_repository.create_and_commit_artifact_in_transaction(conn, EvidenceArtifact(
        type="storyboard_shot",scope_type="storyboard_checkpoint",scope_id=f"{row['episode_id']}:{row['shot_no']}",
        status="candidate",trust_level="T1",content=shot.model_dump(mode="json"),
        parent_artifact_ids=[row["storyboard_artifact_id"]] if row.get("storyboard_artifact_id") else [],contract_version=get_contract("storyboard").version,
    ), [Evaluation(evaluator_type="deterministic",evaluator_name="segment_identity_revision",evaluator_version=segment["identity_contract_version"],status="passed",hard_gate_passed=True,score=100,evidence={"identity_fingerprint":segment["identity_contract_fingerprint"]})])
    conn.execute("UPDATE shots SET storyboard_artifact_id=? WHERE id=?", (artifact["id"], row["id"]))
    return artifact["id"]


def save_identity_candidate(conn, *, shot_id: str, baseline: str, candidate: dict) -> dict:
    """持有写锁后重验来源和版本；全部成功才替换，旧视频文件完整保留。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row, episode, original = _assert_idle_current(conn, shot_id, baseline)
        prepared = prepare_identity_candidate(conn, shot_id=shot_id, candidate=candidate)
        if identity_contract_fingerprint(prepared) == identity_contract_fingerprint(original):
            conn.rollback()
            return {"unchanged":True}
        artifact_id = _record_identity_revision(conn, row, prepared)
        changed = conn.execute("UPDATE shot_versions SET status='stale', error='片段发声或人物身份已修订；此版本保留供历史对比', video_slot_active=0 WHERE shot_id=? AND status='succeeded'", (shot_id,)).rowcount
        conn.execute("UPDATE episodes SET status='scripted', storyboard_warning=NULL WHERE id=?", (episode["id"],))
        invalidate_episode_delivery_authority(conn, episode["id"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    invalidate_episode_final(episode["id"])
    return {"artifact_id":artifact_id,"history_versions_preserved":changed,"segment":prepared}
