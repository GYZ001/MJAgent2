"""道具卡「按现行规则复核」（card rules audit）编排：规则版本落后的存量卡
在下次使用前按当前规则重新核验外观子句/别名，核验结果只删不写——新外观是
原子句去掉被删的那些后原样拼接（``judge.rebuild_appearance_excluding``），
不新增、不改写一个字；外观有变化时用现有出图路径重出参考图，**重出成功才
替换旧图与写回外观**，失败时旧外观与旧图原封不动（CLAUDE.md「破坏性操作要
有原子性」）。

三个触发点（见各自调用处的说明）：
1. 新卡创建后立即复核（``app.props.service._register_one_prop``，两条建卡
   路径——映射台 ``ensure_props_for_labels`` 与分镜补卡 ``register_prop_
   card_for_label``——都走这同一个函数，复核只需接一处）；
2. 视频生成前懒复核（``app.props.card_audit_ensure.stale_audit_props_for_
   shots`` 判定 + ``app.props.card_pending_ensure.pending_prop_card_gate``
   接入，见该模块）；
3. 道具库手动入口（``app.domain.bible_ops.props_api``）对整项目全部卡发起。

**对下游的影响**：外观/图片改变会让 ``prop_references`` 的 revision 变化，
但已采纳视频的交付清单不受影响——``app.downstream_authority`` 只核验视频
文件/Artifact/技术门禁，不重新解析参考图清单（与
``tests/test_props_library.py::test_registering_new_prop_card_does_not_
change_adopted_video_delivery_manifest`` 同一结论，复核只是另一种"道具卡
内容变化"，机制相同）。已冻结 manifest 的在途任务会被
``app.media_exec.authority._review_shot_manifest_equal`` 判定依赖漂移而
``REVIEW_DEPENDENCY_STALE`` 失败——这是 ``manifest_props_signature`` 刻意
设计的既有信号（任何道具"无图/旧图"变"新图"都算依赖漂移），不是本功能引入
的新行为，失败可重试、不耗视频生成额度。道具拼图的人脸判定按图片内容哈希
缓存，复核换了新图后该哈希自然不同，会重新判定，无需额外处理。

dry-run（供 B 沙箱对全部存量卡做真实模型验证）：``audit_one_prop_card``/
``audit_project_prop_cards`` 的 ``dry_run=True`` 路径只调用模型 + 代码核验，
不触碰 ``card_audit_store``、不写世界书、不重新出图——纯只读计算，可重复
安全调用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any

from app import db
from app.bible_store import mutate_bible_json
from app.db import get_conn, new_id, now
from app.schemas import Bible, Prop

from . import card_audit_consensus, card_audit_rules, card_audit_store, image, judge, store

log = logging.getLogger(__name__)

MAX_AUDIT_ATTEMPTS = 3
_AUDIT_RUNNING_STALE_S = 600.0
_MAX_CONCURRENT_AUDITS = 3
_BACKGROUND_AUDIT_TASKS: set[asyncio.Task[Any]] = set()


def _load_bible(conn: sqlite3.Connection, project_id: str) -> Bible | None:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    raw = (row["bible_json"] or "").strip() if row else ""
    if not raw:
        return None
    return Bible.model_validate(json.loads(raw))


def _remove_prop_aliases(conn: sqlite3.Connection, project_id: str, name: str, aliases: list[str]) -> bool:
    drop = set(aliases)

    def mutate(data: dict) -> bool:
        for entry in data.get("props", []):
            if entry.get("name") == name:
                current = [a for a in entry.get("aliases") or [] if a not in drop]
                if current == list(entry.get("aliases") or []):
                    return False
                entry["aliases"] = current
                return True
        return False
    return mutate_bible_json(conn, project_id, mutate)


def _set_prop_appearance_and_image(
    conn: sqlite3.Connection, project_id: str, name: str, appearance: str, image_path: str,
) -> bool:
    def mutate(data: dict) -> bool:
        for entry in data.get("props", []):
            if entry.get("name") == name:
                if entry.get("appearance_canonical") == appearance and entry.get("ref_image_path") == image_path:
                    return False
                entry["appearance_canonical"] = appearance
                entry["ref_image_path"] = image_path
                return True
        return False
    return mutate_bible_json(conn, project_id, mutate)


def _apply_negation_group_safety_net(
    appearance: str, removed_indexes: set[int], removed_clauses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """否定关联子句组的安全网（审查发现，CLAUDE.md「不要给以后的生成埋雷」）：
    ``judge.split_appearance_clauses`` 按标点切分，不理解"无A、B"这类共享否定/
    并列结构的作用范围——模型如果只删除这类组合里的一部分（残留的并列项被删、
    否定词那条被留下，或反过来），``rebuild_appearance_excluding`` 原样拼接出
    的新外观会让字面意思反转（"无印花"+"涂鸦等额外装饰"各自独立判定，万一只删
    前者，"涂鸦"从"没有"变成"有"）。这里用 ``judge.negation_linked_clause_
    groups`` 的结构信号识别这类子句组：组内判定不一致（部分删、部分留）时，
    整组强制改判"不删"（原地修改 ``removed_indexes``/``removed_clauses``）——
    宁可多留几个字保留原状，也不让半删产生语义反转；组内判定一致（全删或全不
    删）不受影响，原样放行。返回被强制改判的记录，供日志/测试核查这个安全网
    是否被触发过。"""
    overrides: list[dict[str, Any]] = []
    for group in judge.negation_linked_clause_groups(appearance):
        in_removed = group & removed_indexes
        if in_removed and in_removed != group:
            for index in sorted(in_removed):
                removed_indexes.discard(index)
                overrides.append({
                    "index": index,
                    "reason": "否定关联子句组判定不一致，整组改判保留，避免半删反转语义",
                })
            removed_clauses[:] = [r for r in removed_clauses if r["index"] not in group]
    return overrides


def _partial_coverage_audit_result(prop: Prop, clauses: list[str], missing_indexes: frozenset[int]) -> dict[str, Any]:
    """两次调用里只要有一次没对全部子句给出判定——不采用本轮结果，整体按
    失败处理待重试（见 ``card_audit_rules.verify_clause_removal_verdicts``
    的说明）。"""
    judged = len(clauses) - len(missing_indexes)
    return {
        "prop_name": prop.name,
        "old_appearance": prop.appearance_canonical,
        "new_appearance": prop.appearance_canonical,
        "appearance_changed": False,
        "removed_clauses": [],
        "removed_aliases": [],
        "feature_shortfall": False,
        "failed": True,
        "fail_reason": (
            f"模型只对 {judged}/{len(clauses)} 条外观子句给出判定"
            f"（缺少编号：{sorted(missing_indexes)}），为安全起见本轮不采用，视为失败重试"
        ),
        "reimaged": False,
        "negation_overrides": [],
        "doubts": [],
    }


def _finalize_audit_result(
    prop: Prop, clauses: list[str], removed_indexes: set[int], removed_clauses: list[dict[str, Any]],
    removed_aliases: list[dict[str, Any]], doubts: list[dict[str, Any]],
) -> dict[str, Any]:
    """子句/别名两次判定都已合并完毕后，拼出最终外观与结果 dict——从
    ``compute_prop_card_audit`` 拆出来，保持两个函数都在单函数行数红线内。"""
    failed = False
    fail_reason: str | None = None
    if clauses and removed_indexes == set(range(1, len(clauses) + 1)):
        new_appearance = prop.appearance_canonical
        failed = True
        fail_reason = "模型判定全部外观子句都该删除，保留原外观不改动，待人工核查"
    elif clauses:
        new_appearance = judge.rebuild_appearance_excluding(prop.appearance_canonical, removed_indexes)
    else:
        new_appearance = prop.appearance_canonical

    new_clause_count = len(judge.split_appearance_clauses(new_appearance))
    feature_shortfall = not failed and 0 < new_clause_count < judge.MIN_APPEARANCE_FEATURES
    appearance_changed = not failed and new_appearance != prop.appearance_canonical
    return {
        "prop_name": prop.name,
        "old_appearance": prop.appearance_canonical,
        "new_appearance": new_appearance,
        "appearance_changed": appearance_changed,
        "removed_clauses": removed_clauses,
        "removed_aliases": removed_aliases,
        "feature_shortfall": feature_shortfall,
        "failed": failed,
        "fail_reason": fail_reason,
        "reimaged": False,
        "doubts": doubts,
    }


async def compute_prop_card_audit(
    prop: Prop, all_props: list[Prop], kept_doubt_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """纯计算（两次独立模型调用 + 代码核验 + 两次取交集才真正删除），不写库、
    不出图——供 ``audit_one_prop_card`` 的真实写入路径与 dry-run 路径共用同
    一份判定逻辑。两次独立判定的取舍见 ``app.props.card_audit_consensus``
    模块 docstring；``kept_doubt_keys`` 是人工已经点过「保留」的存疑键（见
    ``card_audit_store.get_kept_doubt_keys``），dry-run 路径不读决定表，
    始终传空集合（纯计算，供沙箱验证）。"""
    clauses = judge.split_appearance_clauses(prop.appearance_canonical)
    owner_catalog_text = card_audit_rules.catalog_text_for_prompt(prop, all_props)
    raw_a, raw_b = await card_audit_consensus.run_two_independent_clause_judgments(
        prop, clauses, owner_catalog_text,
    )
    other_identifiers = card_audit_rules.other_card_identifiers(prop, all_props)
    clause_merge = card_audit_consensus.merge_clause_judgments(
        len(clauses), raw_a["clauses"], raw_b["clauses"], other_identifiers, clauses,
    )
    if clauses and clause_merge["missing_indexes"]:
        return _partial_coverage_audit_result(prop, clauses, clause_merge["missing_indexes"])
    removed_indexes = clause_merge["removed_indexes"]
    removed_clauses = clause_merge["removed_records"]
    negation_overrides = _apply_negation_group_safety_net(prop.appearance_canonical, removed_indexes, removed_clauses)
    for record in removed_clauses:
        record["text"] = clauses[record["index"] - 1]

    ambiguous_aliases = card_audit_rules.ambiguous_aliases_to_drop(prop, all_props)
    alias_merge = card_audit_consensus.merge_alias_judgments(prop.aliases, raw_a["aliases"], raw_b["aliases"])
    seen_aliases = {record["alias"] for record in ambiguous_aliases}
    removed_aliases = [*ambiguous_aliases, *[r for r in alias_merge["removed"] if r["alias"] not in seen_aliases]]

    doubts = card_audit_consensus.filter_doubts_against_kept_decisions(
        [*clause_merge["doubts"], *alias_merge["doubts"]], kept_doubt_keys,
    )
    result = _finalize_audit_result(prop, clauses, removed_indexes, removed_clauses, removed_aliases, doubts)
    result["negation_overrides"] = negation_overrides
    return result


async def _apply_audit_result(
    conn: sqlite3.Connection, project_id: str, prop: Prop, result: dict[str, Any],
    *, style: str, episode_no: int,
) -> dict[str, Any]:
    """别名删除立即生效（纯数据清理，与出图成败无关）；外观变化须重出图成功
    才写回——失败时旧外观与旧图原封不动（原子性）。"""
    if result["removed_aliases"]:
        _remove_prop_aliases(conn, project_id, prop.name, [r["alias"] for r in result["removed_aliases"]])
    reimaged = False
    if result["appearance_changed"]:
        prompt = image.prop_ref_prompt(style, result["new_appearance"], name=prop.name)
        image_path = await image.generate_prop_reference_image(project_id, prop.name, prompt)
        if image_path:
            _set_prop_appearance_and_image(conn, project_id, prop.name, result["new_appearance"], image_path)
            store.upsert_prop_reference(
                conn, project_id, prop.name, episode_no, appearance=result["new_appearance"],
                image_path=image_path, prompt=prompt, status="ready", qa={},
            )
            conn.commit()
            reimaged = True
        else:
            log.warning(
                "[PROP_CARD_AUDIT_REIMAGE_FAILED] prop=%s 重出图失败，旧外观与旧图保留不变", prop.name,
            )
    return {**result, "reimaged": reimaged}


def _claim_audit_operation(
    conn: Any, *, project_id: str, prop_name: str, rules_version: str,
) -> tuple[dict[str, Any], bool]:
    """抢占成功时返回的行必须带上这一轮的 ``claim_token``（审查发现：没有围栏
    字段时，僵死 running 被重新抢占后原调用仍可能用旧 row_id 把过期结果写
    回——见 ``card_audit_store`` 模块 docstring 与 ``_mark_audit_ready``/
    ``_mark_audit_failed`` 的围栏校验）——调用方必须原样传给后续的写回。"""
    card_audit_store.ensure_tables_on_connection(conn)
    row = card_audit_store.get_audit(conn, project_id=project_id, prop_name=prop_name)
    stamp = now()
    if row and row["rules_version"] == rules_version:
        if row["status"] == "ready":
            return row, False
        if row["status"] == "running" and (stamp - float(row["updated_at"] or 0)) < _AUDIT_RUNNING_STALE_S:
            return row, False
        if row["status"] == "failed" and int(row["attempts"] or 0) >= MAX_AUDIT_ATTEMPTS:
            return row, False
        attempts = int(row["attempts"] or 0) + 1
        token = card_audit_store.update_running(conn, row_id=row["id"], rules_version=rules_version, attempts=attempts, stamp=stamp)
        return {**row, "status": "running", "attempts": attempts, "claim_token": token}, True
    if row:
        # 规则版本已前进（或 row 带的是旧版本结果）：新版本是新的一轮复核，重新计次。
        token = card_audit_store.update_running(conn, row_id=row["id"], rules_version=rules_version, attempts=1, stamp=stamp)
        return {
            **row, "id": row["id"], "status": "running", "attempts": 1,
            "rules_version": rules_version, "claim_token": token,
        }, True
    row_id = new_id("propaudit")
    token = card_audit_store.insert_running(
        conn, row_id=row_id, project_id=project_id, prop_name=prop_name, rules_version=rules_version, stamp=stamp,
    )
    return {
        "id": row_id, "status": "running", "attempts": 1,
        "rules_version": rules_version, "claim_token": token,
    }, True


async def claim_audit(*, project_id: str, prop_name: str, rules_version: str) -> tuple[dict[str, Any], bool]:
    def _operation(conn: Any) -> tuple[dict[str, Any], bool]:
        return _claim_audit_operation(conn, project_id=project_id, prop_name=prop_name, rules_version=rules_version)
    return await db.run_write_transaction(_operation)


async def _mark_audit_ready(*, row_id: str, claim_token: str, result: dict[str, Any]) -> None:
    """``claim_token`` 必须是 ``claim_audit`` 返回行里的那个值——写回时会校验
    这一行此刻仍是同一轮抢占（见 ``card_audit_store.update_ready``）；如果本轮
    已被重新抢占（``claim_token`` 已被改写），写回不生效，只记日志，不覆盖
    新一轮的结果（CLAUDE.md「不要给以后的生成埋雷」：静默覆盖比不写更危险）。"""
    def _operation(conn: Any) -> bool:
        card_audit_store.ensure_tables_on_connection(conn)
        return card_audit_store.update_ready(
            conn, row_id=row_id, old_appearance=result["old_appearance"], new_appearance=result["new_appearance"],
            removed_clauses=result["removed_clauses"], removed_aliases=result["removed_aliases"],
            reimaged=result["reimaged"], feature_shortfall=result["feature_shortfall"], stamp=now(),
            claim_token=claim_token, doubts=result.get("doubts", []),
        )
    applied = await db.run_write_transaction(_operation)
    if not applied:
        log.warning(
            "[PROP_CARD_AUDIT_STALE_WRITE_DISCARDED] row_id=%s prop=%s "
            "本轮已被重新抢占，写回已丢弃，不覆盖新一轮结果", row_id, result.get("prop_name"),
        )


async def _mark_audit_failed(*, row_id: str, claim_token: str, error: str) -> None:
    """围栏语义同 ``_mark_audit_ready``。"""
    def _operation(conn: Any) -> bool:
        card_audit_store.ensure_tables_on_connection(conn)
        return card_audit_store.update_failed(conn, row_id=row_id, error=error, stamp=now(), claim_token=claim_token)
    applied = await db.run_write_transaction(_operation)
    if not applied:
        log.warning(
            "[PROP_CARD_AUDIT_STALE_WRITE_DISCARDED] row_id=%s 本轮已被重新抢占，失败写回已丢弃", row_id,
        )


async def audit_one_prop_card(
    project_id: str, prop_name: str, *, dry_run: bool = False,
) -> dict[str, Any]:
    """对一张道具卡按现行规则复核；``dry_run=True`` 时只跑模型判定 + 代码
    核验，不抢占 ``card_audit_store``、不写世界书、不重新出图（供 B 沙箱做
    真实模型验证，见模块 docstring）。"""
    conn = get_conn()
    bible = _load_bible(conn, project_id)
    if bible is None:
        raise ValueError(f"项目不存在或人物谱未初始化：{project_id}")
    prop = next((p for p in bible.props if p.name == prop_name), None)
    if prop is None:
        raise ValueError(f"道具不存在：{prop_name}")
    if dry_run:
        return await compute_prop_card_audit(prop, bible.props)

    rules_version = judge.PROP_CARD_RULES_VERSION
    row, should_run = await claim_audit(project_id=project_id, prop_name=prop_name, rules_version=rules_version)
    if not should_run:
        return {"prop_name": prop_name, "status": row["status"], "skipped": True}
    kept_doubt_keys = card_audit_store.get_kept_doubt_keys(
        conn, project_id=project_id, prop_name=prop_name, rules_version=rules_version,
    )
    try:
        result = await compute_prop_card_audit(prop, bible.props, kept_doubt_keys)
        applied = await _apply_audit_result(
            conn, project_id, prop, result,
            style=bible.world.visual_style_canonical, episode_no=prop.first_episode_no or 1,
        )
    except Exception as exc:  # noqa: BLE001 单张卡复核失败不影响其它卡
        await _mark_audit_failed(row_id=row["id"], claim_token=row["claim_token"], error=str(exc))
        return {"prop_name": prop_name, "status": "failed", "error": str(exc)}
    if applied["failed"]:
        await _mark_audit_failed(
            row_id=row["id"], claim_token=row["claim_token"], error=applied["fail_reason"] or "复核判定全删，未采用",
        )
        return {**applied, "status": "failed"}
    await _mark_audit_ready(row_id=row["id"], claim_token=row["claim_token"], result=applied)
    return {**applied, "status": "ready"}


async def _audit_one_quietly(project_id: str, prop_name: str) -> dict[str, Any]:
    try:
        return await audit_one_prop_card(project_id, prop_name, dry_run=False)
    except Exception as exc:  # noqa: BLE001 - 整项目批量复核时单张卡失败不影响其它卡
        log.exception("[PROP_CARD_AUDIT_FAILED] project=%s prop=%s", project_id, prop_name)
        return {"prop_name": prop_name, "status": "failed", "error": str(exc)}


async def audit_project_prop_cards(project_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    """对项目全部道具卡发起复核（手动入口/dry-run 脚本用）；``dry_run=True``
    时每张卡都走纯计算路径，不写库不出图。并发用信号量限流，与
    ``card_pending_ensure.ensure_storyboard_prop_cards`` 同一取舍。"""
    conn = get_conn()
    bible = _load_bible(conn, project_id)
    if bible is None:
        raise ValueError(f"项目不存在或人物谱未初始化：{project_id}")
    if dry_run:
        results = await asyncio.gather(*[
            compute_prop_card_audit(prop, bible.props) for prop in bible.props
        ])
        return {"project_id": project_id, "dry_run": True, "results": list(results)}
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_AUDITS)

    async def _run(name: str) -> dict[str, Any]:
        async with semaphore:
            return await _audit_one_quietly(project_id, name)

    results = await asyncio.gather(*[_run(prop.name) for prop in bible.props])
    return {"project_id": project_id, "dry_run": False, "results": list(results)}


async def audit_specific_prop_cards(project_id: str, prop_names: list[str]) -> list[dict[str, Any]]:
    """等待一批具名道具卡复核完成（供 ``app.props.card_audit_ensure`` 的
    连播台钩子使用——与 ``card_pending_ensure.ensure_storyboard_prop_cards``
    同一"调用方等它跑完才继续派发视频"的取舍，不是后台 fire-and-forget）。"""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_AUDITS)

    async def _run(name: str) -> dict[str, Any]:
        async with semaphore:
            return await _audit_one_quietly(project_id, name)

    return list(await asyncio.gather(*[_run(name) for name in prop_names]))


def launch_background_audit(*, project_id: str, prop_names: list[str]) -> list["asyncio.Task[Any]"]:
    """触发点①②共用：后台发起一批道具卡复核，不阻塞调用方——复核本身是一次
    模型调用 + 可能的重出图，不该让建卡成功/生成闸门的调用方同步等它（与
    ``card_pending_ensure.launch_background_ensure`` 同一取舍）。并发用
    ``_MAX_CONCURRENT_AUDITS`` 信号量限流，与同模块 ``audit_project_prop_
    cards``/``audit_specific_prop_cards`` 同一口径（审查发现：此前这里是
    裸 ``create_task`` 循环，规则版本升级后一个项目几十张卡同时落后/用户点
    一次「整项目复核」会瞬间并发发起同等数量的模型调用，且放大 CAS 僵死窗口
    被触发的概率）。"""
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_AUDITS)

    async def _run(name: str) -> dict[str, Any]:
        async with semaphore:
            return await _audit_one_quietly(project_id, name)

    tasks = []
    for name in prop_names:
        task = asyncio.create_task(_run(name))
        _BACKGROUND_AUDIT_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_AUDIT_TASKS.discard)
        tasks.append(task)
    return tasks


def launch_background_audit_for_project(project_id: str) -> list[str]:
    """触发点③（手动入口 POST /props/audit）：整项目全部道具卡后台复核，
    立即返回被受理的道具名列表；项目不存在/人物谱未初始化时抛
    ``ValueError``（路由层转 409）。"""
    conn = get_conn()
    bible = _load_bible(conn, project_id)
    if bible is None:
        raise ValueError(f"项目不存在或人物谱未初始化：{project_id}")
    prop_names = [prop.name for prop in bible.props]
    launch_background_audit(project_id=project_id, prop_names=prop_names)
    return prop_names


def audits_for_project(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    """道具库手动入口 GET 结果用：返回该项目全部复核记录（含 running/failed）。"""
    rows = card_audit_store.list_audits(conn, project_id=project_id)
    out = []
    for row in rows:
        out.append({
            "prop_name": row["prop_name"], "rules_version": row["rules_version"], "status": row["status"],
            "old_appearance": row["old_appearance"], "new_appearance": row["new_appearance"],
            "removed_clauses": json.loads(row["removed_clauses_json"]) if row["removed_clauses_json"] else [],
            "removed_aliases": json.loads(row["removed_aliases_json"]) if row["removed_aliases_json"] else [],
            "reimaged": bool(row["reimaged"]), "feature_shortfall": bool(row["feature_shortfall"]),
            "doubts": json.loads(row["doubts_json"]) if row["doubts_json"] else [],
            "error": row["error"],
        })
    return out
