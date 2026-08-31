"""POST /projects/{project_id}/characters/nominate：用户提名一个原文称呼，让
系统按既有建卡判据处理——不是"让用户手写一张卡"（那会绕开原文证据检索、
subject_kind=person 硬闸、外观生成，还会制造重名）。

三态路由，判据全部来自 ``app.portraits.card_owner.resolve_card_owner``（人物谱
建卡去重唯一权威解析器，见该模块 docstring，本文件不新写第二套匹配逻辑）：

- 命中已有角色（``resolve_card_owner`` 返回 ``owner``）：不新建卡；尝试把提名的
  称呼登记为该角色的一条别名——核验路径复用
  ``app.portraits.card_aliases.new_card_aliases`` 的共现证据判据（称呼与角色
  规范名要能在原文同一短窗口内共现），不自己写一套；核验不过时只回报归属者，
  不登记、不编造别名。响应 ``status="exists"``，与 ``ensure_character_card``
  自身并发竞态下的早退语义保持同一个状态字（见下）。
- 冲突（``resolve_card_owner`` 返回 ``conflict``）：该称呼精确命中 ≥2 个角色
  （真实存在的合法数据，例如"大汉"同时是两个人的别名），fail closed——列出
  全部命中者，需要人工判断，不替用户猜一个。响应 ``status="conflict"``。
- 都没命中（``none``）：走 ``app.portraits.cards.ensure_character_card
  (require_identity_card=True)``，原文片段检索、``subject_kind=person`` 硬闸、
  外观生成、长度校验全部照旧，一条都不绕过；被拒时把它返回的真实 ``status``
  （``skipped_minor``/``card_incomplete``/``skipped_not_person``/``error``——
  实测 ``require_identity_card=True`` 时 ``app.portraits.card_verdict.
  unimportant_verdict_result`` 在 ``card_incomplete``/``skipped_minor`` 判据
  之前先命中专属的 ``if require_identity_card: return error`` 分支，所以这条
  路径下卡片不完整/戏份判定不足实际都落成 ``status="error"``，reason 里仍带
  模型给的具体越界数值——本文件只负责原样透出 ``ensure_character_card`` 真实
  返回的 status/reason，不在这里补一层"翻译成 card_incomplete"，也不糊成
  一句"建卡失败"）。``ensure_character_card`` 自己在并发写锁内复查也可能发现
  称呼已被抢先建卡/产生歧义，此时它返回的 ``exists``/``conflict`` 与上面两条
  早退分支同构，本文件统一成同一份响应形状再返回。

``from_episode_no``：原文证据检索的起点集数（``ensure_character_card`` 与建卡
核验共用的 ``_forward_fragments`` 都以它为锚点向后检索一个固定窗口），省略时
按 1 处理——若称呼在小说更靠后的章节才出现，调用方应显式传入更接近的集数，
见前端提名表单的起始集数输入。
"""
from __future__ import annotations

import json

from fastapi import HTTPException

from app.db import get_conn
from app.domain.common import _project_or_404, router
from app.portraits.card_aliases import new_card_aliases
from app.portraits.card_owner import resolve_card_owner
from app.portraits.cards import ensure_character_card
from app.portraits.discovery_fragments import _bible_lock, _forward_fragments
from app.schemas import Bible

_STATUS_MESSAGES = {
    "added": "「{name}」已建卡",
    "exists": "「{name}」已经是人物谱里「{owner}」的别名/本名，未新建卡",
    "conflict": "「{name}」在人物谱中同时命中 {owners}，无法安全判定唯一归属，需要人工判断",
    "skipped_minor": "模型判定「{name}」戏份不足，未建卡：{reason}",
    "card_incomplete": "「{name}」未通过人物卡完整度校验，未建卡：{reason}",
    "skipped_not_person": "「{name}」不是可建卡的人物（{subject_kind}），未建卡：{reason}",
    "error": "「{name}」建卡失败：{reason}",
}


def _from_episode_no(body: dict) -> int:
    raw = body.get("from_episode_no")
    try:
        value = int(raw) if raw not in (None, "") else 1
    except (TypeError, ValueError):
        value = 1
    return value if value > 0 else 1


