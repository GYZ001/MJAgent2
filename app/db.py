"""SQLite 存储。9 张表（PRD §5.2），媒体文件只存路径不存内容。"""
from __future__ import annotations

import asyncio
import json
import hashlib
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, TypeVar
import weakref

from app.config import DATA_DIR, DB_PATH, DEFAULT_SETTINGS

_local = threading.local()
_task_connections: weakref.WeakKeyDictionary[
    asyncio.Task[Any], sqlite3.Connection
] = weakref.WeakKeyDictionary()
_task_connections_lock = threading.Lock()
_T = TypeVar("_T")

ASYNC_WRITE_RETRY_DELAYS_S = (0.05, 0.1, 0.2, 0.4)


class _WriteTransactionStartError(Exception):
    def __init__(self, original: sqlite3.OperationalError):
        self.original = original
        super().__init__(str(original))


SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    novel_chars INTEGER DEFAULT 0,
    bible_json TEXT,
    bible_version INTEGER DEFAULT 0,
    bible_status TEXT DEFAULT 'idle',
    bible_error TEXT,
    bible_style_name TEXT,
    plan_status TEXT DEFAULT 'idle',
    plan_error TEXT,
    key_timeline TEXT,
    bible_artifact_id TEXT,
    harness_engine_enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    summary TEXT,
    char_count INTEGER DEFAULT 0,
    cleaned_lines INTEGER DEFAULT 0,
    UNIQUE(project_id, idx),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS novel_import_receipts (
    token_hash TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_novel_import_receipts_project
    ON novel_import_receipts(project_id);
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    episode_no INTEGER NOT NULL,
    title TEXT,
    hook TEXT,
    cliffhanger TEXT,
    synopsis TEXT,
    source_chapters TEXT,
    target_duration_s INTEGER DEFAULT 50,
    planning_target_duration_s INTEGER,
    planning_duration_source TEXT,
    target_duration_authority TEXT NOT NULL DEFAULT 'planning_estimate',
    storyboard_outline_revision INTEGER NOT NULL DEFAULT 0,
    storyboard_outline_fingerprint TEXT,
    storyboard_outline_artifact_id TEXT,
    screenplay_json TEXT,
    screenplay_status TEXT DEFAULT 'pending',
    screenplay_error TEXT,
    screenplay_started_at REAL,
    screenplay_updated_at REAL,
    screenplay_required_dialogues TEXT NOT NULL DEFAULT '[]',
    screenplay_required_dialogue_occurrences TEXT NOT NULL DEFAULT '[]',
    screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]',
    screenplay_artifact_id TEXT,
    screenplay_publish_fence INTEGER NOT NULL DEFAULT 0,
    screenplay_snapshot_version INTEGER NOT NULL DEFAULT 0,
    screenplay_constraint_version INTEGER NOT NULL DEFAULT 0,
    storyboard_artifact_id TEXT,
    narrative_status TEXT NOT NULL DEFAULT 'needs_review',
    narrative_review_artifact_id TEXT,
    narrative_calibration_artifact_id TEXT,
    delivery_artifact_id TEXT,
    delivery_status TEXT NOT NULL DEFAULT 'not_ready',
    status TEXT DEFAULT 'planned',
    script_error TEXT,
    created_at REAL NOT NULL,
    UNIQUE(project_id, episode_no),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS shots (
    id TEXT PRIMARY KEY,
    shot_uid TEXT,
    episode_id TEXT NOT NULL,
    script_id TEXT,
    shot_no INTEGER NOT NULL,
    duration_s INTEGER NOT NULL,
    shot_size TEXT,
    camera_move TEXT,
    scene_time TEXT DEFAULT '',
    scene_setting TEXT,
    scene_name TEXT,
    characters TEXT,
    action_desc TEXT,
    source_excerpt TEXT DEFAULT '',
    narration TEXT,
    dialogues TEXT,
    transition TEXT,
    continuity_from_prev INTEGER DEFAULT 0,
    shot_contract_json TEXT,
    continuity_mode TEXT DEFAULT '',
    observed_state_out TEXT DEFAULT '',
    adopted_version_id TEXT,
    approved_scene_id TEXT,
    approved_head_scene_id TEXT,
    approved_tail_scene_id TEXT,
    scene_status TEXT DEFAULT 'none',
    storyboard_artifact_id TEXT,
    UNIQUE(episode_id, shot_no),
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS screenplay_drafts (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    baseline_artifact_id TEXT,
    content_json TEXT,
    constraint_json TEXT NOT NULL DEFAULT '{}',
    dirty_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(episode_id),
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS shot_versions (
    id TEXT PRIMARY KEY,
    shot_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    prompt_text TEXT NOT NULL,
    idem_key TEXT NOT NULL,
    provider_task_id TEXT,
    status TEXT DEFAULT 'queued',
    video_slot_active INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    video_path TEXT,
    last_frame_url TEXT,
    qa_json TEXT,
    cost_cny REAL DEFAULT 0,
    latency_s REAL DEFAULT 0,
    technical_validation_json TEXT,
    adoption_reason TEXT,
    playback_rate REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL,
    UNIQUE(shot_id, version_no),
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS shot_scenes (
    id TEXT PRIMARY KEY,
    shot_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    kind TEXT DEFAULT 'tail',       -- head（场景起始镜的首图）/ tail（每镜的尾图，下一连续镜的首图）
    prompt_text TEXT NOT NULL,
    image_path TEXT,
    status TEXT DEFAULT 'queued',   -- queued/running/succeeded/failed
    error TEXT,
    qa_json TEXT,                   -- {overall, issues, continuity}
    cost_cny REAL DEFAULT 0,
    adoption_reason TEXT,
    created_at REAL NOT NULL,
    UNIQUE(shot_id, kind, version_no),
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    shot_id TEXT,
    version_id TEXT,
    episode_id TEXT,
    project_id TEXT,
    status TEXT DEFAULT 'queued',
    video_slot_active INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    after_shot_id TEXT,
    after_version_id TEXT,
    scene_kinds TEXT,
    run_id TEXT,
    owner_run_id TEXT,
    step_run_id TEXT,
    lease_owner TEXT,
    lease_expires_at REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    max_retries INTEGER NOT NULL DEFAULT 3,
    reserved_cost_cny REAL NOT NULL DEFAULT 0,
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    provider_non_cancellable INTEGER NOT NULL DEFAULT 0,
    provider_operation_id TEXT,
    provider_create_state TEXT NOT NULL DEFAULT 'not_started',
    provider_failure_category TEXT,
    provider_failure_kind TEXT,
    provider_failure_disposition TEXT,
    provider_failure_retryable INTEGER,
    provider_submitted_at REAL,
    provider_poll_required INTEGER NOT NULL DEFAULT 0,
    provider_result_adoptable INTEGER NOT NULL DEFAULT 1,
    abandoned INTEGER NOT NULL DEFAULT 0,
    attempt_started_at REAL,
    pipeline_stage TEXT,
    stage_status TEXT,
    stage_started_at REAL,
    stage_updated_at REAL,
    stage_progress_json TEXT,
    reason_code TEXT,
    reason_text TEXT,
    scheduler_lane TEXT,
    priority_class TEXT,
    ready_at REAL,
    state_revision INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS provider_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    latency_ms INTEGER,
    error TEXT,
    request_json TEXT,
    request_hash TEXT,
    contract_version TEXT,
    production_grant_id TEXT,
    response_json TEXT,
    meta TEXT,
    project_id TEXT,
    run_id TEXT,
    step_run_id TEXT,
    trace_id TEXT,
    operation_id TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    supersedes_call_id INTEGER,
    superseded_by_call_id INTEGER,
    recovery_disposition TEXT,
    first_chunk_at REAL,
    last_chunk_at REAL,
    received_chars INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(supersedes_call_id) REFERENCES provider_calls(id),
    FOREIGN KEY(superseded_by_call_id) REFERENCES provider_calls(id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_audit (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS character_portraits (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    character_name TEXT NOT NULL,
    ep_start INTEGER NOT NULL,        -- 适用集左区间（含）
    ep_end INTEGER,                   -- 适用集右区间（含）；NULL=当前最新版，开区间向后覆盖
    appearance TEXT,                  -- 该定妆照对应的外观锚点串
    prompt TEXT,                      -- 生成用 prompt
    image_path TEXT,                  -- 落盘路径
    base_portrait_id TEXT,            -- 图生图所基于的上一张定妆照（lineage）
    bible_version INTEGER DEFAULT 0,
    artifact_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE(project_id, character_name, ep_start),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scene_references (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    scene_name TEXT NOT NULL,
    ep_start INTEGER NOT NULL,        -- 适用集左区间（含）
    ep_end INTEGER,                   -- 适用集右区间（含）；NULL=当前最新版，开区间向后覆盖
    scene_canonical TEXT,             -- 该场景图对应的场景锚点串
    prompt TEXT,                      -- 生成用 prompt
    image_path TEXT,                  -- 落盘路径
    qa_json TEXT,                     -- {overall, issues}
    base_scene_id TEXT,               -- 图生图所基于的上一张场景图（lineage）
    bible_version INTEGER DEFAULT 0,
    artifact_id TEXT,
    created_at REAL NOT NULL,
    UNIQUE(project_id, scene_name, ep_start),
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scene_refs_proj_name ON scene_references(project_id, scene_name, ep_start);
CREATE INDEX IF NOT EXISTS idx_portraits_proj_char ON character_portraits(project_id, character_name, ep_start);
CREATE TABLE IF NOT EXISTS character_portrait_views (
    id TEXT PRIMARY KEY,
    portrait_id TEXT NOT NULL,
    view_role TEXT NOT NULL,
    framing TEXT,
    image_path TEXT,
    prompt TEXT,
    qa_json TEXT,
    artifact_id TEXT,
    base_view_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    selected INTEGER NOT NULL DEFAULT 1,
    input_fingerprint TEXT,
    created_at REAL NOT NULL,
    UNIQUE(portrait_id, view_role),
    FOREIGN KEY(portrait_id) REFERENCES character_portraits(id) ON DELETE CASCADE,
    FOREIGN KEY(base_view_id) REFERENCES character_portrait_views(id)
);
CREATE INDEX IF NOT EXISTS idx_portrait_views_portrait ON character_portrait_views(portrait_id, view_role);
CREATE TABLE IF NOT EXISTS scene_reference_views (
    id TEXT PRIMARY KEY,
    scene_reference_id TEXT NOT NULL,
    view_role TEXT NOT NULL,
    camera_axis TEXT,
    image_path TEXT,
    prompt TEXT,
    qa_json TEXT,
    artifact_id TEXT,
    base_view_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    selected INTEGER NOT NULL DEFAULT 1,
    input_fingerprint TEXT,
    created_at REAL NOT NULL,
    UNIQUE(scene_reference_id, view_role),
    FOREIGN KEY(scene_reference_id) REFERENCES scene_references(id) ON DELETE CASCADE,
    FOREIGN KEY(base_view_id) REFERENCES scene_reference_views(id)
);
CREATE INDEX IF NOT EXISTS idx_scene_ref_views_scene ON scene_reference_views(scene_reference_id, view_role);
CREATE TABLE IF NOT EXISTS scene_review_batches (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    policy_version TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    baseline_snapshot_json TEXT NOT NULL DEFAULT '[]',
    incremental_snapshot_json TEXT NOT NULL DEFAULT '[]',
    cutoff_at REAL,
    denominator INTEGER NOT NULL DEFAULT 0,
    evaluated INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    warning INTEGER NOT NULL DEFAULT 0,
    hard_failed INTEGER NOT NULL DEFAULT 0,
    unverified INTEGER NOT NULL DEFAULT 0,
    disposition_count INTEGER NOT NULL DEFAULT 0,
    shadow_mode INTEGER NOT NULL DEFAULT 1,
    block_new_references INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    started_at REAL,
    finished_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS scene_review_items (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    scene_reference_id TEXT NOT NULL,
    adopted_version TEXT NOT NULL,
    scene_name TEXT NOT NULL,
    old_status TEXT,
    result_status TEXT NOT NULL DEFAULT 'queued',
    evidence_json TEXT,
    disposition TEXT NOT NULL DEFAULT 'pending',
    evaluated_at REAL,
    UNIQUE(batch_id, scene_reference_id, adopted_version),
    FOREIGN KEY(batch_id) REFERENCES scene_review_batches(id) ON DELETE CASCADE,
    FOREIGN KEY(scene_reference_id) REFERENCES scene_references(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_scene_review_batches_project ON scene_review_batches(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_scene_review_items_batch ON scene_review_items(batch_id, result_status);
CREATE INDEX IF NOT EXISTS idx_chapters_project ON chapters(project_id, idx);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id, episode_no);
CREATE INDEX IF NOT EXISTS idx_shots_episode ON shots(episode_id, shot_no);
CREATE INDEX IF NOT EXISTS idx_versions_shot ON shot_versions(shot_id, version_no);
CREATE INDEX IF NOT EXISTS idx_scenes_shot ON shot_scenes(shot_id, version_no);
CREATE INDEX IF NOT EXISTS idx_versions_idem ON shot_versions(idem_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id TEXT PRIMARY KEY,
    workflow_type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    parent_run_id TEXT,
    status TEXT NOT NULL,
    current_step_key TEXT,
    requested_by TEXT,
    trigger_type TEXT,
    input_fingerprint TEXT NOT NULL,
    policy_snapshot_json TEXT NOT NULL DEFAULT '{}',
    config_snapshot_json TEXT NOT NULL DEFAULT '{}',
    budget_limit_cny REAL,
    cost_cny REAL NOT NULL DEFAULT 0,
    deadline_at REAL,
    started_at REAL,
    updated_at REAL NOT NULL,
    finished_at REAL,
    failure_code TEXT,
    failure_message TEXT,
    resume_from_step TEXT,
    recovered_by_run_id TEXT,
    recovered_at REAL,
    recovery_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(parent_run_id) REFERENCES workflow_runs(id),
    FOREIGN KEY(recovered_by_run_id) REFERENCES workflow_runs(id)
);
CREATE TABLE IF NOT EXISTS character_payment_quotes (
    quote_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    action TEXT NOT NULL,
    scope_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed_task_id TEXT,
    consumed_run_id TEXT,
    created_at REAL NOT NULL,
    consumed_at REAL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_character_payment_quotes_project
    ON character_payment_quotes(project_id, created_at);
CREATE TABLE IF NOT EXISTS step_runs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_key TEXT NOT NULL,
    iteration_no INTEGER NOT NULL DEFAULT 1,
    parent_step_run_id TEXT,
    status TEXT NOT NULL,
    agent_name TEXT,
    contract_version TEXT,
    prompt_version TEXT,
    policy_version TEXT,
    input_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    context_manifest_json TEXT NOT NULL DEFAULT '{}',
    output_artifact_id TEXT,
    issue_fingerprint TEXT,
    decision TEXT,
    exit_reason TEXT,
    started_at REAL,
    finished_at REAL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost_cny REAL NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
    FOREIGN KEY(parent_step_run_id) REFERENCES step_runs(id)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    trust_level TEXT NOT NULL,
    content_json TEXT,
    file_path TEXT,
    content_hash TEXT NOT NULL,
    created_by_step_run_id TEXT,
    parent_artifact_ids_json TEXT NOT NULL DEFAULT '[]',
    contract_version TEXT,
    prompt_version TEXT,
    model_snapshot_json TEXT NOT NULL DEFAULT '{}',
    stale_reason TEXT,
    superseded_by_artifact_id TEXT,
    created_at REAL NOT NULL,
    approved_at REAL,
    FOREIGN KEY(created_by_step_run_id) REFERENCES step_runs(id),
    FOREIGN KEY(superseded_by_artifact_id) REFERENCES artifacts(id),
    UNIQUE(type, scope_type, scope_id, version)
);
CREATE TABLE IF NOT EXISTS evaluations (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    step_run_id TEXT,
    evaluator_type TEXT NOT NULL,
    evaluator_name TEXT NOT NULL,
    evaluator_version TEXT NOT NULL,
    status TEXT NOT NULL,
    hard_gate_passed INTEGER NOT NULL,
    evaluation_role TEXT,
    score_status TEXT,
    runtime_blocking INTEGER NOT NULL DEFAULT 0,
    retry_eligible INTEGER NOT NULL DEFAULT 0,
    score REAL,
    dimension_scores_json TEXT NOT NULL DEFAULT '{}',
    issues_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    raw_result_ref TEXT,
    confidence REAL,
    recovered INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id),
    FOREIGN KEY(step_run_id) REFERENCES step_runs(id)
);
CREATE TABLE IF NOT EXISTS run_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_run_id TEXT,
    ts REAL NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    trace_id TEXT,
    FOREIGN KEY(run_id) REFERENCES workflow_runs(id),
    FOREIGN KEY(step_run_id) REFERENCES step_runs(id)
);
CREATE TABLE IF NOT EXISTS budget_reservations (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    amount_cny REAL NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    settled_at REAL,
    actual_cost_cny REAL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS gate_decisions (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL,
    run_id TEXT,
    gate_key TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    accepted_risk TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id),
    FOREIGN KEY(run_id) REFERENCES workflow_runs(id)
);
CREATE TABLE IF NOT EXISTS storyboard_workspace_state (
    episode_id TEXT PRIMARY KEY,
    snapshot_version INTEGER NOT NULL DEFAULT 1,
    state_fingerprint TEXT NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS storyboard_action_previews (
    token TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    shot_id TEXT,
    baseline_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    expires_at REAL NOT NULL,
    consumed_at REAL,
    created_at REAL NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_storyboard_previews_scope
    ON storyboard_action_previews(episode_id, shot_id, action_type, created_at);
CREATE TABLE IF NOT EXISTS storyboard_edit_sessions (
    token TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    baseline_artifact_id TEXT,
    baseline_content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_storyboard_edit_sessions_shot
    ON storyboard_edit_sessions(shot_id, status, created_at);
CREATE TABLE IF NOT EXISTS media_cleanup_outbox (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    shot_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at REAL NOT NULL,
    completed_at REAL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_cleanup_outbox_pending
    ON media_cleanup_outbox(status, created_at);
CREATE TABLE IF NOT EXISTS storyboard_source_bindings (
    shot_id TEXT PRIMARY KEY,
    binding_kind TEXT NOT NULL DEFAULT 'source_excerpt',
    chapter_id INTEGER NOT NULL,
    chapter_idx INTEGER NOT NULL,
    source_version_hash TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    excerpt_hash TEXT NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE,
    FOREIGN KEY(chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS video_version_archives (
    version_id TEXT PRIMARY KEY,
    archived_by TEXT NOT NULL DEFAULT 'user',
    reason TEXT,
    archived_at REAL NOT NULL,
    FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS review_action_audit (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    target_version TEXT,
    idempotency_key TEXT,
    old_state_json TEXT NOT NULL DEFAULT '{}',
    new_state_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT,
    decided_by TEXT NOT NULL DEFAULT 'user',
    request_id TEXT,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_review_action_idempotency
    ON review_action_audit(action, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key != '';
CREATE TABLE IF NOT EXISTS delivery_packages (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    status TEXT NOT NULL,
    package_path TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    quality_report_json TEXT NOT NULL,
    known_issues TEXT NOT NULL,
    created_at REAL NOT NULL,
    approved_at REAL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
);
CREATE TABLE IF NOT EXISTS customer_feedback (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    issue_code TEXT,
    rating INTEGER,
    message TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_by TEXT NOT NULL,
    revision_run_id TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    FOREIGN KEY(artifact_id) REFERENCES artifacts(id),
    FOREIGN KEY(revision_run_id) REFERENCES workflow_runs(id)
);
CREATE TABLE IF NOT EXISTS concat_operation_receipts (
    operation_key TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}',
    final_path TEXT NOT NULL DEFAULT '',
    final_sha256 TEXT NOT NULL DEFAULT '',
    report_path TEXT NOT NULL DEFAULT '',
    report_sha256 TEXT NOT NULL DEFAULT '',
    report_content TEXT NOT NULL DEFAULT '',
    stage_path TEXT NOT NULL DEFAULT '',
    stage_sha256 TEXT NOT NULL DEFAULT '',
    promotion_phase TEXT NOT NULL DEFAULT 'claimed',
    release_authority_json TEXT NOT NULL DEFAULT '{}',
    video_manifest_json TEXT NOT NULL DEFAULT '{}',
    claim_token TEXT NOT NULL DEFAULT '',
    lease_expires_at REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    mode TEXT NOT NULL,
    baseline_label TEXT NOT NULL,
    candidate_label TEXT NOT NULL,
    status TEXT NOT NULL,
    sample_count INTEGER NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    thresholds_json TEXT NOT NULL DEFAULT '{}',
    regressions_json TEXT NOT NULL DEFAULT '[]',
    is_real_project INTEGER NOT NULL DEFAULT 0,
    attested_by TEXT,
    attestation_note TEXT,
    created_at REAL NOT NULL,
    finished_at REAL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_scope ON workflow_runs(scope_type, scope_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_parent ON workflow_runs(parent_run_id);
CREATE INDEX IF NOT EXISTS idx_step_runs_run ON step_runs(run_id, started_at, iteration_no);
CREATE INDEX IF NOT EXISTS idx_step_runs_parent ON step_runs(parent_step_run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_scope ON artifacts(scope_type, scope_id, type, version);
CREATE INDEX IF NOT EXISTS idx_artifacts_created_step ON artifacts(created_by_step_run_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_artifact ON evaluations(artifact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evaluations_step ON evaluations(step_run_id);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_run_events_step ON run_events(step_run_id);
CREATE INDEX IF NOT EXISTS idx_budget_scope ON budget_reservations(scope_type, scope_id, status);
CREATE INDEX IF NOT EXISTS idx_gate_pending ON gate_decisions(gate_key, decision, created_at);
CREATE INDEX IF NOT EXISTS idx_gate_decisions_run ON gate_decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_delivery_episode ON delivery_packages(episode_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_episode ON customer_feedback(episode_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_revision_run ON customer_feedback(revision_run_id);
CREATE INDEX IF NOT EXISTS idx_provider_calls_run ON provider_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_provider_calls_step ON provider_calls(step_run_id);
CREATE INDEX IF NOT EXISTS idx_benchmark_project ON benchmark_runs(project_id, created_at);
CREATE TABLE IF NOT EXISTS error_logs (
    id TEXT PRIMARY KEY,             -- 错误ID（ERR-YYYYMMDD-xxxxxx），前端展示 + 后端定位的唯一句柄
    ts REAL NOT NULL,
    category TEXT NOT NULL,          -- 分类 key（validation/conflict/provider/generation/...）
    category_label TEXT,             -- 分类中文名（展示用）
    code TEXT NOT NULL,              -- 报错码（VAL-422/CON-409/LLM/GEN/SYS...）
    is_technical INTEGER DEFAULT 0,  -- 1=技术类（原文脱敏），0=业务类（原文可展示）
    http_status INTEGER,
    action TEXT,                     -- 请求动作：'POST /api/...' 或后台任务标签
    context_json TEXT,               -- 请求上下文（method/path/path_params/query/body/关联id），已脱敏截断
    message TEXT,                    -- 原始报错信息 str(exc)
    traceback TEXT,                  -- 完整堆栈
    exc_type TEXT,                   -- 异常类名
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_logs_ts ON error_logs(ts);
CREATE TABLE IF NOT EXISTS agent_conversations (
  id TEXT PRIMARY KEY,
  title TEXT,
  project_id TEXT,
  created_by TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  turn_id TEXT,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL,
  model_visible INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  FOREIGN KEY(conversation_id) REFERENCES agent_conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_turns (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  status TEXT NOT NULL,
  context_envelope_json TEXT,
  model_provider TEXT,
  model TEXT,
  prompt_version TEXT,
  started_at REAL NOT NULL,
  finished_at REAL,
  failure_code TEXT,
  failure_message TEXT,
  FOREIGN KEY(conversation_id) REFERENCES agent_conversations(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_tool_calls (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  command_name TEXT NOT NULL,
  command_version TEXT,
  arguments_json TEXT NOT NULL,
  risk TEXT,
  status TEXT NOT NULL,
  idempotency_key TEXT,
  approval_id TEXT,
  command_id TEXT,
  run_id TEXT,
  result_summary_json TEXT,
  error_id TEXT,
  started_at REAL,
  finished_at REAL,
  FOREIGN KEY(turn_id) REFERENCES agent_turns(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_approvals (
  id TEXT PRIMARY KEY,
  tool_call_id TEXT NOT NULL,
  decision TEXT,
  impact_snapshot_json TEXT,
  state_fingerprint TEXT,
  token_hash TEXT,
  decided_by TEXT,
  reason TEXT,
  expires_at REAL,
  used_at REAL,
  created_at REAL NOT NULL,
  FOREIGN KEY(tool_call_id) REFERENCES agent_tool_calls(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_turn_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  turn_id TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(turn_id, event_id),
  FOREIGN KEY(turn_id) REFERENCES agent_turns(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_conv ON agent_messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_turns_conv ON agent_turns(conversation_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_turn ON agent_tool_calls(turn_id, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_tool_calls_run ON agent_tool_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_turn ON agent_turn_events(turn_id, event_id);
CREATE TABLE IF NOT EXISTS mcp_tokens (
    id TEXT PRIMARY KEY,
    name TEXT,
    token_hash TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL,
    revoked_at REAL,
    last_used_at REAL
);
CREATE INDEX IF NOT EXISTS idx_mcp_tokens_hash ON mcp_tokens(token_hash);

CREATE TABLE IF NOT EXISTS media_tasks (
    -- DEPRECATED / UNUSED (2026-07)：表已建但全仓库无 DML；现行媒体调度走 jobs + media_scheduler。
    -- 禁止再接新调度器到此表；后续迁移可 DROP。保留仅为兼容旧库文件。
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    resource_class TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 50,
    available_at REAL,
    lease_owner TEXT,
    lease_expires_at REAL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    input_fingerprint TEXT,
    provider_operation_id TEXT,
    provider_task_id TEXT,
    provider_submitted_at REAL,
    next_poll_at REAL,
    started_at REAL,
    finished_at REAL,
    error_code TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_tasks_job ON media_tasks(job_id, stage);
CREATE INDEX IF NOT EXISTS idx_media_tasks_sched ON media_tasks(status, resource_class, priority DESC, available_at);

CREATE TABLE IF NOT EXISTS media_task_dependencies (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(task_id) REFERENCES media_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(depends_on_task_id) REFERENCES media_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_media_task_deps_task ON media_task_dependencies(task_id);
CREATE INDEX IF NOT EXISTS idx_media_task_deps_up ON media_task_dependencies(depends_on_task_id);

CREATE TABLE IF NOT EXISTS reference_sets (
    id TEXT PRIMARY KEY,
    shot_id TEXT NOT NULL,
    source_version_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    static_ready INTEGER NOT NULL DEFAULT 0,
    continuity_ready INTEGER NOT NULL DEFAULT 0,
    group_gate_passed INTEGER NOT NULL DEFAULT 0,
    input_fingerprint TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reference_sets_shot ON reference_sets(shot_id, revision DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reference_sets_fp ON reference_sets(shot_id, fingerprint);

CREATE TABLE IF NOT EXISTS reference_assets (
    id TEXT PRIMARY KEY,
    reference_set_id TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    source TEXT,
    path TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    quality_score REAL,
    consistency_score REAL,
    selected INTEGER NOT NULL DEFAULT 1,
    deleted INTEGER NOT NULL DEFAULT 0,
    qa_json TEXT,
    slot_key TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    generation_status TEXT,
    qa_status TEXT,
    input_fingerprint TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(reference_set_id) REFERENCES reference_sets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reference_assets_set ON reference_assets(reference_set_id, sort_order);

CREATE TABLE IF NOT EXISTS provider_video_capability_snapshots (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    gateway TEXT NOT NULL DEFAULT '',
    api_version TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL,
    probe_time REAL NOT NULL,
    probe_task_id TEXT,
    probe_result TEXT NOT NULL DEFAULT 'unverified',
    technical_success INTEGER NOT NULL DEFAULT 0,
    semantic_continuation_success INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_video_capability_lookup
    ON provider_video_capability_snapshots(provider, model, probe_time DESC);

CREATE TABLE IF NOT EXISTS episode_video_generation_plans (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    source_storyboard_revision_id TEXT NOT NULL,
    published_storyboard_artifact_id TEXT NOT NULL DEFAULT '',
    published_storyboard_artifact_hash TEXT NOT NULL DEFAULT '',
    completion_certificate_id TEXT NOT NULL DEFAULT '',
    narrative_review_artifact_id TEXT NOT NULL DEFAULT '',
    narrative_calibration_artifact_id TEXT NOT NULL DEFAULT '',
    release_qualification_hash TEXT NOT NULL DEFAULT '',
    capability_snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    planner_provider TEXT,
    planner_model TEXT,
    planner_prompt_fingerprint TEXT,
    blockers_json TEXT NOT NULL DEFAULT '[]',
    estimated_latency_ms INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL NOT NULL DEFAULT 0,
    critical_path_latency_ms INTEGER NOT NULL DEFAULT 0,
    safe_parallelism_ratio REAL NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    UNIQUE(episode_id, plan_revision),
    FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE,
    FOREIGN KEY(capability_snapshot_id)
        REFERENCES provider_video_capability_snapshots(id)
);
CREATE INDEX IF NOT EXISTS idx_episode_video_plans
    ON episode_video_generation_plans(episode_id, plan_revision DESC);

CREATE TABLE IF NOT EXISTS shot_video_generation_plans (
    id TEXT PRIMARY KEY,
    episode_video_plan_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    shot_no INTEGER NOT NULL,
    planned_mode TEXT NOT NULL,
    actual_mode TEXT,
    video_input_intent TEXT,
    depends_on_shot_id TEXT,
    relations_json TEXT NOT NULL DEFAULT '{}',
    state_dependency TEXT NOT NULL DEFAULT 'none',
    motion_dependency TEXT NOT NULL DEFAULT 'none',
    required_assets_json TEXT NOT NULL DEFAULT '[]',
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    unknown_dimensions_json TEXT NOT NULL DEFAULT '[]',
    fallback_order_json TEXT NOT NULL DEFAULT '[]',
    max_attempts INTEGER NOT NULL DEFAULT 2,
    max_cost REAL NOT NULL DEFAULT 0,
    timeout_s REAL NOT NULL DEFAULT 7200,
    estimated_latency_ms INTEGER NOT NULL DEFAULT 0,
    estimated_cost REAL NOT NULL DEFAULT 0,
    critical_path_group TEXT,
    capability_snapshot_id TEXT NOT NULL,
    input_fingerprints_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'planned',
    degraded_from_mode TEXT,
    degraded_to_mode TEXT,
    degraded_reason TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(episode_video_plan_id, shot_id),
    FOREIGN KEY(episode_video_plan_id)
        REFERENCES episode_video_generation_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE,
    FOREIGN KEY(depends_on_shot_id) REFERENCES shots(id) ON DELETE SET NULL,
    FOREIGN KEY(capability_snapshot_id)
        REFERENCES provider_video_capability_snapshots(id)
);
CREATE INDEX IF NOT EXISTS idx_shot_video_plan_lookup
    ON shot_video_generation_plans(shot_id, created_at DESC);

CREATE TABLE IF NOT EXISTS video_plan_dependencies (
    id TEXT PRIMARY KEY,
    episode_video_plan_id TEXT NOT NULL,
    shot_plan_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    depends_on_shot_id TEXT NOT NULL,
    dependency_kind TEXT NOT NULL,
    upstream_adopted_version_id TEXT,
    resolved_at REAL,
    created_at REAL NOT NULL,
    UNIQUE(episode_video_plan_id, shot_id, depends_on_shot_id, dependency_kind),
    FOREIGN KEY(episode_video_plan_id)
        REFERENCES episode_video_generation_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_plan_id)
        REFERENCES shot_video_generation_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE,
    FOREIGN KEY(depends_on_shot_id) REFERENCES shots(id) ON DELETE CASCADE,
    FOREIGN KEY(upstream_adopted_version_id)
        REFERENCES shot_versions(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_video_plan_dependencies_upstream
    ON video_plan_dependencies(episode_video_plan_id, depends_on_shot_id);

CREATE TABLE IF NOT EXISTS video_boundary_assets (
    id TEXT PRIMARY KEY,
    episode_video_plan_id TEXT NOT NULL,
    shot_plan_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    source_revision_id TEXT,
    source_shot_id TEXT,
    source_adopted_version_id TEXT,
    path TEXT,
    url TEXT,
    sha256 TEXT,
    mime TEXT,
    width INTEGER,
    height INTEGER,
    qa_status TEXT NOT NULL DEFAULT 'pending',
    qa_json TEXT NOT NULL DEFAULT '{}',
    fingerprint TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(shot_plan_id, role, fingerprint),
    FOREIGN KEY(episode_video_plan_id)
        REFERENCES episode_video_generation_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_plan_id)
        REFERENCES shot_video_generation_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(shot_id) REFERENCES shots(id) ON DELETE CASCADE,
    FOREIGN KEY(source_shot_id) REFERENCES shots(id) ON DELETE SET NULL,
    FOREIGN KEY(source_adopted_version_id)
        REFERENCES shot_versions(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_video_boundary_assets_shot
    ON video_boundary_assets(shot_id, role, created_at DESC);

CREATE TABLE IF NOT EXISTS provider_media_publications (
    id TEXT PRIMARY KEY,
    source_revision_id TEXT NOT NULL,
    source_url TEXT,
    local_path TEXT,
    published_url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    mime TEXT NOT NULL,
    duration_s REAL,
    width INTEGER,
    height INTEGER,
    url_expires_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_media_publication_source
    ON provider_media_publications(source_revision_id, created_at DESC);

CREATE TABLE IF NOT EXISTS video_generation_attempts (
    id TEXT PRIMARY KEY,
    shot_plan_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    planned_mode TEXT NOT NULL,
    actual_mode TEXT NOT NULL,
    video_input_intent TEXT,
    status TEXT NOT NULL,
    provider_task_id TEXT,
    error TEXT,
    latency_ms INTEGER,
    cost REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(version_id, attempt_no),
    FOREIGN KEY(shot_plan_id)
        REFERENCES shot_video_generation_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_video_attempts_plan
    ON video_generation_attempts(shot_plan_id, attempt_no);

CREATE TABLE IF NOT EXISTS video_mode_qa_results (
    id TEXT PRIMARY KEY,
    shot_plan_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    planned_mode TEXT NOT NULL,
    actual_mode TEXT NOT NULL,
    technical_success INTEGER NOT NULL DEFAULT 0,
    semantic_success INTEGER,
    boundary_start_match REAL,
    boundary_end_match REAL,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(version_id, actual_mode),
    FOREIGN KEY(shot_plan_id)
        REFERENCES shot_video_generation_plans(id) ON DELETE CASCADE,
    FOREIGN KEY(version_id) REFERENCES shot_versions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_video_mode_qa_plan
    ON video_mode_qa_results(shot_plan_id, created_at DESC);
"""


def _open_connection(*, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _is_transient_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code is None:
        return False
    return (int(error_code) & 0xFF) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


def _run_write_transaction_once(
    operation: Callable[[sqlite3.Connection], _T],
) -> _T:
    try:
        conn = _open_connection(timeout=0)
    except sqlite3.OperationalError as exc:
        raise _WriteTransactionStartError(exc) from exc
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise _WriteTransactionStartError(exc) from exc
        result = operation(conn)
        conn.commit()
        return result
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


async def run_in_thread_cancellation_safe(operation: Callable[[], _T]) -> _T:
    """Run sync work off-loop and delay cancellation until its thread finishes."""
    worker_task = asyncio.create_task(asyncio.to_thread(operation))
    cancellation_requested = False
    while True:
        try:
            result = await asyncio.shield(worker_task)
        except asyncio.CancelledError:
            cancellation_requested = True
            if not worker_task.done():
                continue
            try:
                worker_task.result()
            except BaseException:
                pass
            raise
        except BaseException:
            if cancellation_requested:
                raise asyncio.CancelledError
            raise
        if cancellation_requested:
            raise asyncio.CancelledError
        return result


async def run_write_transaction(
    operation: Callable[[sqlite3.Connection], _T],
    *,
    retry_delays: tuple[float, ...] = ASYNC_WRITE_RETRY_DELAYS_S,
) -> _T:
    """Run one short transaction off-loop with bounded async lock retries.

    Cancellation waits for the current thread transaction to commit or roll
    back before it propagates, so no database write can land after the caller
    has already observed cancellation.
    """
    for attempt in range(len(retry_delays) + 1):
        try:
            return await run_in_thread_cancellation_safe(
                lambda: _run_write_transaction_once(operation)
            )
        except _WriteTransactionStartError as exc:
            if attempt >= len(retry_delays):
                raise exc.original from exc
            await asyncio.sleep(max(0.0, float(retry_delays[attempt])))
        except sqlite3.OperationalError as exc:
            if (
                not _is_transient_sqlite_lock(exc)
                or attempt >= len(retry_delays)
            ):
                raise
            await asyncio.sleep(max(0.0, float(retry_delays[attempt])))
    raise AssertionError("unreachable")


def _release_task_connection(task: asyncio.Task[Any]) -> None:
    with _task_connections_lock:
        conn = _task_connections.pop(task, None)
    if conn is None:
        return
    try:
        if conn.in_transaction:
            conn.rollback()
    finally:
        conn.close()


def get_conn() -> sqlite3.Connection:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    if task is not None:
        with _task_connections_lock:
            conn = _task_connections.get(task)
            if conn is None:
                conn = _open_connection()
                _task_connections[task] = conn
                task.add_done_callback(_release_task_connection)
        return conn

    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _open_connection()
        _local.conn = conn
    return conn


def _quarantine_static_delivery_fallbacks(conn: sqlite3.Connection) -> int:
    """隔离旧版曾生成的静态图/静音「伪视频」。

    这些行仅作历史证据保留：不再是 succeeded 候选，也不再持有
    ``adopted_version_id``。操作幂等，不删除用户文件。
    """
    fallback_rows = conn.execute(
        """SELECT v.id,s.episode_id,
                  CASE WHEN s.adopted_version_id=v.id THEN 1 ELSE 0 END AS was_adopted
             FROM shot_versions v
             LEFT JOIN shots s ON s.id=v.shot_id
            WHERE json_valid(v.image_inputs)
              AND COALESCE(json_extract(v.image_inputs,'$.delivery_fallback'),0)=1"""
    ).fetchall()
    ids = [str(row["id"]) for row in fallback_rows]
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE shots SET adopted_version_id=NULL WHERE adopted_version_id IN ({placeholders})",
        ids,
    )
    cursor = conn.execute(
        f"""UPDATE shot_versions
               SET status='rejected_static_fallback',
                   error=COALESCE(NULLIF(error,''), '历史静态图/静音占位已隔离，不具备视频资格')
             WHERE id IN ({placeholders})
               AND status!='rejected_static_fallback'""",
        ids,
    )
    # The adopted fallback may already have been snapshotted into a historical
    # delivery package.  Quarantine the media row and retire that package in
    # the same startup transaction so a restart cannot leave the stale package
    # downloadable through an unchanged episode pointer.
    from app.artifacts import invalidate_episode_delivery_authority

    for episode_id in sorted({
        str(row["episode_id"])
        for row in fallback_rows
        if row["episode_id"] and bool(row["was_adopted"])
    }):
        invalidate_episode_delivery_authority(conn, episode_id)
    return int(cursor.rowcount)


# 增量迁移：已有库上加列（首次建表时 SCHEMA 已含则忽略报错）
MIGRATIONS = (
    """CREATE TABLE IF NOT EXISTS media_cleanup_outbox (
           id TEXT PRIMARY KEY,
           episode_id TEXT NOT NULL,
           shot_id TEXT,
           payload_json TEXT NOT NULL DEFAULT '{}',
           status TEXT NOT NULL DEFAULT 'pending',
           attempts INTEGER NOT NULL DEFAULT 0,
           last_error TEXT,
           created_at REAL NOT NULL,
           completed_at REAL,
           FOREIGN KEY(episode_id) REFERENCES episodes(id) ON DELETE CASCADE
       )""",
    "CREATE INDEX IF NOT EXISTS idx_media_cleanup_outbox_pending "
    "ON media_cleanup_outbox(status, created_at)",
    "ALTER TABLE jobs ADD COLUMN after_shot_id TEXT",
    "ALTER TABLE jobs ADD COLUMN after_version_id TEXT",
    "ALTER TABLE jobs ADD COLUMN scene_kinds TEXT",
    "ALTER TABLE shot_versions ADD COLUMN image_inputs TEXT",
    "ALTER TABLE projects ADD COLUMN refs_status TEXT DEFAULT 'idle'",
    "ALTER TABLE projects ADD COLUMN refs_error TEXT",
    "ALTER TABLE projects ADD COLUMN refs_target TEXT",
    "ALTER TABLE projects ADD COLUMN bible_style_name TEXT",
    # 持久化定妆批次语义：0=按最新设定全量重生，1=仅续跑缺口。
    # 进程重启后必须保留这个区别，否则全量重生会被误恢复成“跳过旧成品”。
    "ALTER TABLE projects ADD COLUMN refs_resume INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE projects ADD COLUMN refs_batch_started_at REAL",
    "ALTER TABLE shots ADD COLUMN source_excerpt TEXT DEFAULT ''",
    "ALTER TABLE shots ADD COLUMN approved_scene_id TEXT",
    "ALTER TABLE shots ADD COLUMN approved_head_scene_id TEXT",
    "ALTER TABLE shots ADD COLUMN approved_tail_scene_id TEXT",
    "ALTER TABLE shots ADD COLUMN scene_status TEXT DEFAULT 'none'",  # none/generating/review/approved
    "ALTER TABLE shots ADD COLUMN shot_contract_json TEXT",
    "ALTER TABLE shots ADD COLUMN continuity_mode TEXT DEFAULT ''",
    "ALTER TABLE shots ADD COLUMN observed_state_out TEXT DEFAULT ''",
    "ALTER TABLE shot_scenes ADD COLUMN kind TEXT DEFAULT 'tail'",
    "ALTER TABLE shots ADD COLUMN first_frame_desc TEXT DEFAULT ''",
    "ALTER TABLE shots ADD COLUMN last_frame_desc TEXT DEFAULT ''",
    "ALTER TABLE shots ADD COLUMN script_id TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_json TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_status TEXT DEFAULT 'pending'",
    "ALTER TABLE episodes ADD COLUMN screenplay_error TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_started_at REAL",
    "ALTER TABLE episodes ADD COLUMN screenplay_updated_at REAL",
    "ALTER TABLE episodes ADD COLUMN screenplay_required_dialogues TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE shots ADD COLUMN mode_plan TEXT",
    "ALTER TABLE episode_video_generation_plans ADD COLUMN published_storyboard_artifact_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE episode_video_generation_plans ADD COLUMN published_storyboard_artifact_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE episode_video_generation_plans ADD COLUMN completion_certificate_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE episode_video_generation_plans ADD COLUMN narrative_review_artifact_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE episode_video_generation_plans ADD COLUMN narrative_calibration_artifact_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE episode_video_generation_plans ADD COLUMN release_qualification_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE projects ADD COLUMN bible_feedback TEXT",  # 持久化重谱打回要求，供进程重启后恢复人物谱任务
    "ALTER TABLE projects ADD COLUMN portraits_status TEXT DEFAULT 'idle'",  # 按集刷新定妆照任务状态
    "ALTER TABLE projects ADD COLUMN portraits_error TEXT",
    "ALTER TABLE provider_calls ADD COLUMN request_json TEXT",
    "ALTER TABLE provider_calls ADD COLUMN response_json TEXT",
    "ALTER TABLE episodes ADD COLUMN storyboard_outline_json TEXT",  # 分镜大纲（先规划后逐镜填充），供前端展示进度 k/N
    # 规划估算与已通过门禁的 outline 时长分轨保存。target_duration_s 在
    # outline 接受后是下游权威值，原估算保留用于剧本输入指纹和审计。
    "ALTER TABLE episodes ADD COLUMN planning_target_duration_s INTEGER",
    "ALTER TABLE episodes ADD COLUMN planning_duration_source TEXT",
    "ALTER TABLE episodes ADD COLUMN target_duration_authority TEXT NOT NULL DEFAULT 'planning_estimate'",
    "ALTER TABLE episodes ADD COLUMN storyboard_outline_revision INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE episodes ADD COLUMN storyboard_outline_fingerprint TEXT",
    "ALTER TABLE episodes ADD COLUMN storyboard_outline_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_artifact_id TEXT",
    "ALTER TABLE projects ADD COLUMN scene_refs_status TEXT DEFAULT 'idle'",  # 场景图素材库生成任务状态
    "ALTER TABLE projects ADD COLUMN scene_refs_error TEXT",
    "ALTER TABLE projects ADD COLUMN scene_refs_target TEXT",
    "ALTER TABLE projects ADD COLUMN scene_refs_batch_started_at REAL",
    "ALTER TABLE shots ADD COLUMN scene_name TEXT",  # 归一化命中的库内规范场景名（渲染期取场景库图复用）
    "ALTER TABLE shots ADD COLUMN scene_time TEXT DEFAULT ''",  # 独立时间标签，不参与场景图匹配
    "ALTER TABLE jobs ADD COLUMN run_id TEXT",
    "ALTER TABLE jobs ADD COLUMN owner_run_id TEXT",
    "ALTER TABLE jobs ADD COLUMN step_run_id TEXT",
    "ALTER TABLE jobs ADD COLUMN lease_owner TEXT",
    "ALTER TABLE jobs ADD COLUMN lease_expires_at REAL",
    "ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN next_retry_at REAL",
    "ALTER TABLE provider_calls ADD COLUMN run_id TEXT",
    "ALTER TABLE provider_calls ADD COLUMN step_run_id TEXT",
    "ALTER TABLE provider_calls ADD COLUMN trace_id TEXT",
    "ALTER TABLE shot_versions ADD COLUMN artifact_id TEXT",
    "ALTER TABLE shot_scenes ADD COLUMN artifact_id TEXT",
    "ALTER TABLE projects ADD COLUMN bible_artifact_id TEXT",
    "ALTER TABLE projects ADD COLUMN harness_engine_enabled INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE projects ADD COLUMN bible_draft_json TEXT",
    "ALTER TABLE projects ADD COLUMN bible_draft_updated_at REAL",
    "ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT",
    "ALTER TABLE episodes ADD COLUMN storyboard_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN delivery_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'not_ready'",
    "ALTER TABLE shots ADD COLUMN storyboard_artifact_id TEXT",
    "ALTER TABLE shot_versions ADD COLUMN technical_validation_json TEXT",
    "ALTER TABLE shot_versions ADD COLUMN adoption_reason TEXT",
    # 生成台预览/采纳定稿倍速；合成时按该值实际变速，不只是浏览器播放速度。
    "ALTER TABLE shot_versions ADD COLUMN playback_rate REAL NOT NULL DEFAULT 1.0",
    "ALTER TABLE shot_scenes ADD COLUMN adoption_reason TEXT",
    "ALTER TABLE jobs ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE jobs ADD COLUMN reserved_cost_cny REAL NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN cancellation_requested INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN provider_non_cancellable INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN provider_operation_id TEXT",
    "ALTER TABLE jobs ADD COLUMN provider_create_state TEXT NOT NULL DEFAULT 'not_started'",
    "ALTER TABLE jobs ADD COLUMN provider_failure_category TEXT",
    "ALTER TABLE jobs ADD COLUMN provider_failure_kind TEXT",
    "ALTER TABLE jobs ADD COLUMN provider_failure_disposition TEXT",
    "ALTER TABLE jobs ADD COLUMN provider_failure_retryable INTEGER",
    "ALTER TABLE jobs ADD COLUMN provider_submitted_at REAL",
    "ALTER TABLE jobs ADD COLUMN provider_poll_required INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN provider_result_adoptable INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE jobs ADD COLUMN abandoned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN attempt_started_at REAL",
    "ALTER TABLE character_portraits ADD COLUMN artifact_id TEXT",
    "ALTER TABLE scene_references ADD COLUMN artifact_id TEXT",
    "ALTER TABLE benchmark_runs ADD COLUMN is_real_project INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE benchmark_runs ADD COLUMN attested_by TEXT",
    "ALTER TABLE benchmark_runs ADD COLUMN attestation_note TEXT",
    "ALTER TABLE provider_calls ADD COLUMN operation_id TEXT",
    "ALTER TABLE provider_calls ADD COLUMN request_hash TEXT",
    "ALTER TABLE provider_calls ADD COLUMN contract_version TEXT",
    "ALTER TABLE provider_calls ADD COLUMN production_grant_id TEXT",
    "ALTER TABLE provider_calls ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE provider_calls ADD COLUMN supersedes_call_id INTEGER",
    "ALTER TABLE provider_calls ADD COLUMN superseded_by_call_id INTEGER",
    "ALTER TABLE provider_calls ADD COLUMN recovery_disposition TEXT",
    "ALTER TABLE provider_calls ADD COLUMN first_chunk_at REAL",
    "ALTER TABLE provider_calls ADD COLUMN last_chunk_at REAL",
    "ALTER TABLE provider_calls ADD COLUMN received_chars INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE provider_calls ADD COLUMN project_id TEXT",
    "ALTER TABLE workflow_runs ADD COLUMN recovered_by_run_id TEXT",
    "ALTER TABLE workflow_runs ADD COLUMN recovered_at REAL",
    "ALTER TABLE workflow_runs ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0",
    # 分镜增强项降级可见（大纲失败/场景库维护失败），不覆盖 script_error
    "ALTER TABLE episodes ADD COLUMN storyboard_warning TEXT",
    "ALTER TABLE episodes ADD COLUMN active_storyboard_run_id TEXT",
    "ALTER TABLE episodes ADD COLUMN active_video_run_id TEXT",
    "ALTER TABLE episodes ADD COLUMN video_completion_mode TEXT NOT NULL DEFAULT 'quick'",
    "ALTER TABLE episodes ADD COLUMN video_control_json TEXT",
    # QPSP：权威阶段字段与参考图槽位检查点
    "ALTER TABLE jobs ADD COLUMN pipeline_stage TEXT",
    "ALTER TABLE jobs ADD COLUMN stage_status TEXT",
    "ALTER TABLE jobs ADD COLUMN stage_started_at REAL",
    "ALTER TABLE jobs ADD COLUMN stage_updated_at REAL",
    "ALTER TABLE jobs ADD COLUMN stage_progress_json TEXT",
    "ALTER TABLE jobs ADD COLUMN reason_code TEXT",
    "ALTER TABLE jobs ADD COLUMN reason_text TEXT",
    "ALTER TABLE jobs ADD COLUMN scheduler_lane TEXT",
    "ALTER TABLE jobs ADD COLUMN priority_class TEXT",
    "ALTER TABLE jobs ADD COLUMN ready_at REAL",
    "ALTER TABLE jobs ADD COLUMN state_revision INTEGER NOT NULL DEFAULT 0",
    # 同镜视频付费链路使用显式数据库活动槽，不依赖 request key 或调用来源。
    "ALTER TABLE jobs ADD COLUMN video_slot_active INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE shot_versions ADD COLUMN video_slot_active INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reference_sets ADD COLUMN static_ready INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reference_sets ADD COLUMN continuity_ready INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reference_sets ADD COLUMN group_gate_passed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reference_sets ADD COLUMN input_fingerprint TEXT",
    "ALTER TABLE reference_assets ADD COLUMN slot_key TEXT",
    "ALTER TABLE reference_assets ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE reference_assets ADD COLUMN generation_status TEXT",
    "ALTER TABLE reference_assets ADD COLUMN qa_status TEXT",
    "ALTER TABLE reference_assets ADD COLUMN input_fingerprint TEXT",
    # 人物/场景多视角资产包 + 关键帧证据链
    "ALTER TABLE character_portraits ADD COLUMN pack_status TEXT NOT NULL DEFAULT 'legacy_partial'",
    "ALTER TABLE character_portraits ADD COLUMN group_qa_json TEXT",
    "ALTER TABLE character_portraits ADD COLUMN change_json TEXT",
    "ALTER TABLE character_portraits ADD COLUMN input_fingerprint TEXT",
    "ALTER TABLE scene_references ADD COLUMN pack_status TEXT NOT NULL DEFAULT 'legacy_partial'",
    "ALTER TABLE scene_references ADD COLUMN group_qa_json TEXT",
    "ALTER TABLE scene_references ADD COLUMN state_canonical TEXT",
    "ALTER TABLE scene_references ADD COLUMN change_json TEXT",
    "ALTER TABLE scene_references ADD COLUMN input_fingerprint TEXT",
    "ALTER TABLE reference_assets ADD COLUMN entity_type TEXT",
    "ALTER TABLE reference_assets ADD COLUMN entity_name TEXT",
    "ALTER TABLE reference_assets ADD COLUMN library_revision_id TEXT",
    "ALTER TABLE reference_assets ADD COLUMN library_view_id TEXT",
    "ALTER TABLE reference_assets ADD COLUMN view_role TEXT",
    "ALTER TABLE reference_assets ADD COLUMN purposes_json TEXT",
    "ALTER TABLE reference_assets ADD COLUMN required INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE reference_assets ADD COLUMN dependency_manifest_json TEXT",
    "ALTER TABLE reference_sets ADD COLUMN dependency_manifest_json TEXT",
    "ALTER TABLE reference_sets ADD COLUMN frozen INTEGER NOT NULL DEFAULT 0",
    # Production Repair：Working / Published 双指针与完成凭证
    "ALTER TABLE episodes ADD COLUMN active_screenplay_run_id TEXT",
    "ALTER TABLE episodes ADD COLUMN working_screenplay_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN published_screenplay_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN working_storyboard_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN published_storyboard_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_production_revision_id TEXT",
    "ALTER TABLE episodes ADD COLUMN storyboard_production_revision_id TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_completion_certificate_id TEXT",
    "ALTER TABLE episodes ADD COLUMN storyboard_completion_certificate_id TEXT",
    "ALTER TABLE episodes ADD COLUMN narrative_status TEXT NOT NULL DEFAULT 'needs_review'",
    "ALTER TABLE episodes ADD COLUMN narrative_review_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN narrative_calibration_artifact_id TEXT",
    # 剧本台安全发布、occurrence 约束与轻量状态快照。
    "ALTER TABLE episodes ADD COLUMN screenplay_required_dialogue_occurrences TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE episodes ADD COLUMN screenplay_publish_fence INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE episodes ADD COLUMN screenplay_snapshot_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE episodes ADD COLUMN screenplay_constraint_version INTEGER NOT NULL DEFAULT 0",
    # 人物姓名消歧是剧本生产输入的一部分，必须跨进程恢复、Patch 和手工发布保留。
    "ALTER TABLE episodes ADD COLUMN screenplay_character_resolutions TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE shots ADD COLUMN shot_uid TEXT",
    # Generated chapter-title cards may bind a short, exact authorized
    # paratext line without weakening generic source-excerpt match thresholds.
    "ALTER TABLE storyboard_source_bindings ADD COLUMN binding_kind TEXT "
    "NOT NULL DEFAULT 'source_excerpt'",
    # QA score-only metadata for typed Evaluation rows.
    "ALTER TABLE evaluations ADD COLUMN evaluation_role TEXT",
    "ALTER TABLE evaluations ADD COLUMN score_status TEXT",
    "ALTER TABLE evaluations ADD COLUMN runtime_blocking INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE evaluations ADD COLUMN retry_eligible INTEGER NOT NULL DEFAULT 0",
)


INTEGRITY_SCHEMA = """
-- This index depends on columns added by MIGRATIONS for legacy databases, so it
-- must be created only after the additive migration pass has completed.
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, next_retry_at, lease_expires_at, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_jobs_owner_run ON jobs(owner_run_id, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_video_shot
    ON jobs(shot_id)
    WHERE kind='video' AND shot_id IS NOT NULL AND video_slot_active=1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_versions_active_video_shot
    ON shot_versions(shot_id)
    WHERE video_slot_active=1;
CREATE INDEX IF NOT EXISTS idx_provider_calls_operation ON provider_calls(operation_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_provider_calls_project ON provider_calls(project_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_provider_calls_ts ON provider_calls(ts, id);
CREATE INDEX IF NOT EXISTS idx_monitor_audit_object ON monitor_audit(object_type, object_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS uq_chapters_project_idx ON chapters(project_id, idx);
CREATE UNIQUE INDEX IF NOT EXISTS uq_episodes_project_no ON episodes(project_id, episode_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_screenplay_drafts_episode ON screenplay_drafts(episode_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shots_episode_no ON shots(episode_id, shot_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shots_uid ON shots(shot_uid) WHERE shot_uid IS NOT NULL AND shot_uid!='';
CREATE UNIQUE INDEX IF NOT EXISTS uq_versions_shot_no ON shot_versions(shot_id, version_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scenes_shot_kind_no ON shot_scenes(shot_id, kind, version_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_portraits_segment ON character_portraits(project_id, character_name, ep_start);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scene_refs_segment ON scene_references(project_id, scene_name, ep_start);

-- 旧数据库无法原地 ALTER TABLE 增加外键；触发器为存量库提供等价的父记录校验与级联。
CREATE TRIGGER IF NOT EXISTS guard_chapter_project BEFORE INSERT ON chapters
WHEN NOT EXISTS (SELECT 1 FROM projects WHERE id=NEW.project_id)
BEGIN SELECT RAISE(ABORT, 'chapters.project_id missing'); END;
CREATE TRIGGER IF NOT EXISTS guard_episode_project BEFORE INSERT ON episodes
WHEN NOT EXISTS (SELECT 1 FROM projects WHERE id=NEW.project_id)
BEGIN SELECT RAISE(ABORT, 'episodes.project_id missing'); END;
CREATE TRIGGER IF NOT EXISTS guard_shot_screenplay_publish_fence BEFORE INSERT ON shots
WHEN EXISTS (
    SELECT 1 FROM episodes e
    WHERE e.id=NEW.episode_id AND e.screenplay_publish_fence=1
)
BEGIN SELECT RAISE(ABORT, 'screenplay publish fence rejects storyboard write'); END;
CREATE TRIGGER IF NOT EXISTS guard_shot_episode BEFORE INSERT ON shots
WHEN NOT EXISTS (SELECT 1 FROM episodes WHERE id=NEW.episode_id)
BEGIN SELECT RAISE(ABORT, 'shots.episode_id missing'); END;
CREATE TRIGGER IF NOT EXISTS guard_version_shot BEFORE INSERT ON shot_versions
WHEN NOT EXISTS (SELECT 1 FROM shots WHERE id=NEW.shot_id)
BEGIN SELECT RAISE(ABORT, 'shot_versions.shot_id missing'); END;
CREATE TRIGGER IF NOT EXISTS guard_scene_shot BEFORE INSERT ON shot_scenes
WHEN NOT EXISTS (SELECT 1 FROM shots WHERE id=NEW.shot_id)
BEGIN SELECT RAISE(ABORT, 'shot_scenes.shot_id missing'); END;
CREATE TRIGGER IF NOT EXISTS guard_portrait_project BEFORE INSERT ON character_portraits
WHEN NOT EXISTS (SELECT 1 FROM projects WHERE id=NEW.project_id)
BEGIN SELECT RAISE(ABORT, 'character_portraits.project_id missing'); END;
CREATE TRIGGER IF NOT EXISTS guard_scene_ref_project BEFORE INSERT ON scene_references
WHEN NOT EXISTS (SELECT 1 FROM projects WHERE id=NEW.project_id)
BEGIN SELECT RAISE(ABORT, 'scene_references.project_id missing'); END;

CREATE TRIGGER IF NOT EXISTS cascade_project AFTER DELETE ON projects
BEGIN
  DELETE FROM chapters WHERE project_id=OLD.id;
  DELETE FROM episodes WHERE project_id=OLD.id;
  DELETE FROM jobs WHERE project_id=OLD.id;
  DELETE FROM character_portraits WHERE project_id=OLD.id;
  DELETE FROM scene_references WHERE project_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS cascade_episode AFTER DELETE ON episodes
BEGIN
  DELETE FROM shots WHERE episode_id=OLD.id;
  DELETE FROM jobs WHERE episode_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS cascade_shot AFTER DELETE ON shots
BEGIN
  DELETE FROM shot_versions WHERE shot_id=OLD.id;
  DELETE FROM shot_scenes WHERE shot_id=OLD.id;
  DELETE FROM jobs WHERE shot_id=OLD.id;
END;
CREATE TRIGGER IF NOT EXISTS cascade_version AFTER DELETE ON shot_versions
BEGIN DELETE FROM jobs WHERE version_id=OLD.id; END;
"""


def _integrity_findings(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Return machine-readable counts and bounded identifiers before a repair."""
    checks = {
        "orphan_shot_versions": (
            "shot_versions", "NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=shot_versions.shot_id)"
        ),
        "orphan_shot_scenes": (
            "shot_scenes", "NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=shot_scenes.shot_id)"
        ),
        "orphan_shots": (
            "shots", "NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id=shots.episode_id)"
        ),
        "orphan_episodes": (
            "episodes", "NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=episodes.project_id)"
        ),
        "orphan_chapters": (
            "chapters", "NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=chapters.project_id)"
        ),
        "orphan_portraits": (
            "character_portraits",
            "NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=character_portraits.project_id)",
        ),
        "orphan_scene_refs": (
            "scene_references",
            "NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=scene_references.project_id)",
        ),
        "orphan_jobs": (
            "jobs",
            "(project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=jobs.project_id)) OR "
            "(episode_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id=jobs.episode_id)) OR "
            "(shot_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=jobs.shot_id)) OR "
            "(version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shot_versions v WHERE v.id=jobs.version_id))",
        ),
    }
    findings: dict[str, dict[str, Any]] = {}
    for key, (table, predicate) in checks.items():
        rows = conn.execute(
            f"SELECT rowid, * FROM {table} WHERE {predicate} ORDER BY rowid LIMIT 100"
        ).fetchall()
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {table} WHERE {predicate}").fetchone()["c"]
        findings[key] = {
            "count": int(count),
            "identifiers": [str(row["id"] if "id" in row.keys() else row["rowid"]) for row in rows],
        }

    duplicate_checks = {
        "duplicate_chapters": ("chapters", "project_id, idx"),
        "duplicate_episodes": ("episodes", "project_id, episode_no"),
        "duplicate_shots": ("shots", "episode_id, shot_no"),
        "duplicate_versions": ("shot_versions", "shot_id, version_no"),
        "duplicate_scenes": ("shot_scenes", "shot_id, kind, version_no"),
        "duplicate_portrait_segments": (
            "character_portraits", "project_id, character_name, ep_start"
        ),
        "duplicate_scene_ref_segments": (
            "scene_references", "project_id, scene_name, ep_start"
        ),
    }
    for key, (table, columns) in duplicate_checks.items():
        groups = conn.execute(
            f"SELECT {columns}, COUNT(*) AS c FROM {table} GROUP BY {columns} HAVING COUNT(*)>1 LIMIT 100"
        ).fetchall()
        extra_count = conn.execute(
            f"SELECT COALESCE(SUM(c-1), 0) AS c FROM ("
            f"SELECT COUNT(*) AS c FROM {table} GROUP BY {columns} HAVING COUNT(*)>1)"
        ).fetchone()["c"]
        findings[key] = {
            "count": int(extra_count),
            "identifiers": ["|".join(str(row[column.strip()]) for column in columns.split(",")) for row in groups],
        }
    return findings


def _backup_before_integrity_repair(conn: sqlite3.Connection, stamp: str) -> str | None:
    database_path = next(
        (row[2] for row in conn.execute("PRAGMA database_list").fetchall() if row[1] == "main"),
        "",
    )
    if not database_path:
        return None
    backup_dir = DATA_DIR / "integrity_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"manju-before-repair-{stamp}.db"
    target = sqlite3.connect(backup_path)
    try:
        conn.backup(target)
    finally:
        target.close()
    return str(backup_path)


def _reconcile_video_slot_activity(conn: sqlite3.Connection) -> int:
    """Separate one local generation slot from every accepted provider task."""
    if (
        "video_slot_active" not in _column_names(conn, "jobs")
        or "video_slot_active" not in _column_names(conn, "shot_versions")
        or "provider_poll_required" not in _column_names(conn, "jobs")
        or "provider_result_adoptable" not in _column_names(conn, "jobs")
    ):
        return 0
    has_provider_claim_ledger = _table_exists(
        conn, "provider_video_budget_claims"
    )
    closed_claim_filter = (
        """AND NOT EXISTS (
                      SELECT 1 FROM provider_video_budget_claims c
                       WHERE c.job_id=j.id
                         AND c.operation_id=j.provider_operation_id
                         AND c.status IN (
                           'released','settled','closed_liability'
                         )
                    )"""
        if has_provider_claim_ledger
        else ""
    )
    rows = conn.execute(
        f"""SELECT j.id,j.shot_id,j.version_id,j.status,j.updated_at,
                  j.lease_owner,j.provider_non_cancellable,j.provider_create_state,
                  j.provider_operation_id,j.provider_submitted_at,
                  j.episode_id,j.project_id,j.cancellation_requested,j.abandoned,
                  j.video_slot_active,j.provider_result_adoptable,
                  v.provider_task_id,v.video_path,
                  br.amount_cny AS reservation_amount,
                  CASE WHEN j.cancellation_requested=0 AND j.abandoned=0
                         AND NOT (
                           j.provider_poll_required=1
                           AND j.provider_result_adoptable=0
                         )
                         AND (
                           j.status IN (
                             'queued','running','waiting_provider','waiting_retry',
                             'waiting_human','waiting','paused'
                           )
                           OR (j.status='stale' AND j.provider_non_cancellable=1)
                         )
                       THEN 1 ELSE 0 END AS locally_active
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
             LEFT JOIN budget_reservations br ON br.job_id=j.id
            WHERE j.kind='video' AND j.shot_id IS NOT NULL
              AND (
                  (
                    j.cancellation_requested=0 AND j.abandoned=0
                    AND (
                      j.status IN (
                        'queued','running','waiting_provider','waiting_retry',
                        'waiting_human','waiting','paused'
                      )
                      OR (j.status='stale' AND j.provider_non_cancellable=1)
                    )
                  )
                  OR (
                    j.provider_create_state='accepted'
                    AND (
                      (v.provider_task_id IS NOT NULL AND v.provider_task_id!='')
                      OR j.provider_non_cancellable=1
                    )
                    {closed_claim_filter}
                  )
              )
            ORDER BY j.shot_id,
              CASE
                WHEN j.video_slot_active=1 THEN 0
                WHEN j.provider_create_state='accepted'
                     AND v.provider_task_id IS NOT NULL
                     AND v.provider_task_id!='' THEN 1
                WHEN j.provider_result_adoptable=1
                     AND j.cancellation_requested=0 AND j.abandoned=0 THEN 2
                WHEN j.lease_owner IS NOT NULL AND j.lease_owner!='' THEN 3
                ELSE 4
              END,
              j.updated_at DESC,j.id DESC"""
    ).fetchall()
    conn.execute(
        "UPDATE jobs SET video_slot_active=0 WHERE kind='video'"
    )
    conn.execute("UPDATE shot_versions SET video_slot_active=0")
    by_shot: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_shot.setdefault(str(row["shot_id"]), []).append(row)

    reconciled = 0
    stamp = now()
    for shot_rows in by_shot.values():
        owner = next(
            (row for row in shot_rows if bool(row["locally_active"])),
            None,
        )
        if owner is not None:
            conn.execute(
                """UPDATE jobs
                      SET video_slot_active=1,provider_result_adoptable=1
                    WHERE id=?""",
                (owner["id"],),
            )
            if owner["version_id"]:
                conn.execute(
                    "UPDATE shot_versions SET video_slot_active=1 WHERE id=?",
                    (owner["version_id"],),
                )
        for row in shot_rows:
            is_owner = owner is not None and row["id"] == owner["id"]
            provider_accepted = bool(
                row["provider_create_state"] == "accepted"
                and (row["provider_task_id"] or row["provider_non_cancellable"])
            )
            if provider_accepted:
                operation_id = (
                    str(row["provider_operation_id"] or "").strip()
                    or f"video-create-{row['version_id']}"
                )
                message = (
                    "数据库同镜活动槽迁移：继续轮询原供应商任务；"
                    + (
                        "该任务保留生成结果采用资格"
                        if is_owner
                        else "该历史结果只入隔离审计，不参与采用"
                    )
                )
                if not is_owner:
                    conn.execute(
                        """UPDATE jobs
                              SET status='waiting_provider',video_slot_active=0,
                                  provider_poll_required=1,
                                  provider_result_adoptable=0,
                                  provider_operation_id=?,
                                  cancellation_requested=0,abandoned=0,
                                  lease_owner=NULL,lease_expires_at=NULL,
                                  next_retry_at=?,error=?,updated_at=?
                            WHERE id=?""",
                        (operation_id, stamp, message, stamp, row["id"]),
                    )
                    if row["version_id"]:
                        conn.execute(
                            """UPDATE shot_versions
                                  SET status='waiting_provider',
                                      video_slot_active=0,error=?
                                WHERE id=? AND video_path IS NULL""",
                            (message, row["version_id"]),
                        )
                    conn.execute(
                        """UPDATE budget_reservations
                              SET status='reserved',settled_at=NULL,
                                  actual_cost_cny=NULL
                            WHERE job_id=?""",
                        (row["id"],),
                    )
                    reconciled += 1
                else:
                    conn.execute(
                        """UPDATE jobs
                              SET provider_poll_required=1,
                                  provider_result_adoptable=1,
                                  provider_operation_id=?
                            WHERE id=?""",
                        (operation_id, row["id"]),
                    )
                if (
                    has_provider_claim_ledger
                    and
                    row["project_id"]
                    and row["episode_id"]
                    and row["version_id"]
                ):
                    accepted_at = float(
                        row["provider_submitted_at"] or row["updated_at"] or stamp
                    )
                    conn.execute(
                        """INSERT OR IGNORE INTO provider_video_budget_claims(
                               operation_id,project_id,episode_id,shot_id,
                               job_id,version_id,origin_episode_id,origin_shot_id,
                               origin_job_id,origin_version_id,amount_cny,status,
                               created_at,updated_at,accepted_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?)""",
                        (
                            operation_id,
                            row["project_id"],
                            row["episode_id"],
                            row["shot_id"],
                            row["id"],
                            row["version_id"],
                            row["episode_id"],
                            row["shot_id"],
                            row["id"],
                            row["version_id"],
                            float(row["reservation_amount"] or 0),
                            accepted_at,
                            stamp,
                            accepted_at,
                        ),
                    )
                continue
            if is_owner:
                continue
            pre_transport_cancel = bool(
                not row["provider_non_cancellable"]
                and not row["provider_task_id"]
                and row["provider_create_state"] in {
                    "",
                    "not_started",
                    "submitting",
                }
            )
            message = (
                "数据库同镜活动槽迁移：未提交供应商的历史重复任务已关闭，"
                f"活动所有者为 {owner['id'] if owner is not None else 'none'}"
            )
            conn.execute(
                """UPDATE jobs
                      SET status='cancelled',video_slot_active=0,
                          provider_poll_required=0,provider_result_adoptable=0,
                          cancellation_requested=1,abandoned=0,
                          provider_create_state=CASE WHEN ? THEN 'not_started'
                                                     ELSE provider_create_state END,
                          provider_non_cancellable=CASE WHEN ? THEN 0
                                                        ELSE provider_non_cancellable END,
                          lease_owner=NULL,lease_expires_at=NULL,
                          next_retry_at=NULL,error=?,updated_at=?
                    WHERE id=?""",
                (
                    int(pre_transport_cancel),
                    int(pre_transport_cancel),
                    message,
                    stamp,
                    row["id"],
                ),
            )
            if row["version_id"]:
                conn.execute(
                    """UPDATE shot_versions
                          SET status='cancelled',video_slot_active=0,error=?
                        WHERE id=?""",
                    (message, row["version_id"]),
                )
            conn.execute(
                """UPDATE budget_reservations
                      SET status='released',settled_at=?,actual_cost_cny=0
                    WHERE job_id=? AND status IN ('reserved','running')""",
                (stamp, row["id"]),
            )
            reconciled += 1
            if (
                has_provider_claim_ledger
                and pre_transport_cancel
                and row["provider_operation_id"]
            ):
                conn.execute(
                    """UPDATE provider_video_budget_claims
                          SET status='released',updated_at=?,released_at=?
                        WHERE operation_id=? AND job_id=? AND status='reserved'
                          AND accepted_at IS NULL""",
                    (
                        stamp,
                        stamp,
                        row["provider_operation_id"],
                        row["id"],
                    ),
                )
    return reconciled
    return reconciled


def _repair_integrity(conn: sqlite3.Connection) -> dict[str, Any]:
    """Back up, report, repair and re-check legacy orphan/duplicate rows."""
    before = _integrity_findings(conn)
    repair_count = sum(item["count"] for item in before.values())
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:8]}"
    backup_path = _backup_before_integrity_repair(conn, stamp) if repair_count else None
    conn.executescript("""
        UPDATE shot_scenes SET kind='tail' WHERE kind IS NULL OR TRIM(kind)='';
        DELETE FROM shot_versions WHERE NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=shot_versions.shot_id);
        DELETE FROM shot_scenes WHERE NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=shot_scenes.shot_id);
        DELETE FROM shots WHERE NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id=shots.episode_id);
        DELETE FROM episodes WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=episodes.project_id);
        DELETE FROM chapters WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=chapters.project_id);
        DELETE FROM character_portraits WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=character_portraits.project_id);
        DELETE FROM scene_references WHERE NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=scene_references.project_id);
        DELETE FROM jobs WHERE
          (project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=jobs.project_id)) OR
          (episode_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id=jobs.episode_id)) OR
          (shot_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=jobs.shot_id)) OR
          (version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shot_versions v WHERE v.id=jobs.version_id));

        DELETE FROM chapters WHERE rowid NOT IN (SELECT MIN(rowid) FROM chapters GROUP BY project_id, idx);
        DELETE FROM episodes WHERE rowid NOT IN (SELECT MIN(rowid) FROM episodes GROUP BY project_id, episode_no);
        DELETE FROM shots WHERE rowid NOT IN (SELECT MIN(rowid) FROM shots GROUP BY episode_id, shot_no);
        DELETE FROM shot_versions WHERE rowid NOT IN (SELECT MIN(rowid) FROM shot_versions GROUP BY shot_id, version_no);
        DELETE FROM shot_scenes WHERE rowid NOT IN (SELECT MIN(rowid) FROM shot_scenes GROUP BY shot_id, kind, version_no);
        DELETE FROM character_portraits WHERE rowid NOT IN (
          SELECT MIN(rowid) FROM character_portraits GROUP BY project_id, character_name, ep_start);
        DELETE FROM scene_references WHERE rowid NOT IN (
          SELECT MIN(rowid) FROM scene_references GROUP BY project_id, scene_name, ep_start);
        DELETE FROM screenplay_drafts WHERE rowid NOT IN (
          SELECT rowid FROM screenplay_drafts latest
           WHERE latest.rowid=(
             SELECT newer.rowid FROM screenplay_drafts newer
              WHERE newer.episode_id=latest.episode_id
              ORDER BY newer.updated_at DESC, newer.rowid DESC LIMIT 1
           ));

        -- Removing duplicate parents can create new orphans in legacy databases that
        -- did not yet have foreign keys/triggers. Run the child cleanup once more.
        DELETE FROM shot_versions WHERE NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=shot_versions.shot_id);
        DELETE FROM shot_scenes WHERE NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=shot_scenes.shot_id);
        DELETE FROM shots WHERE NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id=shots.episode_id);
        DELETE FROM jobs WHERE
          (project_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM projects p WHERE p.id=jobs.project_id)) OR
          (episode_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM episodes e WHERE e.id=jobs.episode_id)) OR
          (shot_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.id=jobs.shot_id)) OR
          (version_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM shot_versions v WHERE v.id=jobs.version_id));
    """)
    video_slot_repairs = _reconcile_video_slot_activity(conn)
    conn.executescript(INTEGRITY_SCHEMA)
    after = _integrity_findings(conn)
    report = {
        "schema_version": "1.0.0",
        "created_at": time.time(),
        "backup_path": backup_path,
        "repair_count": repair_count + video_slot_repairs,
        "video_slot_repairs": video_slot_repairs,
        "before": before,
        "after": after,
        "remaining_count": sum(item["count"] for item in after.values()),
    }
    database_path = next(
        (row[2] for row in conn.execute("PRAGMA database_list").fetchall() if row[1] == "main"),
        "",
    )
    if database_path and repair_count:
        report_dir = DATA_DIR / "integrity_reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"integrity-{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(report_path)
    return report


def _reconcile_settled_orphan_blueprint_shards(conn: sqlite3.Connection) -> None:
    """Terminalize orphan INTERRUPTED blueprint shards that later completed.

    An interrupted blueprint-shard provider call left with
    ``superseded_by_call_id IS NULL`` counts as unresolved "unknown" liability
    forever (``_BlueprintGenerationBudget.from_durable_calls``). The normal
    resolution path only supersedes the exact receipt set pinned by the run
    that rebuilt the blueprint, so an orphan from a crashed run or an older
    lineage can linger until retention prunes it.

    This resolves ONLY the provably-settled ones: an orphan whose episode has a
    later ``VALIDATED_BLUEPRINT_AUTHORITY`` resolution is superseded by that
    resolution. A validated authority proves the blueprint operation completed
    and was settled, so linking the orphan to it cannot drop real unknown
    liability or risk a double-charge. Orphans without such a successor are left
    untouched — the retry path (with a valid grant) resolves them on the next
    successful rebuild.
    """
    try:
        conn.execute(
            """
            UPDATE provider_calls
               SET superseded_by_call_id=(
                       SELECT r.id FROM provider_calls r
                        WHERE r.recovery_disposition='VALIDATED_BLUEPRINT_AUTHORITY'
                          AND json_extract(r.meta,'$.episode_id')
                              =json_extract(provider_calls.meta,'$.episode_id')
                          AND r.ts>=provider_calls.ts
                        ORDER BY r.ts LIMIT 1
                   ),
                   recovery_disposition='RECONCILED_SUPERSEDED_BY_LATER_AUTHORITY'
             WHERE status IN ('INTERRUPTED','RUNNING')
               AND superseded_by_call_id IS NULL
               AND json_extract(meta,'$.stage_key') LIKE 'screenplay_blueprint%'
               AND json_extract(meta,'$.episode_id') IS NOT NULL
               AND EXISTS(
                       SELECT 1 FROM provider_calls r
                        WHERE r.recovery_disposition='VALIDATED_BLUEPRINT_AUTHORITY'
                          AND json_extract(r.meta,'$.episode_id')
                              =json_extract(provider_calls.meta,'$.episode_id')
                          AND r.ts>=provider_calls.ts
                   )
            """
        )
    except sqlite3.OperationalError:
        # Legacy/partial schemas (e.g. no json1) must not block startup.
        return


def _repair_invalid_provider_metadata(conn: sqlite3.Connection) -> None:
    """Replace legacy character-truncated metadata with valid audit summaries."""
    try:
        rows = conn.execute(
            "SELECT id,meta FROM provider_calls "
            "WHERE meta IS NOT NULL AND json_valid(meta)=0"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    for row in rows:
        raw = str(row["meta"] or "")
        summary = {
            "_legacy_invalid": True,
            "_original_chars": len(raw),
            "_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        }
        conn.execute(
            "UPDATE provider_calls SET meta=? WHERE id=?",
            (json.dumps(summary, ensure_ascii=False, sort_keys=True), row["id"]),
        )


def _prune_observability_logs(conn: sqlite3.Connection) -> None:
    """Bound diagnostic tables so routine monitoring cannot grow the DB forever."""
    def retention_days(key: str, fallback: int) -> int:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        try:
            return max(1, int(row["value"] if row else fallback))
        except (TypeError, ValueError):
            return fallback

    stamp = time.time()
    calls_cutoff = stamp - retention_days("provider_call_retention_days", 30) * 86400
    errors_cutoff = stamp - retention_days("error_log_retention_days", 30) * 86400
    conn.execute(
        "DELETE FROM provider_calls WHERE ts < ? AND status != 'RUNNING'", (calls_cutoff,)
    )
    conn.execute("DELETE FROM error_logs WHERE ts < ?", (errors_cutoff,))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()


def _drop_obsolete_storyboard_columns(conn: sqlite3.Connection) -> None:
    """移除已退出主链路的分镜采纳与自动确认字段。

    旧版可能把部分镜头标为不采纳；新链路要求人工确认后的每个镜头都进入生成台，
    因此删除字段前先恢复为全量参与，避免升级时继续保留一条隐藏分支。
    """
    shot_columns = _column_names(conn, "shots")
    if "storyboard_adopted" in shot_columns:
        conn.execute("UPDATE shots SET storyboard_adopted=1 WHERE storyboard_adopted<>1")
        try:
            conn.execute("ALTER TABLE shots DROP COLUMN storyboard_adopted")
        except sqlite3.OperationalError:
            # 兼容不支持 DROP COLUMN 的旧 SQLite；字段已归一且业务代码不再读取。
            pass
    episode_columns = _column_names(conn, "episodes")
    if "storyboard_completion_mode" in episode_columns:
        try:
            conn.execute("ALTER TABLE episodes DROP COLUMN storyboard_completion_mode")
        except sqlite3.OperationalError:
            pass


def _backfill_multiview_assets(conn: sqlite3.Connection) -> None:
    """旧单图资产回填为多视角子记录；幂等可重复执行。"""
    stamp = now()
    if _table_exists(conn, "character_portrait_views"):
        cols = _column_names(conn, "character_portraits")
        portraits = conn.execute("SELECT * FROM character_portraits").fetchall()
        for row in portraits:
            existing = conn.execute(
                "SELECT id FROM character_portrait_views WHERE portrait_id=? AND view_role='front_full'",
                (row["id"],),
            ).fetchone()
            if not existing and row["image_path"]:
                view_id = new_id("pview")
                conn.execute(
                    """INSERT INTO character_portrait_views(
                           id, portrait_id, view_role, framing, image_path, prompt, qa_json,
                           artifact_id, base_view_id, status, selected, input_fingerprint, created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        view_id, row["id"], "front_full", "full_body", row["image_path"],
                        row["prompt"], None, row["artifact_id"] if "artifact_id" in row.keys() else None,
                        None, "ready", 1, None, stamp,
                    ),
                )
            if "pack_status" in cols:
                status = row["pack_status"] if "pack_status" in row.keys() and row["pack_status"] else None
                if not status:
                    conn.execute(
                        "UPDATE character_portraits SET pack_status=? WHERE id=?",
                        ("legacy_partial", row["id"]),
                    )
    if _table_exists(conn, "scene_reference_views"):
        cols = _column_names(conn, "scene_references")
        scenes = conn.execute("SELECT * FROM scene_references").fetchall()
        for row in scenes:
            existing = conn.execute(
                "SELECT id FROM scene_reference_views WHERE scene_reference_id=? AND view_role='establishing'",
                (row["id"],),
            ).fetchone()
            if not existing and row["image_path"]:
                view_id = new_id("sview")
                conn.execute(
                    """INSERT INTO scene_reference_views(
                           id, scene_reference_id, view_role, camera_axis, image_path, prompt, qa_json,
                           artifact_id, base_view_id, status, selected, input_fingerprint, created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        view_id, row["id"], "establishing", "establishing", row["image_path"],
                        row["prompt"], row["qa_json"],
                        row["artifact_id"] if "artifact_id" in row.keys() else None,
                        None, "ready", 1, None, stamp,
                    ),
                )
            if "pack_status" in cols:
                status = row["pack_status"] if "pack_status" in row.keys() and row["pack_status"] else None
                if not status:
                    conn.execute(
                        "UPDATE scene_references SET pack_status=? WHERE id=?",
                        ("legacy_partial", row["id"]),
                    )


def init_db(*, reconcile_interrupted: bool = False) -> None:
    """初始化/迁移数据库。

    中断恢复是带全局副作用的启动动作：它会把所有 RUNNING Run/Step/ProviderCall
    改写为服务重启状态。因此默认必须关闭，只允许 ``app.main`` 在持有
    ``runtime-recovery.lock`` 后显式传入 ``True``。测试、CLI 和短生命周期脚本只做
    schema 初始化，不能误杀正在运行的主服务任务。
    """
    conn = get_conn()
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    # Provider claims are a project-owned accounting ledger. Migrate their
    # ownership before any integrity repair can delete disposable jobs/assets.
    from app.completion_grant import (
        ensure_video_budget_authority_tables,
        migrate_legacy_video_liabilities,
    )
    ensure_video_budget_authority_tables(conn)
    conn.execute(
        """UPDATE provider_calls
              SET project_id=COALESCE(
                    NULLIF(
                      CASE WHEN json_valid(meta)
                           THEN json_extract(meta,'$.project_id') END,
                      ''
                    ),
                    (
                      SELECT e.project_id FROM episodes e
                       WHERE e.id=CASE WHEN json_valid(provider_calls.meta)
                                       THEN json_extract(provider_calls.meta,'$.episode_id') END
                    ),
                    (
                      SELECT e.project_id
                        FROM shots s JOIN episodes e ON e.id=s.episode_id
                       WHERE s.id=CASE WHEN json_valid(provider_calls.meta)
                                       THEN json_extract(provider_calls.meta,'$.shot_id') END
                    ),
                    (
                      SELECT CASE wr.scope_type
                               WHEN 'project' THEN wr.scope_id
                               WHEN 'episode' THEN (
                                 SELECT e.project_id FROM episodes e WHERE e.id=wr.scope_id
                               )
                               WHEN 'shot' THEN (
                                 SELECT e.project_id
                                   FROM shots s JOIN episodes e ON e.id=s.episode_id
                                  WHERE s.id=wr.scope_id
                               )
                             END
                        FROM workflow_runs wr
                       WHERE wr.id=provider_calls.run_id
                    ),
                    ''
                  )
            WHERE project_id IS NULL"""
    )
    _quarantine_static_delivery_fallbacks(conn)
    _drop_obsolete_storyboard_columns(conn)
    # 视频补齐授权表；历史分镜自动确认授权会在表初始化时清理。
    try:
        from app.completion_grant import ensure_completion_grants_table
        ensure_completion_grants_table(conn)
    except Exception:  # noqa: BLE001
        pass
    # Production Repair：revision / certificate / grant
    try:
        from app.production.revision import ensure_production_revisions_table
        from app.production.certificate import ensure_completion_certificates_table
        from app.production.grant import ensure_production_grants_table
        from app.production.shot_uid import backfill_shot_uids
        ensure_production_revisions_table(conn)
        ensure_completion_certificates_table(conn)
        ensure_production_grants_table(conn)
        backfill_shot_uids(conn)
    except Exception:  # noqa: BLE001
        pass
    # 剧本 warning 终态已移除：有工作副本的继续 Repair，其余旧候选明确标为失败。
    conn.execute(
        """UPDATE episodes
              SET screenplay_status=CASE
                    WHEN COALESCE(working_screenplay_artifact_id, '') != '' THEN 'repairing'
                    ELSE 'failed'
                  END,
                  screenplay_error=COALESCE(
                    screenplay_error,
                    '旧版 warning 候选未取得 QA 通过凭证，请继续修复或重新生成'
                  ),
                  screenplay_snapshot_version=screenplay_snapshot_version+1
            WHERE screenplay_status='warning'"""
    )
    _backfill_multiview_assets(conn)
    _repair_integrity(conn)
    migrate_legacy_video_liabilities(conn)
    # 旧版把“候选已生成但缺新版 QA 证据”误归类为 ProviderError，并在项目页显示为
    # 大模型故障。保留历史 run 原貌供审计，只修正项目当前态与面向用户的恢复指引。
    conn.execute(
        """UPDATE projects
           SET scene_refs_status='warning',
               scene_refs_error=(
                 SELECT '历史任务已更正分类：图片候选已生成，但缺少新版 QA 证据。'
                        || '请进入候选页重新验 QA，或对未验证候选执行人工复核；系统不会继续重复出图。原始诊断：'
                        || substr(r.failure_message, 1, 700)
                   FROM workflow_runs r
                  WHERE r.workflow_type='scene_references'
                    AND r.scope_type='project'
                    AND r.scope_id=projects.id
                  ORDER BY r.updated_at DESC LIMIT 1
               )
         WHERE scene_refs_status='failed'
           AND EXISTS (
             SELECT 1 FROM workflow_runs r
              WHERE r.workflow_type='scene_references'
                AND r.scope_type='project'
                AND r.scope_id=projects.id
                AND r.id=(
                  SELECT latest.id FROM workflow_runs latest
                   WHERE latest.workflow_type='scene_references'
                     AND latest.scope_type='project'
                     AND latest.scope_id=projects.id
                   ORDER BY latest.updated_at DESC LIMIT 1
                )
                AND r.failure_message LIKE '%候选缺少可用的新版%'
           )"""
    )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    # Seedance 2.0 参考图容量已从旧默认 8 升级到 9。INSERT OR IGNORE 不会更新
    # 已有工作区，因此用一次性标记只升级仍停留在旧默认值的实例；后续用户
    # 手工改回 8 时不会被每次启动覆盖。
    capacity_migration_key = "_migration_video_reference_capacity_9_v1"
    capacity_migrated = conn.execute(
        "SELECT 1 FROM settings WHERE key=?", (capacity_migration_key,),
    ).fetchone()
    if not capacity_migrated:
        conn.execute(
            "UPDATE settings SET value='9' WHERE key='video_reference_max_images' AND value='8'"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, 'applied')", (capacity_migration_key,),
        )
    # 所有辅助时序关键帧也升级为默认 3 选 1。仅迁移仍停留在旧默认值 1
    # 的工作区；一次性标记确保用户之后主动改成 1 时不会被启动流程覆盖。
    supporting_candidates_migration_key = "_migration_supporting_keyframe_candidates_3_v1"
    supporting_candidates_migrated = conn.execute(
        "SELECT 1 FROM settings WHERE key=?", (supporting_candidates_migration_key,),
    ).fetchone()
    if not supporting_candidates_migrated:
        conn.execute(
            "UPDATE settings SET value='3' "
            "WHERE key='video_supporting_keyframe_candidates' AND value='1'"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, 'applied')",
            (supporting_candidates_migration_key,),
        )
    # 剧本结构化调用的业务语义修复预算从 1 提升到 2：给模型一次带确定性错误反馈的
    # 二次修复机会，降低单次输出不满足业务校验即整条 run 硬失败的概率。仅迁移仍停留
    # 在旧默认值 1 的工作区；一次性标记确保用户之后主动改回 1 时不会被启动流程覆盖。
    semantic_retry_migration_key = "_migration_screenplay_semantic_retry_limit_2_v1"
    semantic_retry_migrated = conn.execute(
        "SELECT 1 FROM settings WHERE key=?", (semantic_retry_migration_key,),
    ).fetchone()
    if not semantic_retry_migrated:
        conn.execute(
            "UPDATE settings SET value='2' "
            "WHERE key='screenplay_semantic_retry_limit' AND value='1'"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, 'applied')",
            (semantic_retry_migration_key,),
        )
    # These settings are persisted immediately but only become runtime-effective
    # after a process restart.  Capture the authoritative startup value separately
    # so the monitor UI never reports a pending value as already active.
    for key in ("provider_call_retention_days", "error_log_retention_days"):
        conn.execute(
            "INSERT INTO settings(key,value) SELECT ?,value FROM settings WHERE key=? "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"_monitor_effective_{key}", key),
        )
    _repair_invalid_provider_metadata(conn)
    _prune_observability_logs(conn)
    if reconcile_interrupted:
        # 只有持有运行时恢复锁的实例才能宣告旧调用中断。
        # 否则另一端口的启动会把主实例正在执行的长请求错误围栏。
        conn.execute(
            "UPDATE provider_calls SET status='INTERRUPTED', "
            "error=COALESCE(error, '服务重启，调用结果未回写'), "
            "recovery_disposition=COALESCE(recovery_disposition, 'AWAITING_RETRY') "
            "WHERE status='RUNNING'"
        )
        _reconcile_settled_orphan_blueprint_shards(conn)
        conn.execute(
            "UPDATE step_runs SET status='FAILED', finished_at=?, exit_reason='service_restart', "
            "error_code='SERVICE_RESTART', error_message=COALESCE(error_message, '服务重启，步骤已中断') "
            "WHERE status IN ('RUNNING','EVALUATING','REPAIRING')",
            (now(),),
        )
        conn.execute(
            "UPDATE workflow_runs SET status='PAUSED_EXTERNAL', updated_at=?, "
            "failure_code='SERVICE_RESTART', failure_message='服务重启，可从安全检查点恢复', "
            "resume_from_step=COALESCE(resume_from_step, current_step_key) "
            "WHERE status IN ('RUNNING','WAITING_RETRY')",
            (now(),),
        )
        # A running command claim belongs to the stopped process. Durable domain
        # receipts/runs remain authoritative; releasing only the claim lets the
        # same idempotency key reconcile instead of appearing busy for 24 hours.
        try:
            conn.execute("DELETE FROM command_idempotency WHERE status='running'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                """UPDATE video_command_operation_receipts
                      SET lease_expires_at=0
                    WHERE status='running'"""
            )
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute(
                """UPDATE concat_operation_receipts
                      SET lease_expires_at=0,updated_at=?
                    WHERE status='running'""",
                (now(),),
            )
        except sqlite3.OperationalError:
            pass
        try:
            # Only the process holding the runtime recovery lock may fence the
            # previous process's delivery leases.  Preserve workspace/phase;
            # the subsequent owner records them as abandoned evidence and
            # rebuilds in a distinct owner directory.
            from app.delivery import _ensure_delivery_operation_receipts

            _ensure_delivery_operation_receipts(conn)
            conn.execute(
                """UPDATE delivery_operation_receipts
                      SET lease_expires_at=0,interrupted_at=?,updated_at=?
                    WHERE status='running'""",
                (now(), now()),
            )
        except sqlite3.OperationalError:
            pass
    # ``approving`` is a durable approval claim.  Recovery resumes the exact
    # draft snapshot; startup must not reopen it for a concurrent user action.
    conn.commit()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def get_setting(key: str) -> str:
    """读取 settings；表未建或测试用内存库缺表时回退 DEFAULT_SETTINGS，避免拖垮主流程。"""
    try:
        row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")
    except sqlite3.OperationalError:
        return DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def _trim_for_call_log(value: Any, *, max_string: int = 120_000) -> Any:
    if isinstance(value, dict):
        return {k: _trim_for_call_log(v, max_string=max_string) for k, v in value.items()}
    if isinstance(value, list):
        return [_trim_for_call_log(v, max_string=max_string) for v in value]
    if isinstance(value, str):
        if ";base64," in value[:80]:
            prefix = value.split(";base64,", 1)[0]
            return f"{prefix};base64,[omitted {len(value)} chars]"
        if len(value) > max_string:
            return f"{value[:max_string]}\n...[truncated {len(value) - max_string} chars]"
    return value


def _dump_call_json(value: Any) -> str | None:
    if value is None:
        return None
    try:
        compact = _trim_for_call_log(value)
        return json.dumps(compact, ensure_ascii=False)
    except TypeError:
        return json.dumps(str(value), ensure_ascii=False)


def provider_request_hash(value: Any | None) -> str:
    """Hash the complete canonical request; observability truncation is excluded."""
    if isinstance(value, dict):
        value = dict(value)
        value.pop("stream", None)
        value.pop("stream_options", None)
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        raw = json.dumps(str(value), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _dump_meta_json(meta: dict | None, *, max_chars: int = 800) -> str:
    """Serialize metadata as valid JSON even when the original payload is large."""
    value = meta or {}
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(raw) <= max_chars:
        return raw
    summary: dict[str, Any] = {
        "_truncated": True,
        "_original_chars": len(raw),
        "_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }
    priority_keys = (
        "generation_contract",
        "published_output_contract",
        "contract_version",
        "prompt_version",
        "stage",
        "stage_key",
        "call_role",
        "call_role_label",
        "repair_round",
        "episode_id",
        "production_grant_id",
        "requested_max_tokens",
        "effective_max_tokens",
        "operation_id",
        "legacy_unknown_resolution_id",
        "gateway",
        "run_id",
        "step_run_id",
    )
    projected_items: list[tuple[str, Any]] = []
    for key in value:
        item = value[key]
        if isinstance(item, (str, int, float, bool)) or item is None:
            projected: Any = item if not isinstance(item, str) or len(item) <= 160 else item[:157] + "..."
        elif isinstance(item, (list, tuple, set)):
            projected = {"type": type(item).__name__, "count": len(item)}
        elif isinstance(item, dict):
            projected = {"type": "dict", "keys": sorted(str(k) for k in item)[:20]}
        else:
            projected = {"type": type(item).__name__}
        projected_items.append((str(key), projected))
    projected_by_key = dict(projected_items)
    for key in priority_keys:
        if key not in projected_by_key:
            continue
        candidate = {**summary, key: projected_by_key[key]}
        encoded = json.dumps(
            candidate, ensure_ascii=False, sort_keys=True, default=str,
        )
        if len(encoded) <= max_chars:
            summary[key] = projected_by_key[key]
    projected_items.sort(
        key=lambda pair: (
            len(json.dumps(pair[1], ensure_ascii=False, default=str)),
            pair[0],
        )
    )
    for key, projected in projected_items:
        if key in summary:
            continue
        candidate = {**summary, str(key): projected}
        encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)
        if len(encoded) > max_chars:
            continue
        summary[str(key)] = projected
    return json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)


def provider_operation_id(kind: str, model: str, request_json: Any | None) -> str:
    """Stable business-operation fingerprint shared by retries and process restarts."""
    payload = provider_request_hash(request_json)
    digest = hashlib.sha256(
        f"{kind}\0{model}\0{payload}".encode("utf-8", "replace")
    ).hexdigest()
    return f"op_{digest[:32]}"


def _provider_recovery_ledger_available(conn: sqlite3.Connection) -> bool:
    """Allow observability writes during rolling upgrades and isolated legacy tests."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_calls)")}
    return {
        "operation_id", "attempt_no", "supersedes_call_id",
        "superseded_by_call_id", "recovery_disposition",
        "request_hash",
    }.issubset(columns)


def log_provider_call(kind: str, model: str, status: str, http_status: int | None,
                      latency_ms: int, error: str | None = None, meta: dict | None = None,
                      request_json: Any | None = None, response_json: Any | None = None,
                      operation_id: str | None = None) -> None:
    from app.observability.tracing import current_trace

    trace = current_trace()
    conn = get_conn()
    try:
        _log_provider_call_inner(
            conn, trace, kind, model, status, http_status, latency_ms,
            error=error, meta=meta, request_json=request_json,
            response_json=response_json, operation_id=operation_id,
        )
    except sqlite3.OperationalError:
        # 观测写入为 best-effort：滚动升级或隔离单测库缺表时不应阻断业务。
        pass


def _provider_call_project_id(
    conn: sqlite3.Connection,
    trace: Any,
    meta: dict | None,
) -> str | None:
    context = meta or {}
    direct = str(context.get("project_id") or "").strip()
    if direct:
        return direct
    episode_id = str(context.get("episode_id") or "").strip()
    if episode_id:
        row = conn.execute(
            "SELECT project_id FROM episodes WHERE id=?",
            (episode_id,),
        ).fetchone()
        if row:
            return str(row["project_id"])
    shot_id = str(context.get("shot_id") or "").strip()
    if shot_id:
        row = conn.execute(
            """SELECT e.project_id
                 FROM shots s JOIN episodes e ON e.id=s.episode_id
                WHERE s.id=?""",
            (shot_id,),
        ).fetchone()
        if row:
            return str(row["project_id"])
    run_id = str(getattr(trace, "run_id", "") or "").strip()
    if not run_id:
        return None
    run = conn.execute(
        "SELECT scope_type,scope_id FROM workflow_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if not run:
        return None
    if run["scope_type"] == "project":
        return str(run["scope_id"])
    if run["scope_type"] == "episode":
        row = conn.execute(
            "SELECT project_id FROM episodes WHERE id=?",
            (run["scope_id"],),
        ).fetchone()
        return str(row["project_id"]) if row else None
    if run["scope_type"] == "shot":
        row = conn.execute(
            """SELECT e.project_id
                 FROM shots s JOIN episodes e ON e.id=s.episode_id
                WHERE s.id=?""",
            (run["scope_id"],),
        ).fetchone()
        return str(row["project_id"]) if row else None
    return None


def _log_provider_call_inner(
    conn: sqlite3.Connection,
    trace: Any,
    kind: str,
    model: str,
    status: str,
    http_status: int | None,
    latency_ms: int,
    *,
    error: str | None = None,
    meta: dict | None = None,
    request_json: Any | None = None,
    response_json: Any | None = None,
    operation_id: str | None = None,
) -> None:
    project_id = _provider_call_project_id(conn, trace, meta)
    if not _provider_recovery_ledger_available(conn):
        conn.execute(
            """INSERT INTO provider_calls(
                ts, kind, model, status, http_status, latency_ms, error, request_json, response_json,
                meta, project_id, run_id, step_run_id, trace_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), kind, model, status, http_status, latency_ms,
             (error or "")[:500] or None, _dump_call_json(request_json),
             _dump_call_json(response_json), _dump_meta_json(meta), project_id,
             trace.run_id, trace.step_run_id, trace.trace_id),
        )
        conn.commit()
        return
    op_id = operation_id or str((meta or {}).get("operation_id") or "") \
        or provider_operation_id(kind, model, request_json)
    previous = conn.execute(
        "SELECT id, attempt_no, status FROM provider_calls WHERE operation_id=? ORDER BY id DESC LIMIT 1",
        (op_id,),
    ).fetchone()
    retry_previous = (
        previous
        if previous is not None and previous["status"] == "INTERRUPTED"
        else None
    )
    attempt_no = (
        int(retry_previous["attempt_no"] or 0) + 1
        if retry_previous else 1
    )
    cur = conn.execute(
        """INSERT INTO provider_calls(
            ts, kind, model, status, http_status, latency_ms, error, request_json, request_hash, response_json, meta,
            project_id, run_id, step_run_id, trace_id, operation_id, attempt_no, supersedes_call_id,
            contract_version,production_grant_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now(), kind, model, status, http_status, latency_ms,
         (error or "")[:500] or None, _dump_call_json(request_json), provider_request_hash(request_json), _dump_call_json(response_json),
         _dump_meta_json(meta), project_id, trace.run_id, trace.step_run_id, trace.trace_id,
         op_id, attempt_no, retry_previous["id"] if retry_previous else None,
         str((meta or {}).get("contract_version") or "") or None,
         str((meta or {}).get("production_grant_id") or "") or None),
    )
    if retry_previous and status in {"OK", "SUCCEEDED", "SUCCESS"}:
        conn.execute(
            "UPDATE provider_calls SET superseded_by_call_id=?, recovery_disposition='RETRIED_SUCCESSFULLY' "
            "WHERE id=? AND status='INTERRUPTED'",
            (int(cur.lastrowid), retry_previous["id"]),
        )
    conn.commit()


def start_provider_call(kind: str, model: str, *, meta: dict | None = None,
                        request_json: Any | None = None) -> int:
    """请求发出前先写入账本，让长请求立即在监制房显示。"""
    from app.observability.tracing import current_trace

    trace = current_trace()
    conn = get_conn()
    try:
        return _start_provider_call_inner(
            conn, trace, kind, model, meta=meta, request_json=request_json,
        )
    except sqlite3.OperationalError:
        return 0


def _start_provider_call_inner(
    conn: sqlite3.Connection,
    trace: Any,
    kind: str,
    model: str,
    *,
    meta: dict | None = None,
    request_json: Any | None = None,
) -> int:
    project_id = _provider_call_project_id(conn, trace, meta)
    if not _provider_recovery_ledger_available(conn):
        cur = conn.execute(
            """INSERT INTO provider_calls(
                ts, kind, model, status, http_status, latency_ms, error, request_json, response_json,
                meta, project_id, run_id, step_run_id, trace_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), kind, model, "RUNNING", None, 0, None, _dump_call_json(request_json), None,
             _dump_meta_json(meta), project_id, trace.run_id, trace.step_run_id,
             trace.trace_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    op_id = str((meta or {}).get("operation_id") or "") \
        or provider_operation_id(kind, model, request_json)
    previous = conn.execute(
        "SELECT id, attempt_no, status FROM provider_calls WHERE operation_id=? ORDER BY id DESC LIMIT 1",
        (op_id,),
    ).fetchone()
    explicit_previous_id = int((meta or {}).get("supersedes_provider_call_id") or 0)
    legacy_unknown_resolution_id = 0
    if previous is None and explicit_previous_id:
        legacy_previous = conn.execute(
            """SELECT id,attempt_no,status,request_hash
                 FROM provider_calls WHERE id=? AND status='INTERRUPTED'""",
            (explicit_previous_id,),
        ).fetchone()
        # A version migration may replace an old run-scoped operation id with
        # the stable semantic id.  Link it only when the exact outbound request
        # is byte-identical; stage/round proximity is never enough authority.
        if legacy_previous is not None and (
            str(legacy_previous["request_hash"] or "")
            == provider_request_hash(request_json)
        ):
            previous = legacy_previous
        elif legacy_previous is not None and not str(
            legacy_previous["request_hash"] or ""
        ):
            # Pre-migration observability kept only a truncated request, so it
            # can never prove byte identity and must not become a supersedes
            # edge.  A newly authorized operation may consume this unknown
            # liability once, after its own result is durable.
            legacy_unknown_resolution_id = int(legacy_previous["id"])
    # Default recovery-chain linking only trusts a prior INTERRUPTED row: the
    # request's fate there was unknown, so an identical follow-up is provably
    # the same in-flight operation resuming. A prior FAILED row is normally a
    # *known*, definitive outcome and is deliberately left unlinked (e.g. the
    # unrelated 5xx retries already performed inside hiagent._post_json's own
    # loop). One narrow, explicit exception: callers that know they are
    # replaying an unmodified request after a transport-level rejection (see
    # hiagent.chat's response_format_required 400 retry) may opt in via
    # meta["provider_call_retry_of_failed"] so that specific, self-declared
    # retry chain still shows up as attempt_no/supersedes_call_id linkage in
    # /api/system/calls instead of looking like unrelated duplicate calls.
    # This never changes op_id (still pure kind/model/payload hash) and never
    # affects any other FAILED→FAILED transition that does not set the flag.
    link_failed_retry = bool((meta or {}).get("provider_call_retry_of_failed"))
    retry_previous = (
        previous
        if previous is not None and (
            previous["status"] == "INTERRUPTED"
            or (link_failed_retry and previous["status"] == "FAILED")
        )
        else None
    )
    attempt_no = (
        int(retry_previous["attempt_no"] or 0) + 1
        if retry_previous else 1
    )
    retry_disposition = None
    if retry_previous:
        retry_disposition = (
            "RETRYING_INTERRUPTED"
            if retry_previous["status"] == "INTERRUPTED"
            else "RETRYING_REJECTED_REQUEST"
        )
    cur = conn.execute(
        """INSERT INTO provider_calls(
            ts, kind, model, status, http_status, latency_ms, error, request_json, request_hash, response_json, meta,
            project_id, run_id, step_run_id, trace_id, operation_id, attempt_no, supersedes_call_id,
            recovery_disposition,contract_version,production_grant_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now(), kind, model, "RUNNING", None, 0, None, _dump_call_json(request_json),
         provider_request_hash(request_json), None, _dump_meta_json({
             **(meta or {}),
             "legacy_unknown_resolution_id": (
                 legacy_unknown_resolution_id or None
             ),
         }), project_id, trace.run_id, trace.step_run_id, trace.trace_id,
         op_id, attempt_no, retry_previous["id"] if retry_previous else None,
         retry_disposition,
         str((meta or {}).get("contract_version") or "") or None,
         str((meta or {}).get("production_grant_id") or "") or None),
    )
    if retry_previous:
        conn.execute(
            "UPDATE provider_calls SET superseded_by_call_id=?, recovery_disposition='RETRY_STARTED' WHERE id=?",
            (int(cur.lastrowid), retry_previous["id"]),
        )
    conn.commit()
    return int(cur.lastrowid)


def latest_provider_request_json(
    kind: str,
    model: str,
    operation_id: str,
) -> Any | None:
    """Return the newest durable request checkpoint for one business operation."""
    if not operation_id:
        return None
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT request_json FROM provider_calls
               WHERE kind=? AND model=? AND operation_id=?
                 AND request_json IS NOT NULL
               ORDER BY id DESC""",
            (kind, model, operation_id),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        try:
            return json.loads(row["request_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def update_provider_call_request(
    call_id: int,
    request_json: Any,
    *,
    preserve_exact: bool = False,
) -> None:
    """Persist the exact outbound request before the non-idempotent write."""
    if not call_id:
        return
    if preserve_exact:
        try:
            encoded = json.dumps(
                request_json,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except TypeError:
            encoded = json.dumps(str(request_json), ensure_ascii=False)
    else:
        encoded = _dump_call_json(request_json)
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE provider_calls SET request_json=?,request_hash=?
               WHERE id=? AND status='RUNNING'""",
            (encoded, provider_request_hash(request_json), call_id),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()


def update_provider_call_progress(
    call_id: int,
    *,
    received_chars: int,
    chunk_at: float | None = None,
) -> None:
    """Persist bounded stream heartbeat data without affecting business flow."""
    if not call_id:
        return
    stamp = float(chunk_at or now())
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE provider_calls
                  SET first_chunk_at=COALESCE(first_chunk_at,?),
                      last_chunk_at=?,
                      received_chars=MAX(received_chars,?)
                WHERE id=? AND status='RUNNING'""",
            (stamp, stamp, max(0, int(received_chars)), call_id),
        )
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()


def finish_provider_call(call_id: int, status: str, http_status: int | None,
                         latency_ms: int, *, error: str | None = None,
                         response_json: Any | None = None) -> None:
    """原地更新已开始的调用，避免“调用中 + 成功”被误认为两次模型花费。"""
    if not call_id:
        return
    conn = get_conn()
    try:
        _finish_provider_call_inner(
            conn, call_id, status, http_status, latency_ms,
            error=error, response_json=response_json,
        )
    except sqlite3.OperationalError:
        pass


def _finish_provider_call_inner(
    conn: sqlite3.Connection,
    call_id: int,
    status: str,
    http_status: int | None,
    latency_ms: int,
    *,
    error: str | None = None,
    response_json: Any | None = None,
) -> None:
    updated = conn.execute(
        """UPDATE provider_calls
           SET status=?, http_status=?, latency_ms=?, error=?, response_json=?,
               recovery_disposition=CASE
                 WHEN ?='INTERRUPTED' THEN 'REQUIRES_EXPLICIT_RETRY'
                 ELSE recovery_disposition
               END
           WHERE id=? AND status='RUNNING'""",
        (status, http_status, latency_ms, (error or "")[:500] or None,
         _dump_call_json(response_json), status, call_id),
    )
    # A previous process may finish a socket after the replacement process has
    # fenced it as INTERRUPTED.  Its late result must not rewrite restart audit
    # state or race the recovery attempt.
    if updated.rowcount != 1:
        conn.commit()
        return
    if not _provider_recovery_ledger_available(conn):
        conn.commit()
        return
    if status in {"OK", "SUCCEEDED", "SUCCESS"}:
        row = conn.execute(
            "SELECT supersedes_call_id,meta FROM provider_calls WHERE id=?", (call_id,)
        ).fetchone()
        if row and row["supersedes_call_id"]:
            # supersedes_call_id is only ever populated by the gated logic in
            # _start_provider_call_inner (previous row was INTERRUPTED, or the
            # narrow opted-in provider_call_retry_of_failed case where it was
            # FAILED) — by the time we reach here the predecessor's identity
            # is already proven, so accepting either terminal status here just
            # lets the FAILED-retry chain close out symmetrically instead of
            # only linking forward (supersedes_call_id) without ever setting
            # the backward pointer (superseded_by_call_id) on success.
            conn.execute(
                "UPDATE provider_calls SET superseded_by_call_id=?, "
                "recovery_disposition='RETRIED_SUCCESSFULLY' "
                "WHERE id=? AND status IN ('INTERRUPTED','FAILED')",
                (call_id, row["supersedes_call_id"]),
            )
        if row:
            try:
                call_meta = json.loads(row["meta"] or "{}")
                legacy_unknown_id = int(
                    call_meta.get("legacy_unknown_resolution_id") or 0
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                legacy_unknown_id = 0
            if legacy_unknown_id:
                conn.execute(
                    """UPDATE provider_calls
                          SET superseded_by_call_id=?,
                              recovery_disposition='LEGACY_UNKNOWN_RESOLVED_BY_EXPLICIT_RETRY'
                        WHERE id=? AND status='INTERRUPTED'
                          AND request_hash IS NULL
                          AND superseded_by_call_id IS NULL""",
                    (call_id, legacy_unknown_id),
                )
    conn.commit()


def insert_error_log(error_id: str, *, category: str, category_label: str, code: str,
                     is_technical: bool, http_status: int | None, action: str | None,
                     context: Any | None, message: str | None, traceback_text: str | None,
                     exc_type: str | None, meta: dict | None = None) -> None:
    """落库一条报错日志。原文/堆栈/上下文全留后端，前端只拿 error_id+code+category。"""
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO error_logs(
            id, ts, category, category_label, code, is_technical, http_status,
            action, context_json, message, traceback, exc_type, meta_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (error_id, now(), category, category_label, code, 1 if is_technical else 0, http_status,
         action, _dump_call_json(context), (message or "")[:20_000] or None,
         (traceback_text or "")[:40_000] or None, exc_type,
         json.dumps(meta or {}, ensure_ascii=False)[:1000]),
    )
    conn.commit()
