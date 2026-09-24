"""角色固定声音的表结构与读写原语（懒建表，照 ``app/props/store.py`` 的写法）。

L2（``app/LAYERS.toml`` 声明 ``"app.voice.store" = 2``，与 ``app.db`` 同层）：只
依赖 ``app.db``/``app.db_schema``/``app.config``，不引入模型调用、ffmpeg、语音
识别等 L3/L4 依赖——``app.voice`` 包根声明 L4，是给真正发起外部调用的
``service``/``description``/``clipping`` 用的；本模块只做持久化，理应更底层，
与 ``app.audit``「只 import app.db/app.config 等 <=2 依赖的纯持久化模块判 L2」
同一理由（``app.props`` 整包判 L4 是另一回事——它的子模块之间彼此依赖更深，
不能类比）。

不进 ``app/db.py``：该模块扇入 254，CLAUDE.md「扇入 >100 的模块不得再加
职责」；``ensure_schema()``/``ensure_tables_on_connection()`` 的双入口分工、
DDL 逐条 ``conn.execute()``（禁 ``executescript``，它执行前的隐式 COMMIT 会把
调用方尚未提交的事务一起偷偷提交掉）都照抄 ``app/props/store.py`` 的先例，
理由同该模块 docstring，不重复整套论证。

业务数据读写（插入/更新一条声音版本、查询候选与当前）不是审计那种"失败
静默降级"的诊断写入——它们是状态转移的一部分，必须显式用调用方传入的
``conn``，本模块任何函数都不自行 ``commit()``（CLAUDE.md「不得在调用方的
连接上隐式提交」），事务边界由调用方（``app.voice.service``、
``app.portraits.card_rebind``/``card_merge``）决定。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from app import config, db, db_schema

_CREATE_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS character_voices (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        character_name TEXT NOT NULL,
        anchor_key TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'design',
        model_id TEXT NOT NULL DEFAULT '',
        provider_voice_id TEXT NOT NULL DEFAULT '',
        voice_prompt TEXT NOT NULL DEFAULT '',
        preview_text TEXT NOT NULL DEFAULT '',
        audio_path TEXT NOT NULL DEFAULT '',
        clip_path TEXT NOT NULL DEFAULT '',
        clip_duration_s REAL,
        clip_sha256 TEXT NOT NULL DEFAULT '',
        check_status TEXT NOT NULL DEFAULT 'unchecked',
        check_reason TEXT NOT NULL DEFAULT '',
        asr_text TEXT NOT NULL DEFAULT '',
        asr_match REAL,
        error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        created_by TEXT NOT NULL DEFAULT '',
        adopted_at REAL,
        adopted_by TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_character_voices_lookup "
    "ON character_voices(project_id, character_name, anchor_key, status)",
    # 部分唯一索引：每个 (项目, 角色, 年龄段) 最多一条 current；insert_generating/
    # mark_finished 都不直接写 status='current'（只有 set_current 会），所以这里
    # 不会跟正常生成流程的落库顺序冲突。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_character_voices_current "
    "ON character_voices(project_id, character_name, anchor_key) WHERE status='current'",
)

STATUS_GENERATING = "generating"
STATUS_CANDIDATE = "candidate"
STATUS_CURRENT = "current"
STATUS_FAILED = "failed"
STATUS_RETIRED = "retired"

MAX_NONCURRENT_PER_CHARACTER = 5
#: 超过这个时长还停在 generating 的行按「生成中断」呈现（服务重启/后台任务
#: 丢失），不是永远显示"生成中"——真实生成流程约 10 秒内完成，10 分钟是
#: 留足网络重试余量后的保守上限。
GENERATING_STALE_AFTER_S = 600.0

_ensured_paths: set[str] = set()


def ensure_tables_on_connection(conn: sqlite3.Connection) -> None:
    """轻量、同连接、无副作用的建表兜底，安全用于调用方已持有事务的场景。"""
    for statement in _CREATE_STATEMENTS:
        conn.execute(statement)


def ensure_schema() -> None:
    """幂等建表；按当前 ``db.DB_PATH`` 记忆已建，避免每次调用都重跑 DDL。

    调用方选错入口不再有后果：``app.db.get_conn()`` 若已经处在调用方开的事务
    里，直接改走同连接的 ``ensure_tables_on_connection``，不开独立连接、不抢
    锁；否则保持独立连接行为（``db._run_write_transaction_once``）。
    """
    key = str(db.DB_PATH)
    if key in _ensured_paths:
        return

    def _run_independent() -> None:
        def operation(conn: sqlite3.Connection) -> None:
            ensure_tables_on_connection(conn)

        try:
            db._run_write_transaction_once(operation)
        except Exception:  # noqa: BLE001 建表失败留到下一次调用重试，不阻塞调用方
            return
        _ensured_paths.add(key)

    db_schema.ensure_schema_respecting_caller_transaction(
        db.get_conn(),
        on_caller_connection=ensure_tables_on_connection,
        run_independent=_run_independent,
    )


def voice_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def insert_generating(
    conn: sqlite3.Connection, *, project_id: str, character_name: str, anchor_key: str = "",
    model_id: str, voice_prompt: str, preview_text: str, created_by: str,
) -> str:
    """登记一条 generating 行，返回其 id。调用方负责在合适的时机 ``commit()``
    ——这一步应当独立提交，供应商调用前不持有写事务（禁止写锁跨 await）。"""
    ensure_schema()
    voice_id = db.new_id("voice")
    stamp = db.now()
    conn.execute(
        "INSERT INTO character_voices(id, project_id, character_name, anchor_key, status, "
        "source, model_id, voice_prompt, preview_text, created_at, updated_at, created_by) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (voice_id, project_id, character_name, anchor_key, STATUS_GENERATING,
         "design", model_id, voice_prompt, preview_text, stamp, stamp, created_by),
    )
    return voice_id


def mark_finished(
    conn: sqlite3.Connection, project_id: str, voice_id: str, *, status: str,
    provider_voice_id: str = "", audio_path: str = "", clip_path: str = "",
    clip_duration_s: float | None = None, clip_sha256: str = "",
    check_status: str = "unchecked", check_reason: str = "", asr_text: str = "",
    asr_match: float | None = None, error: str = "",
) -> None:
    """把一条 generating 行落定为 candidate 或 failed（``status`` 由调用方决定，
    本函数不判断）。不写 status='current'——采用是单独的 ``set_current``。"""
    ensure_schema()
    conn.execute(
        "UPDATE character_voices SET status=?, provider_voice_id=?, audio_path=?, clip_path=?, "
        "clip_duration_s=?, clip_sha256=?, check_status=?, check_reason=?, asr_text=?, "
        "asr_match=?, error=?, updated_at=? WHERE id=? AND project_id=?",
        (status, provider_voice_id, audio_path, clip_path, clip_duration_s, clip_sha256,
         check_status, check_reason, asr_text, asr_match, error, db.now(), voice_id, project_id),
    )


def get(conn: sqlite3.Connection, project_id: str, voice_id: str) -> sqlite3.Row | None:
    ensure_schema()
    return conn.execute(
        "SELECT * FROM character_voices WHERE id=? AND project_id=?", (voice_id, project_id),
    ).fetchone()


def list_for_character(
    conn: sqlite3.Connection, project_id: str, character_name: str, anchor_key: str = "",
) -> list[sqlite3.Row]:
    """该角色（指定年龄段）的全部声音行，按创建时间倒序。"""
    ensure_schema()
    return conn.execute(
        "SELECT * FROM character_voices WHERE project_id=? AND character_name=? AND anchor_key=? "
        "ORDER BY created_at DESC",
        (project_id, character_name, anchor_key),
    ).fetchall()


def current_for(
    conn: sqlite3.Connection, project_id: str, character_name: str, anchor_key: str = "",
) -> sqlite3.Row | None:
    ensure_schema()
    return conn.execute(
        "SELECT * FROM character_voices WHERE project_id=? AND character_name=? AND anchor_key=? "
        "AND status=?",
        (project_id, character_name, anchor_key, STATUS_CURRENT),
    ).fetchone()


def effective_status(row: sqlite3.Row, *, now_ts: float | None = None) -> tuple[str, str]:
    """把「生成中断」的 stale generating 行投影为 failed，供读路径展示——不落库
    改动（GET 不应该有写副作用），真正落库的判定只在生成流程自己的
    ``mark_finished`` 里发生。"""
    status = str(row["status"])
    error = str(row["error"] or "")
    if status == STATUS_GENERATING:
        cutoff = (now_ts if now_ts is not None else db.now()) - GENERATING_STALE_AFTER_S
        if float(row["created_at"] or 0) <= cutoff:
            return STATUS_FAILED, "生成中断（服务重启或后台任务丢失），请重新生成"
    return status, error


def set_current(
    conn: sqlite3.Connection, project_id: str, character_name: str, voice_id: str,
    anchor_key: str = "", *, adopted_by: str,
) -> None:
    """把 ``voice_id`` 设为该角色（同年龄段）的 current；原 current（若存在且
    不是同一行）降级为 candidate 留在候选池里（受 ``prune_noncurrent`` 的保留
    上限约束），不是被删除——人工采用允许换回旧版本。"""
    ensure_schema()
    stamp = db.now()
    conn.execute(
        "UPDATE character_voices SET status=? WHERE project_id=? AND character_name=? "
        "AND anchor_key=? AND status=? AND id<>?",
        (STATUS_CANDIDATE, project_id, character_name, anchor_key, STATUS_CURRENT, voice_id),
    )
    conn.execute(
        "UPDATE character_voices SET status=?, adopted_at=?, adopted_by=?, updated_at=? "
        "WHERE id=? AND project_id=?",
        (STATUS_CURRENT, stamp, adopted_by, stamp, voice_id, project_id),
    )


def prune_noncurrent(
    conn: sqlite3.Connection, project_id: str, character_name: str, anchor_key: str = "",
    *, keep: int = MAX_NONCURRENT_PER_CHARACTER,
) -> None:
    """非当前行超过 ``keep`` 条时删最旧的，连同它们记录的文件——只删行里记录
    的路径，不扫目录（CLAUDE.md「机器配置一般」：保留类数据要有上限）。"""
    ensure_schema()
    rows = conn.execute(
        "SELECT id, audio_path, clip_path FROM character_voices WHERE project_id=? "
        "AND character_name=? AND anchor_key=? AND status<>? ORDER BY created_at DESC",
        (project_id, character_name, anchor_key, STATUS_CURRENT),
    ).fetchall()
    for row in rows[keep:]:
        conn.execute("DELETE FROM character_voices WHERE id=?", (row["id"],))
        for path in (row["audio_path"], row["clip_path"]):
            if path:
                Path(path).unlink(missing_ok=True)


def migrate_character_voices(
    conn: sqlite3.Connection, project_id: str, from_name: str, to_name: str,
) -> None:
    """卡改名/合并时随 ``character_portraits`` 同一事务迁移声音行（CLAUDE.md
    「卡改名/合并联动」）。目标角色若已有 current，来源角色的 current 行改名
    后降级为 retired（不删文件，只是不再是任何角色的当前声音）；目标没有
    current 时来源的 current 直接顶上。非当前行一律随行迁移，不做额外裁剪
    ——``prune_noncurrent`` 走各自独立的生成/采用路径触发，这里只搬家。
    """
    ensure_schema()
    if from_name == to_name:
        return
    rows = conn.execute(
        "SELECT id, anchor_key, status FROM character_voices WHERE project_id=? AND character_name=?",
        (project_id, from_name),
    ).fetchall()
    stamp = db.now()
    for row in rows:
        anchor_key = row["anchor_key"]
        new_status = row["status"]
        if new_status == STATUS_CURRENT:
            clash = conn.execute(
                "SELECT 1 FROM character_voices WHERE project_id=? AND character_name=? "
                "AND anchor_key=? AND status=?",
                (project_id, to_name, anchor_key, STATUS_CURRENT),
            ).fetchone()
            if clash is not None:
                new_status = STATUS_RETIRED
        conn.execute(
            "UPDATE character_voices SET character_name=?, status=?, updated_at=? WHERE id=?",
            (to_name, new_status, stamp, row["id"]),
        )