def _status_message(result: dict) -> str:
    template = _STATUS_MESSAGES.get(result.get("status") or "")
    if not template:
        return result.get("reason") or "提名已处理"
    return template.format(
        name=result.get("name") or result.get("label") or "",
        owner=result.get("owner") or "",
        owners="、".join(result.get("owners") or []),
        reason=result.get("reason") or "",
        subject_kind=result.get("subject_kind") or "",
    )


async def _register_alias_if_verified(
    project_id: str, owner_name: str, label: str, from_episode_no: int,
) -> dict:
    """核验通过则把 ``label`` 登记为 ``owner_name`` 的别名；不通过只回报原因，
    不登记、不编造。核验路径见模块 docstring。"""
    if label == owner_name:
        return {"alias_registered": False, "alias_reason": "该称呼就是角色本名"}
    lock = await _bible_lock(project_id)
    async with lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT bible_json, bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if not row or not row["bible_json"]:
            return {"alias_registered": False, "alias_reason": "人物谱不存在"}
        data = json.loads(row["bible_json"])
        target = next((c for c in data.get("characters", []) if c.get("name") == owner_name), None)
        if target is None:
            return {"alias_registered": False, "alias_reason": f"角色「{owner_name}」不存在"}
        existing_texts = {str(a.get("text") or "").strip() for a in target.get("aliases", []) or []}
        if label in existing_texts:
            return {"alias_registered": False, "alias_reason": "该称呼已经登记过"}
        _, _, chapters_by_idx = _forward_fragments(conn, project_id, label, from_episode_no)
        verified = new_card_aliases(owner_name, [label], chapters_by_idx)
        if not verified:
            return {
                "alias_registered": False,
                "alias_reason": "原文中未找到该称呼与角色本名的共现证据，未登记",
            }
        target.setdefault("aliases", []).extend(verified)
        expected_version = int(row["bible_version"] or 0)
        cursor = conn.execute(
            "UPDATE projects SET bible_json=?,bible_version=? WHERE id=? AND COALESCE(bible_version,0)=?",
            (json.dumps(data, ensure_ascii=False), expected_version + 1, project_id, expected_version),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return {"alias_registered": False, "alias_reason": "并发写入冲突，请重试"}
        conn.commit()
        return {"alias_registered": True, "alias_reason": None}


def _normalize_ensure_result(result: dict, label: str) -> dict:
    """把 ``ensure_character_card`` 并发竞态下的 ``exists``/``conflict`` 早退
    归一成本端点自己的响应形状（字段含义与 ``_card_owner_lookup`` 不同——它的
    ``name`` 在 exists 分支里是归属者而不是被查询的标签，见该函数 docstring），
    其余状态原样透传并补上 ``label``。"""
    status = result.get("status")
    if status == "exists":
        return {"status": "exists", "label": label, "owner": result.get("name") or ""}
    if status == "conflict":
        return {"status": "conflict", "label": label, "owners": result.get("owners") or []}
    result.setdefault("label", label)
    return result


@router.post("/projects/{project_id}/characters/nominate")
async def nominate_character(project_id: str, body: dict):
    from app.capabilities.dispatch import ui_route

    body = body or {}
    label = str(body.get("label") or "").strip()
    from_episode_no = _from_episode_no(body)
    routed = await ui_route(
        "bible.nominate_character",
        {"project_id": project_id, "label": label, "from_episode_no": from_episode_no},
    )
    if routed is not None:
        return routed

    p = _project_or_404(project_id)
    if not label:
        raise HTTPException(422, "请输入要提名的称呼")
    if not p.get("bible_json"):
        raise ValueError("请先生成角色圣经，才能提名角色")

    bible = Bible.model_validate(json.loads(p["bible_json"]))
    status, value = resolve_card_owner(bible, label)
    if status == "conflict":
        result = {"status": "conflict", "label": label, "owners": value}
        result["message"] = _status_message(result)
        return result
    if status == "owner":
        alias_result = await _register_alias_if_verified(project_id, value, label, from_episode_no)
        result = {"status": "exists", "label": label, "owner": value, **alias_result}
        result["message"] = (
            f"「{label}」已登记为「{value}」的别名" if alias_result["alias_registered"]
            else _status_message(result) + (
                f"（{alias_result['alias_reason']}）" if alias_result.get("alias_reason") else ""
            )
        )
        return result

    raw_result = await ensure_character_card(
        project_id, label, from_episode_no, require_identity_card=True,
    )
    result = _normalize_ensure_result(raw_result, label)
    result["message"] = _status_message(result)
    return result
