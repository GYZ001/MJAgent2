"""道具卡复核「存疑」的人工确认/保留端点（2026-10-03-v2 新增，
``app.domain.bible_ops.props_api`` 的 POST 入口落在这里）。

一条存疑（``app.props.card_audit_consensus`` 产出，结构见该模块与
``card_audit_store`` 的 docstring）代表"两次独立判定不一致 / 归属没有自己的
卡 / 模型自述拿不准"里的任意一种——模型没能给出足够确定的删除判定，交给
人工看过原文再决定。两个出路：

- 确认删除：人工认定这条外观/别名确实该删，走与自动删除**同一条核验**
  （别名经 ``app.props.card_audit._remove_prop_aliases``、子句经
  ``judge.rebuild_appearance_excluding``）+ 重出图 + 原子回滚（重出图失败
  时旧外观/旧图原封不动，整个请求失败，不留半成品）。
- 保留：人工认定模型的怀疑是误报，记录决定后同一规则版本内不再重复呈现
  （CLAUDE.md「拦住用户时必须给出路」——不止给「确认删除」，也要能让用户
  说「我看过了，这条没问题」，且这个决定要持久化）。

两个端点都按**卡名 + 存疑原文**定位（``doubt_key`` 本身就是
``f"clause:{text}"``/``f"alias:{alias}"``，不是不透明的随机 id），不用下标
——前面的删除会让子句下标错位，原文逐字匹配才是稳定的。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app import db
from app.db import get_conn, now
from app.schemas import Bible, Prop

from . import card_audit, card_audit_consensus, card_audit_store, judge

log = logging.getLogger(__name__)


def _locate_doubt(row: dict[str, Any], doubt_key: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从某张卡最近一轮复核记录里取出全部存疑、以及 ``doubt_key`` 指认的
    那一条；找不到抛 ``ValueError``（路由层转 409）——两个端点「按卡名与
    子句/别名原文逐字定位」的共用前半段。"""
    doubts = json.loads(row["doubts_json"]) if row["doubts_json"] else []
    doubt = next((d for d in doubts if card_audit_consensus.doubt_key(d) == doubt_key), None)
    if doubt is None:
        raise ValueError(f"存疑「{doubt_key}」不存在或已被处理，可能页面还没刷新到最新结果")
    return doubts, doubt


async def _delete_doubted_alias(conn: Any, project_id: str, prop: Prop, alias: str) -> bool:
    if alias not in prop.aliases:
        raise ValueError(f"别名「{alias}」已不在当前别名列表里，可能已被处理，请刷新后重试")
    card_audit._remove_prop_aliases(conn, project_id, prop.name, [alias])  # 内部已 commit，见 mutate_bible_json
    return False  # 别名删除不涉及出图，reimaged 恒为 False


async def _delete_doubted_clause(conn: Any, project_id: str, bible: Bible, prop: Prop, text: str) -> bool:
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    if text not in clauses:
        raise ValueError("这条外观子句的原文在当前外观里找不到逐字匹配，道具卡可能已经变化，无法定位删除")
    index = clauses.index(text) + 1
    new_appearance = judge.rebuild_appearance_excluding(prop.appearance_canonical, {index})
    result = {"removed_aliases": [], "appearance_changed": True, "new_appearance": new_appearance}
    applied = await card_audit._apply_audit_result(
        conn, project_id, prop, result,
        style=bible.world.visual_style_canonical, episode_no=prop.first_episode_no or 1,
    )
    if not applied["reimaged"]:
        raise ValueError("重新出图失败，外观与参考图均未改变，请稍后重试——失败前的旧外观与旧图原封不动")
    return True


