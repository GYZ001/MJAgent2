"""剧本人物增量发现：剧本生成任务体的 stage 0，独立于主任务体可被剧本修复流程复用。

从 ``app/domain/screenplay_ops/task_body.py`` 按原样搬移（2026-08-30，层号
治理，消掉 ``app/LAYERS.toml`` 组 12 全仓最后一条上行边
``app.production.screenplay_repair.checkpoint_recovery -> app.domain.
screenplay_ops``）：``_screenplay_character_discovery`` 本身只依赖
``app.db``（L2）/``app.errors``（L2）/``app.orchestration.state_machine``
（L2）/``app.stages``（L4）/延迟 ``app.portraits``（L4）/延迟
``app.source_paratext``（L4）/延迟 ``app.observability.tracing``（L1）+ 同包
``.run_owner``（L2，同批从 ``run_control.py`` 搬出的
``_assert_screenplay_run_owner``）。``task_body.py`` 里其余函数
（``_screenplay_task``/``_new_screenplay_recorder``/
``_reserve_screenplay_concurrency_slot``/``_screenplay_context_pack``/
``_recorded_screenplay_task``）才是真正需要 ``task_registry``/
``app.orchestration.engine.WorkflowRecorder``/``app.hiagent``/``app.quota``
等 L5 编排依赖的部分，整个文件不能降级——只把这一个函数搬到独立文件，供
``app.production.screenplay_repair.checkpoint_recovery``（L4）直接引用，
不再需要经 ``app.domain.screenplay_ops`` 包聚合入口（默认 L5）中转。

``_project_bible_or_placeholder`` 改直接从 ``app.visual_styles``（L1）导入，
不再经 ``app.domain.common`` 这层默认 L5 的聚合外观转手——是同一个函数对象
（``app.domain.common`` 本身就是 ``from app.visual_styles import
_project_bible_or_placeholder`` 转发来的），行为不变。

``task_body.py`` 从本文件重新导入并保持原名可从
``app.domain.screenplay_ops``/``app.domain``/``.task_body`` 原样导入，不影响
既有调用点。
"""
from __future__ import annotations

from typing import Any

from app import errors
from app.db import get_conn
from app.orchestration.state_machine import StateConflict
from app.stages import StageError
from app.visual_styles import _project_bible_or_placeholder

from .run_owner import _assert_screenplay_run_owner


async def _screenplay_character_discovery(
    episode_id: str,
    source_text: str,
    *,
    draft_text: str = "",
) -> dict[str, Any]:
    """Run the required incremental cast pass for one screenplay generation."""
    conn = get_conn()
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if not ep:
        raise StageError("新人物发现", ["剧集不存在"])
    project = conn.execute("SELECT * FROM projects WHERE id=?", (ep["project_id"],)).fetchone()
    if not project:
        raise StageError("新人物发现", ["项目不存在"])
    _assert_screenplay_run_owner(episode_id)
    if not (project["bible_json"] or "").strip():
        # 剧本允许先于完整人物谱生产，但人物身份不能因此绕过预检。先原子写入
        # 最小骨架，后续仍由既有增量流程建文字卡；bible_status 保持原值，
        # 不把这个骨架伪装成用户已完成的人物谱。
        placeholder = _project_bible_or_placeholder(project)
        conn.execute(
            "UPDATE projects SET bible_json=? "
            "WHERE id=? AND COALESCE(TRIM(bible_json), '')=''",
            (placeholder.model_dump_json(), ep["project_id"]),
        )
        conn.commit()
        project = conn.execute(
            "SELECT * FROM projects WHERE id=?", (ep["project_id"],)
        ).fetchone()
    bible = _project_bible_or_placeholder(project)
    from app.portraits import (
        ensure_cards_for_text,
        persist_screenplay_character_resolutions,
        screenplay_identity_scope_fingerprint,
    )

    # 人物发现是剧本 stage 0，跑在叙事蓝图**之前**，拿不到蓝图那份 paratext
    # 判定，于是会把作者的话里的人名（作者笔名本身）当成出场人物立卡。
    # 这里用同一份判据先净化一次；判不出来就退回原文，绝不挡住人物发现。
    # 只净化**发现用**的文本，剧本链路的 source_text 一个字都不动——
    # 那里需要完整原文做 audit_only 来源审计，删字会让 SRC 段编号错位。
    from app.source_paratext import strip_paratext

    discovery_text = await strip_paratext(
        source_text, operation_id=f"screenplay.discovery.paratext:{episode_id}"
    )
    try:
        result = await ensure_cards_for_text(
            ep["project_id"],
            ep["episode_no"],
            discovery_text,
            bible,
            draft_text=draft_text,
            generate_portraits=False,
            write_guard=lambda: _assert_screenplay_run_owner(episode_id),
        )
    except (StageError, StateConflict):
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成剧本阶段可恢复诊断
        from app.errors import code_ref

        public = code_ref(
            exc,
            action="screenplay_character_discovery",
            context={"episode_id": episode_id, "project_id": ep["project_id"]},
        )
        raise StageError(
            "新人物发现",
            [
                f"人物身份模型暂未完成本集预检，请在剧本阶段重试（{public}）"
                "[IDENTITY_DISCOVERY_FIXED_RETRY_BUDGET]"
            ],
        ) from exc
    if result.get("errors"):
        raise StageError("新人物发现", list(result["errors"]))
    _assert_screenplay_run_owner(episode_id)
    from app.observability.tracing import current_trace

    expected_run_id = current_trace().run_id
    result["resolutions"] = persist_screenplay_character_resolutions(
        conn,
        episode_id,
        result.get("resolutions") or [],
        retire_legacy_future_identity=True,
        expected_active_run_id=expected_run_id,
        replace_identity_scope=screenplay_identity_scope_fingerprint(
            int(ep["episode_no"]), source_text
        ),
    )
    for warning in result.get("warnings") or []:
        errors.log_error(
            None,
            action="screenplay_character_discovery_warning",
            context={
                "project_id": ep["project_id"],
                "episode_id": episode_id,
                "episode_no": ep["episode_no"],
            },
            message=warning,
        )
    return result
