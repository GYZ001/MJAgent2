"""SQLite 存储。9 张表（PRD §5.2），媒体文件只存路径不存内容。"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
import uuid
from typing import Any

from app.config import DATA_DIR, DB_PATH, DEFAULT_SETTINGS

_local = threading.local()

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
    screenplay_json TEXT,
    screenplay_status TEXT DEFAULT 'pending',
    screenplay_error TEXT,
    screenplay_started_at REAL,
    screenplay_updated_at REAL,
    screenplay_artifact_id TEXT,
    storyboard_artifact_id TEXT,
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
    episode_id TEXT NOT NULL,
    script_id TEXT,
    shot_no INTEGER NOT NULL,
    duration_s INTEGER NOT NULL,
    shot_size TEXT,
    camera_move TEXT,
    scene_setting TEXT,
    characters TEXT,
    action_desc TEXT,
    source_excerpt TEXT DEFAULT '',
    narration TEXT,
    dialogues TEXT,
    transition TEXT,
    continuity_from_prev INTEGER DEFAULT 0,
    adopted_version_id TEXT,
    approved_scene_id TEXT,
    approved_head_scene_id TEXT,
    approved_tail_scene_id TEXT,
    scene_status TEXT DEFAULT 'none',
    storyboard_artifact_id TEXT,
    UNIQUE(episode_id, shot_no),
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
    error TEXT,
    video_path TEXT,
    last_frame_url TEXT,
    qa_json TEXT,
    cost_cny REAL DEFAULT 0,
    latency_s REAL DEFAULT 0,
    technical_validation_json TEXT,
    adoption_reason TEXT,
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
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    after_shot_id TEXT,
    after_version_id TEXT,
    scene_kinds TEXT,
    run_id TEXT,
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
    provider_submitted_at REAL,
    abandoned INTEGER NOT NULL DEFAULT 0,
    attempt_started_at REAL,
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
    response_json TEXT,
    meta TEXT,
    run_id TEXT,
    step_run_id TEXT,
    trace_id TEXT,
    operation_id TEXT,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    supersedes_call_id INTEGER,
    superseded_by_call_id INTEGER,
    recovery_disposition TEXT,
    FOREIGN KEY(supersedes_call_id) REFERENCES provider_calls(id),
    FOREIGN KEY(superseded_by_call_id) REFERENCES provider_calls(id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_step_runs_run ON step_runs(run_id, started_at, iteration_no);
CREATE INDEX IF NOT EXISTS idx_artifacts_scope ON artifacts(scope_type, scope_id, type, version);
CREATE INDEX IF NOT EXISTS idx_evaluations_artifact ON evaluations(artifact_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_budget_scope ON budget_reservations(scope_type, scope_id, status);
CREATE INDEX IF NOT EXISTS idx_gate_pending ON gate_decisions(gate_key, decision, created_at);
CREATE INDEX IF NOT EXISTS idx_delivery_episode ON delivery_packages(episode_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_episode ON customer_feedback(episode_id, created_at);
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
    created_at REAL NOT NULL,
    FOREIGN KEY(reference_set_id) REFERENCES reference_sets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reference_assets_set ON reference_assets(reference_set_id, sort_order);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


# 增量迁移：已有库上加列（首次建表时 SCHEMA 已含则忽略报错）
MIGRATIONS = (
    "ALTER TABLE jobs ADD COLUMN after_shot_id TEXT",
    "ALTER TABLE jobs ADD COLUMN after_version_id TEXT",
    "ALTER TABLE jobs ADD COLUMN scene_kinds TEXT",
    "ALTER TABLE shot_versions ADD COLUMN image_inputs TEXT",
    "ALTER TABLE projects ADD COLUMN refs_status TEXT DEFAULT 'idle'",
    "ALTER TABLE projects ADD COLUMN refs_error TEXT",
    "ALTER TABLE projects ADD COLUMN refs_target TEXT",
    "ALTER TABLE shots ADD COLUMN source_excerpt TEXT DEFAULT ''",
    "ALTER TABLE shots ADD COLUMN approved_scene_id TEXT",
    "ALTER TABLE shots ADD COLUMN approved_head_scene_id TEXT",
    "ALTER TABLE shots ADD COLUMN approved_tail_scene_id TEXT",
    "ALTER TABLE shots ADD COLUMN scene_status TEXT DEFAULT 'none'",  # none/generating/review/approved
    "ALTER TABLE shot_scenes ADD COLUMN kind TEXT DEFAULT 'tail'",
    "ALTER TABLE shots ADD COLUMN first_frame_desc TEXT DEFAULT ''",
    "ALTER TABLE shots ADD COLUMN last_frame_desc TEXT DEFAULT ''",
    "ALTER TABLE shots ADD COLUMN script_id TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_json TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_status TEXT DEFAULT 'pending'",
    "ALTER TABLE episodes ADD COLUMN screenplay_error TEXT",
    "ALTER TABLE episodes ADD COLUMN screenplay_started_at REAL",
    "ALTER TABLE episodes ADD COLUMN screenplay_updated_at REAL",
    "ALTER TABLE shots ADD COLUMN mode_plan TEXT",
    "ALTER TABLE projects ADD COLUMN bible_feedback TEXT",  # 持久化重谱打回要求，供进程重启后恢复人物谱任务
    "ALTER TABLE projects ADD COLUMN portraits_status TEXT DEFAULT 'idle'",  # 按集刷新定妆照任务状态
    "ALTER TABLE projects ADD COLUMN portraits_error TEXT",
    "ALTER TABLE provider_calls ADD COLUMN request_json TEXT",
    "ALTER TABLE provider_calls ADD COLUMN response_json TEXT",
    "ALTER TABLE episodes ADD COLUMN storyboard_outline_json TEXT",  # 分镜大纲（先规划后逐镜填充），供前端展示进度 k/N
    "ALTER TABLE episodes ADD COLUMN screenplay_artifact_id TEXT",
    "ALTER TABLE projects ADD COLUMN scene_refs_status TEXT DEFAULT 'idle'",  # 场景图素材库生成任务状态
    "ALTER TABLE projects ADD COLUMN scene_refs_error TEXT",
    "ALTER TABLE projects ADD COLUMN scene_refs_target TEXT",
    "ALTER TABLE shots ADD COLUMN scene_name TEXT",  # 归一化命中的库内规范场景名（渲染期取场景库图复用）
    "ALTER TABLE jobs ADD COLUMN run_id TEXT",
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
    "ALTER TABLE episodes ADD COLUMN storyboard_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN delivery_artifact_id TEXT",
    "ALTER TABLE episodes ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'not_ready'",
    "ALTER TABLE shots ADD COLUMN storyboard_artifact_id TEXT",
    "ALTER TABLE shot_versions ADD COLUMN technical_validation_json TEXT",
    "ALTER TABLE shot_versions ADD COLUMN adoption_reason TEXT",
    "ALTER TABLE shot_scenes ADD COLUMN adoption_reason TEXT",
    "ALTER TABLE jobs ADD COLUMN max_retries INTEGER NOT NULL DEFAULT 3",
    "ALTER TABLE jobs ADD COLUMN reserved_cost_cny REAL NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN cancellation_requested INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN provider_non_cancellable INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN provider_operation_id TEXT",
    "ALTER TABLE jobs ADD COLUMN provider_create_state TEXT NOT NULL DEFAULT 'not_started'",
    "ALTER TABLE jobs ADD COLUMN provider_submitted_at REAL",
    "ALTER TABLE jobs ADD COLUMN abandoned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN attempt_started_at REAL",
    "ALTER TABLE character_portraits ADD COLUMN artifact_id TEXT",
    "ALTER TABLE scene_references ADD COLUMN artifact_id TEXT",
    "ALTER TABLE benchmark_runs ADD COLUMN is_real_project INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE benchmark_runs ADD COLUMN attested_by TEXT",
    "ALTER TABLE benchmark_runs ADD COLUMN attestation_note TEXT",
    "ALTER TABLE provider_calls ADD COLUMN operation_id TEXT",
    "ALTER TABLE provider_calls ADD COLUMN attempt_no INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE provider_calls ADD COLUMN supersedes_call_id INTEGER",
    "ALTER TABLE provider_calls ADD COLUMN superseded_by_call_id INTEGER",
    "ALTER TABLE provider_calls ADD COLUMN recovery_disposition TEXT",
    "ALTER TABLE workflow_runs ADD COLUMN recovered_by_run_id TEXT",
    "ALTER TABLE workflow_runs ADD COLUMN recovered_at REAL",
    "ALTER TABLE workflow_runs ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0",
)


INTEGRITY_SCHEMA = """
-- This index depends on columns added by MIGRATIONS for legacy databases, so it
-- must be created only after the additive migration pass has completed.
CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, next_retry_at, lease_expires_at, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON jobs(run_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_provider_calls_operation ON provider_calls(operation_id, attempt_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_chapters_project_idx ON chapters(project_id, idx);
CREATE UNIQUE INDEX IF NOT EXISTS uq_episodes_project_no ON episodes(project_id, episode_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_shots_episode_no ON shots(episode_id, shot_no);
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
    conn.executescript(INTEGRITY_SCHEMA)
    after = _integrity_findings(conn)
    report = {
        "schema_version": "1.0.0",
        "created_at": time.time(),
        "backup_path": backup_path,
        "repair_count": repair_count,
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


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise
    _repair_integrity(conn)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    _prune_observability_logs(conn)
    # 进程重启时，旧进程不可能再回写这些请求；不能让监控页永久显示“调用中”。
    conn.execute(
        "UPDATE provider_calls SET status='INTERRUPTED', "
        "error=COALESCE(error, '服务重启，调用结果未回写'), "
        "recovery_disposition=COALESCE(recovery_disposition, 'AWAITING_RETRY') "
        "WHERE status='RUNNING'"
    )
    # A process restart cannot leave a persisted run pretending to be active.
    # Phase 1 has no durable leases yet, so interruption is explicit and resumable.
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
    # 审批进程可能在不可变 T5 快照生成前后退出。已有更新批准包则旧草稿只保留审计状态；
    # 否则恢复等待人工，允许安全重试。
    conn.execute(
        """UPDATE delivery_packages AS draft SET status='superseded'
           WHERE draft.status='approving' AND EXISTS (
             SELECT 1 FROM delivery_packages newer
             WHERE newer.episode_id=draft.episode_id AND newer.status='approved'
               AND newer.created_at>draft.created_at
           )"""
    )
    conn.execute("UPDATE delivery_packages SET status='waiting_human' WHERE status='approving'")
    conn.commit()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def get_setting(key: str) -> str:
    row = get_conn().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


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


def provider_operation_id(kind: str, model: str, request_json: Any | None) -> str:
    """Stable business-operation fingerprint shared by retries and process restarts."""
    payload = _dump_call_json(request_json) or "null"
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
    }.issubset(columns)


def log_provider_call(kind: str, model: str, status: str, http_status: int | None,
                      latency_ms: int, error: str | None = None, meta: dict | None = None,
                      request_json: Any | None = None, response_json: Any | None = None,
                      operation_id: str | None = None) -> None:
    from app.observability.tracing import current_trace

    trace = current_trace()
    conn = get_conn()
    if not _provider_recovery_ledger_available(conn):
        conn.execute(
            """INSERT INTO provider_calls(
                ts, kind, model, status, http_status, latency_ms, error, request_json, response_json,
                meta, run_id, step_run_id, trace_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), kind, model, status, http_status, latency_ms,
             (error or "")[:500] or None, _dump_call_json(request_json),
             _dump_call_json(response_json), json.dumps(meta or {}, ensure_ascii=False)[:800],
             trace.run_id, trace.step_run_id, trace.trace_id),
        )
        conn.commit()
        return
    op_id = operation_id or str((meta or {}).get("operation_id") or "") \
        or provider_operation_id(kind, model, request_json)
    previous = conn.execute(
        "SELECT id, attempt_no FROM provider_calls WHERE operation_id=? ORDER BY id DESC LIMIT 1",
        (op_id,),
    ).fetchone()
    attempt_no = int(previous["attempt_no"] or 0) + 1 if previous else 1
    cur = conn.execute(
        """INSERT INTO provider_calls(
            ts, kind, model, status, http_status, latency_ms, error, request_json, response_json, meta,
            run_id, step_run_id, trace_id, operation_id, attempt_no, supersedes_call_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now(), kind, model, status, http_status, latency_ms,
         (error or "")[:500] or None, _dump_call_json(request_json), _dump_call_json(response_json),
         json.dumps(meta or {}, ensure_ascii=False)[:800], trace.run_id, trace.step_run_id, trace.trace_id,
         op_id, attempt_no, previous["id"] if previous else None),
    )
    if previous and status in {"OK", "SUCCEEDED", "SUCCESS"}:
        conn.execute(
            "UPDATE provider_calls SET superseded_by_call_id=?, recovery_disposition='RETRIED_SUCCESSFULLY' "
            "WHERE id=? AND status='INTERRUPTED'",
            (int(cur.lastrowid), previous["id"]),
        )
    conn.commit()


def start_provider_call(kind: str, model: str, *, meta: dict | None = None,
                        request_json: Any | None = None) -> int:
    """请求发出前先写入账本，让长请求立即在监制房显示。"""
    from app.observability.tracing import current_trace

    trace = current_trace()
    conn = get_conn()
    if not _provider_recovery_ledger_available(conn):
        cur = conn.execute(
            """INSERT INTO provider_calls(
                ts, kind, model, status, http_status, latency_ms, error, request_json, response_json,
                meta, run_id, step_run_id, trace_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), kind, model, "RUNNING", None, 0, None, _dump_call_json(request_json), None,
             json.dumps(meta or {}, ensure_ascii=False)[:800], trace.run_id, trace.step_run_id,
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
    attempt_no = int(previous["attempt_no"] or 0) + 1 if previous else 1
    cur = conn.execute(
        """INSERT INTO provider_calls(
            ts, kind, model, status, http_status, latency_ms, error, request_json, response_json, meta,
            run_id, step_run_id, trace_id, operation_id, attempt_no, supersedes_call_id,
            recovery_disposition
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now(), kind, model, "RUNNING", None, 0, None, _dump_call_json(request_json), None,
         json.dumps(meta or {}, ensure_ascii=False)[:800], trace.run_id, trace.step_run_id, trace.trace_id,
         op_id, attempt_no, previous["id"] if previous else None,
         "RETRYING_INTERRUPTED" if previous and previous["status"] == "INTERRUPTED" else None),
    )
    if previous and previous["status"] == "INTERRUPTED":
        conn.execute(
            "UPDATE provider_calls SET superseded_by_call_id=?, recovery_disposition='RETRY_STARTED' WHERE id=?",
            (int(cur.lastrowid), previous["id"]),
        )
    conn.commit()
    return int(cur.lastrowid)


def finish_provider_call(call_id: int, status: str, http_status: int | None,
                         latency_ms: int, *, error: str | None = None,
                         response_json: Any | None = None) -> None:
    """原地更新已开始的调用，避免“调用中 + 成功”被误认为两次模型花费。"""
    conn = get_conn()
    updated = conn.execute(
        """UPDATE provider_calls
           SET status=?, http_status=?, latency_ms=?, error=?, response_json=?
           WHERE id=? AND status='RUNNING'""",
        (status, http_status, latency_ms, (error or "")[:500] or None,
         _dump_call_json(response_json), call_id),
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
            "SELECT supersedes_call_id FROM provider_calls WHERE id=?", (call_id,)
        ).fetchone()
        if row and row["supersedes_call_id"]:
            conn.execute(
                "UPDATE provider_calls SET superseded_by_call_id=?, "
                "recovery_disposition='RETRIED_SUCCESSFULLY' "
                "WHERE id=? AND status='INTERRUPTED'",
                (call_id, row["supersedes_call_id"]),
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