async def confirm_doubt(project_id: str, prop_name: str, doubt_key: str) -> dict[str, Any]:
    """人工确认删除一条存疑，见模块 docstring。"""
    conn = get_conn()
    bible = card_audit._load_bible(conn, project_id)
    if bible is None:
        raise ValueError(f"项目不存在或人物谱未初始化：{project_id}")
    prop = next((p for p in bible.props if p.name == prop_name), None)
    if prop is None:
        raise ValueError(f"道具不存在：{prop_name}")
    row = card_audit_store.get_audit(conn, project_id=project_id, prop_name=prop_name)
    if row is None:
        raise ValueError(f"道具「{prop_name}」还没有复核记录，无法定位存疑")
    _doubts, doubt = _locate_doubt(row, doubt_key)
    if doubt["kind"] == "alias":
        reimaged = await _delete_doubted_alias(conn, project_id, prop, doubt["alias"])
    else:
        reimaged = await _delete_doubted_clause(conn, project_id, bible, prop, doubt["text"])
    # 外观/别名此刻已经改完并提交（见上面两条分支），下面这步是记录"这条存疑
    # 已处理"——两步不在同一个事务里（CLAUDE.md「诊断类写入用独立连接」的同一
    # 精神：状态转移用谁的事务要显式声明，这里卡内容变更用调用方连接，存疑记录
    # 用独立连接，不混在一次提交里）。如果这一步失败，卡内容的改动不回退（已经
    # 生效，不是"什么都没发生"），所以失败必须显式告知用户已经改了什么、该怎么
    # 办，不能让异常原样冒泡成一个暗示"全部失败"的裸 500（审查发现）。
    try:
        await _persist_doubt_resolution(
            row_id=row["id"], project_id=project_id, prop_name=prop_name,
            rules_version=row["rules_version"], doubt_key=doubt_key, decision="deleted",
        )
    except Exception as exc:  # noqa: BLE001 卡内容已经改完，这里失败必须给出明确后续动作
        log.exception(
            "[PROP_CARD_AUDIT_DOUBT_PERSIST_FAILED] project=%s prop=%s doubt_key=%s "
            "外观/别名已删除且已重出图，但存疑记录更新失败", project_id, prop_name, doubt_key,
        )
        raise ValueError(
            f"「{prop_name}」这条存疑对应的外观/别名已经删除并重出图，但存疑记录更新失败，"
            "请刷新页面确认最新状态（不要重复点击，重复点击会因为原文已不存在而被拒绝）"
        ) from exc
    return {"prop_name": prop_name, "doubt_key": doubt_key, "status": "deleted", "reimaged": reimaged}


async def keep_doubt(project_id: str, prop_name: str, doubt_key: str) -> dict[str, Any]:
    """人工确认保留一条存疑，见模块 docstring。"""
    conn = get_conn()
    row = card_audit_store.get_audit(conn, project_id=project_id, prop_name=prop_name)
    if row is None:
        raise ValueError(f"道具「{prop_name}」还没有复核记录，无法定位存疑")
    _locate_doubt(row, doubt_key)
    await _persist_doubt_resolution(
        row_id=row["id"], project_id=project_id, prop_name=prop_name,
        rules_version=row["rules_version"], doubt_key=doubt_key, decision="kept",
    )
    return {"prop_name": prop_name, "doubt_key": doubt_key, "status": "kept"}


async def _persist_doubt_resolution(
    *, row_id: str, project_id: str, prop_name: str, rules_version: str, doubt_key: str, decision: str,
) -> None:
    """确认删除/保留共用的收尾：把这条存疑从剩余列表摘掉、持久化人工决定
    （独立连接的写事务，不借用调用方连接隐式提交——CLAUDE.md「诊断类写入用
    独立连接」的同一精神，这里是状态转移而不是诊断，但同样不该与调用方那边
    可能存在的未提交操作混在一次提交里）。

    ``doubts_json`` 的"剩余列表"必须在**这个写事务内部**重新读取当前最新值
    再计算，不能用调用方在事务外缓存的旧快照（审查发现：并发处理同一张卡的
    两条不同存疑时，后写入的请求若拿着自己请求开始时读到的旧快照整表覆写，
    会把先写入请求已经摘掉的那条"复活"回 doubts_json——即使决定表里已经正确
    记了决定，GET /props/audit 还是会把处理过的存疑重新呈现给用户）。
    ``db.run_write_transaction`` 对每个写事务用 ``BEGIN IMMEDIATE`` 独占写锁，
    这里的读与写在同一个事务、同一个连接上，不会与另一个并发写事务交错。"""
    def _operation(conn: Any) -> None:
        card_audit_store.ensure_tables_on_connection(conn)
        fresh_row = card_audit_store.get_audit(conn, project_id=project_id, prop_name=prop_name)
        current_doubts = json.loads(fresh_row["doubts_json"]) if fresh_row and fresh_row["doubts_json"] else []
        remaining = [d for d in current_doubts if card_audit_consensus.doubt_key(d) != doubt_key]
        card_audit_store.update_doubts(conn, row_id=row_id, doubts=remaining)
        card_audit_store.record_doubt_decision(
            conn, project_id=project_id, prop_name=prop_name, rules_version=rules_version,
            doubt_key=doubt_key, decision=decision, stamp=now(),
        )
    await db.run_write_transaction(_operation)
