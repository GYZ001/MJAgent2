"""真名揭示后的人物卡改绑：旧称谓卡就地改名，不新建第二张卡。

``app/identity_adjudication.py`` 的 IR 身份仲裁在 ``status=="bind"`` 时可能同时
揭晓一个已绑定实体的稳定真名（例如某人一直以「许师姐」建卡，本集原文第一次
逐字给出真名「许清」）。过去这种情况只改 ``identity.authority_id``，人物谱里的
卡永远留着旧称谓——本模块是这条改绑路径唯一的落地点。

改绑只做「就地改名」，刻意不做「合并」：若目标真名在人物谱里已经有归属（
``app.portraits.card_owner.resolve_card_owner`` 返回 owner 或 conflict），那是
两张卡其实是同一个人，需要人工判断如何合并，不是本原语的职责——遇到就 fail
closed 抛出可见异常，不允许静默跳过也不允许猜测哪张卡该留下。

定妆资产原地保留：本模块只改 ``characters[].name`` 与追加一条别名，绝不触碰
``ref_image_path`` / ``portrait_prompt_override`` / 已采纳的 portrait 行内容——
改名不等于重新定妆，新猜出的照片会丢失已核验的连续性锚点。

``character_portraits`` 是改名后第二个必须同步的表：该表按
``(project_id, character_name, ep_start)`` 检索定妆照，改名后旧称谓查询会
查无此卡。扫描/更新该表必须加 ``ep_start >= 0``——负数 ``ep_start`` 是
``promote_staged_initial_portrait`` 压进去的已作废定妆照历史槽位（从 -1
递减、``ep_end=0``），是死数据，不是当前生效的区间标记，纳入改名会把历史
槽位错误地当成当前生效行处理。

原子性：bible_json 的 CAS 写入成功之后才更新 ``character_portraits`` 的旧
称谓引用，且两者在同一个 DB 事务里一起提交——bible CAS 失败（并发改写撞车）
时整个函数直接返回 False，``character_portraits`` 不会被触碰，旧卡原封不动。

内部按接缝拆成四个小函数，``_rebind_character_card_cas`` 只做编排：前置校验
（``_reject_if_target_name_owned``）、人物谱条目改写
（``_rewrite_character_entry``）、证据 artifact 落盘（``_create_rebind_artifact``）、
CAS 写回（``_cas_write_bible``），互不改变彼此的错误处理契约。
"""

from __future__ import annotations

import json

from app.db import get_conn
from app.errors import ContentGenerationError, code_ref
from app.evidence import repository as evidence_repository
from app.harness.types import EvidenceArtifact
from app.schemas import Bible, CharacterAlias

from ._db_probe import _has_column, _has_table
from .card_owner import resolve_card_owner
from .constants import IDENTITY_NAME_FORM_REFERENTIAL
from .discovery_fragments import _bible_lock

# 改绑生成的别名没有原文引句可核验（本原语不接收章节/原文输入），如实标注
# 为「未核验」，不假装有证据锚点——is_exclusive=False 与 app.stages.roster_
# recurring._attach_roster_source_appellations 的免检通道同一纪律。
_REBIND_ALIAS_EVIDENCE_CHAPTER_INDEX = -1
_REBIND_ALIAS_EVIDENCE_QUOTE = "身份仲裁改绑：无原文引句锚点，未核验"


def _reject_if_target_name_owned(
    data: dict, from_label: str, to_canonical_name: str,
) -> None:
    """目标真名若在人物谱里已有归属，fail closed：合并语义不在本原语职责内。

    唯一的安全放行例外：owner 恰好就是 ``from_label`` 本身——这不是"两张卡
    其实是同一个人"，而是同一张卡自己的一条别名先被别的通道（如免核验的
    共现别名回填）记过，真名揭示只是把它从别名提升为主名，不构成合并。
    """
    owner_status, owner_info = resolve_card_owner(
        Bible.model_validate(data), to_canonical_name,
    )
    if owner_status == "none" or (owner_status == "owner" and owner_info == from_label):
        return
    raise ContentGenerationError(
        f"身份改绑目标真名「{to_canonical_name}」在人物谱中已有归属"
        f"（{owner_status}：{owner_info}），需要人工合并，不能盲目改名"
    )


