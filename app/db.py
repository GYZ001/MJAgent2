"""SQLite 存储。9 张表（PRD §5.2），媒体文件只存路径不存内容。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from app.config import DB_PATH, DEFAULT_SETTINGS

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
    cleaned_lines INTEGER DEFAULT 0
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
    status TEXT DEFAULT 'planned',
    script_error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
    id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
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
    scene_status TEXT DEFAULT 'none'
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
    created_at REAL NOT NULL
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
    created_at REAL NOT NULL
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
    scene_kinds TEXT
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
    meta TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pronunciation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    term TEXT NOT NULL,            -- 标准词（画面/字幕里显示的写法）
    tts_alias TEXT,               -- 喂给 TTS 的安全写法（保证读音），为空则用 term
    asr_aliases TEXT,             -- JSON 数组：ASR 可能识别成的别字，归一化时映射回 term
    level TEXT DEFAULT 'A',       -- S/A/B：声音重要等级（S 人名/境界/结果，必须读对）
    created_at REAL NOT NULL,
    UNIQUE(project_id, term)
);
CREATE TABLE IF NOT EXISTS shot_audio (
    shot_id TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    source_text TEXT,             -- 本镜口播标准文本（台词+旁白，标准词）
    tts_text TEXT,                -- 实际喂 TTS 的安全文本（已套用正音别名）
    audio_path TEXT,              -- 落盘配音文件
    asr_text TEXT,                -- 成片/预检 ASR 识别（归一化后）
    cer REAL DEFAULT -1,
    level TEXT DEFAULT 'A',
    status TEXT DEFAULT 'pending',-- pending/ok/failed/empty(无台词)
    regen_count INTEGER DEFAULT 0,
    error TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapters_project ON chapters(project_id, idx);
CREATE INDEX IF NOT EXISTS idx_episodes_project ON episodes(project_id, episode_no);
CREATE INDEX IF NOT EXISTS idx_shots_episode ON shots(episode_id, shot_no);
CREATE INDEX IF NOT EXISTS idx_versions_shot ON shot_versions(shot_id, version_no);
CREATE INDEX IF NOT EXISTS idx_scenes_shot ON shot_scenes(shot_id, version_no);
CREATE INDEX IF NOT EXISTS idx_versions_idem ON shot_versions(idem_key);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_pron_project ON pronunciation(project_id);
CREATE INDEX IF NOT EXISTS idx_shot_audio_episode ON shot_audio(episode_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30)
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
)


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # 列已存在
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
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


def log_provider_call(kind: str, model: str, status: str, http_status: int | None,
                      latency_ms: int, error: str | None = None, meta: dict | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO provider_calls(ts, kind, model, status, http_status, latency_ms, error, meta) VALUES(?,?,?,?,?,?,?,?)",
        (now(), kind, model, status, http_status, latency_ms,
         (error or "")[:500] or None, json.dumps(meta or {}, ensure_ascii=False)[:800]),
    )
    conn.commit()