def _rewrite_character_entry(
    data: dict, from_label: str, to_canonical_name: str,
) -> bool:
    """把 ``from_label`` 对应的角色条目就地改名，旧称谓降为未核验别名。

    返回 ``False`` 表示 ``from_label`` 在当前 bible_json 里查无此卡——调用方
    据此判定改绑无需发生，不是异常。若 ``to_canonical_name`` 已经是这张卡自己
    的一条别名（见 ``_reject_if_target_name_owned`` 的自我提升例外），丢弃那条
    别名——它现在与 name 字段重复，不是新增信息。
    """
    target = next(
        (c for c in data.get("characters", []) if c.get("name") == from_label),
        None,
    )
    if target is None:
        return False
    aliases = [
        a for a in target.get("aliases", []) or []
        if str(a.get("text") or "").strip() != to_canonical_name
    ]
    alias_texts = {str(a.get("text") or "").strip() for a in aliases}
    target["aliases"] = aliases
    target["name"] = to_canonical_name
    if from_label not in alias_texts:
        target.setdefault("aliases", []).append(CharacterAlias(
            text=from_label,
            name_kind=IDENTITY_NAME_FORM_REFERENTIAL,
            evidence_chapter_index=_REBIND_ALIAS_EVIDENCE_CHAPTER_INDEX,
            evidence_quote=_REBIND_ALIAS_EVIDENCE_QUOTE,
            is_exclusive=False,
        ).model_dump(mode="json"))
    return True


def _create_rebind_artifact(
    project_id: str, row, data: dict, from_label: str, to_canonical_name: str,
) -> tuple[bool, str | None]:
    """产出改名后 bible_json 的证据 artifact，串进既有 lineage。

    返回 ``(ok, artifact_id)``；``ok=False`` 表示 artifact 创建失败——调用方须
    fail closed，不落库、不移除任何旧引用。
    """
    try:
        previous_id = row["bible_artifact_id"]
        artifact = evidence_repository.create_artifact(EvidenceArtifact(
            type="character_bible",
            scope_type="project",
            scope_id=project_id,
            status="approved",
            trust_level="T2",
            content=data,
            parent_artifact_ids=[previous_id] if previous_id else [],
            contract_version="character-bible-1.0.0",
            prompt_version="identity-adjudication-rebind-1.0.0",
            model_snapshot={
                "operation": "rebind_character_card",
                "from_label": from_label,
                "to_canonical_name": to_canonical_name,
            },
        ))
        return True, artifact["id"]
    except Exception as exc:  # noqa: BLE001 - authority mutation must fail closed
        code_ref(
            exc,
            action="rebind_character_card_artifact",
            context={
                "project_id": project_id,
                "from_label": from_label,
                "to_canonical_name": to_canonical_name,
            },
        )
        return False, None


def _cas_write_bible(
    conn, project_id: str, row, payload: str,
    artifact_supported: bool, next_artifact_id: str | None,
) -> bool:
    """乐观并发 CAS 写回 bible_json；并发撞车返回 False，调用方据此中止整个改绑。"""
    expected_version = int(row["bible_version"] or 0)
    if artifact_supported:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=?,bible_artifact_id=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (payload, expected_version + 1, next_artifact_id, project_id, expected_version),
        )
    else:
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=? "
            "WHERE id=? AND COALESCE(bible_version,0)=?",
            (payload, expected_version + 1, project_id, expected_version),
        )
    if cursor.rowcount != 1:
        conn.rollback()
        return False
    return True


def _rebind_character_card_cas(
    conn, project_id: str, from_label: str, to_canonical_name: str,
) -> bool:
    artifact_supported = (
        _has_column(conn, "projects", "bible_artifact_id")
        and _has_table(conn, "artifacts")
    )
    select_cols = "bible_json, bible_version"
    if artifact_supported:
        select_cols += ", bible_artifact_id"
    row = conn.execute(
        f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,),
    ).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    _reject_if_target_name_owned(data, from_label, to_canonical_name)
    if not _rewrite_character_entry(data, from_label, to_canonical_name):
        return False
    payload = json.dumps(data, ensure_ascii=False)
    next_artifact_id = None
    if artifact_supported:
        ok, next_artifact_id = _create_rebind_artifact(
            project_id, row, data, from_label, to_canonical_name,
        )
        if not ok:
            return False
    if not _cas_write_bible(
        conn, project_id, row, payload, artifact_supported, next_artifact_id,
    ):
        return False
    conn.execute(
        "UPDATE character_portraits SET character_name=? "
        "WHERE project_id=? AND character_name=? AND ep_start>=0",
        (to_canonical_name, project_id, from_label),
    )
    conn.commit()
    return True


async def rebind_character_card(
    project_id: str, from_label: str, to_canonical_name: str,
) -> bool:
    """把旧称谓卡（``from_label``）就地改名为真名（``to_canonical_name``）。

    返回 ``True`` 表示改绑落地；``False`` 表示无需改绑（旧卡不存在，或与目标
    同名，或并发 CAS 撞车——调用方可按原有 authority_id 绑定继续，人物谱本身
    不受影响）。目标真名已有归属时不会返回 ``False``，而是抛出
    ``ContentGenerationError``——那是需要人工合并的信号，不是可以吞掉的分支。
    """
    from_label = str(from_label or "").strip()
    to_canonical_name = str(to_canonical_name or "").strip()
    if not from_label or not to_canonical_name or from_label == to_canonical_name:
        return False
    lock = await _bible_lock(project_id)
    async with lock:
        conn = get_conn()
        return _rebind_character_card_cas(
            conn, project_id, from_label, to_canonical_name,
        )
