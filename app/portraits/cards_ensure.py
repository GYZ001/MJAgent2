"""按正文 ensure_cards_for_text：发现候选并保证已具备人物卡的对外入口。
"""

from __future__ import annotations


from collections.abc import Callable

from app.db import get_conn
from app.errors import ContentGenerationError, code_ref
from app.portraits.card_owner import bible_known_labels
from app.schemas import Bible, character_is_portrait_eligible

from ._db_probe import _has_column
from ._identity_tokens import _identity_disambiguating_suffix
from .cards import (
    _candidate_requires_identity_card,
    ensure_character_card,
)
from .constants import (
    CURRENT_IDENTITY_LITERAL_PROVENANCE,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
)
from .current_ref import current_portrait_ref
from .discovery import discover_character_candidates
from .discovery_fragments import _card_lock, _future_chapter_context
from .discovery_resample import (
    _named_candidate_materialization_compatible,
    screenplay_identity_scope_fingerprint,
)
from .evidence_receipt import _validate_current_identity_receipt_bundle
from .portrait_io import _generate_discovered_character_portrait
from .resolution_store import load_screenplay_character_resolutions
from .structural_coverage import (
    _identity_resolution,
    screenplay_identity_resolution_is_current_for_source,
)


async def _ensure_known_character_portrait(
    conn, project_id: str, name: str, episode_no: int, bible: Bible,
    *, generate_portraits: bool, write_guard: Callable[[], None] | None,
) -> dict:
    """已在人物谱里的角色，映射进本集时是否已有可用定妆照——判据与
    ``app.portraits.current_ref.current_portrait_ref`` 完全同一张表、同一个
    查询形状（该模块 docstring 明确禁止另写一条相似查询），额外校验落盘
    文件是否存在，不会把"卡还在但图已丢"误判成"有图"。

    真实事故链：角色改成映射时逐个建卡，出图可能因供应商错误/内容审核/
    并发中断失败；旧代码此时只做称谓归一，永远不再检查也不再补图，分镜台
    永久拿不到这个角色的素材。这里补上"缺就补"，并让补图失败产生可见信号
    （返回 status="failed"，调用方计入 warnings），不静默跳过。

    ``generate_portraits=False`` 时不发起任何供应商调用（与本函数同级的
    ``unknown_by_name`` 分支同一约定），只报告"缺图待补"——下一次以
    ``generate_portraits=True`` 调用本函数的入口（映射台资产解析，
    ``app.production.prep_pack.discovery._discover_new_characters``）会真正
    补上。
    """
    if current_portrait_ref(project_id, name, episode_no) is not None:
        return {"status": "has_portrait", "name": name}
    character = next((c for c in bible.characters if c.name == name), None)
    if character is None or not character_is_portrait_eligible(character):
        return {"status": "skipped", "name": name, "reason": "not portrait eligible"}
    if not generate_portraits:
        return {"status": "deferred", "name": name}
    lock = await _card_lock(project_id, name)
    async with lock:
        if write_guard:
            write_guard()
        if current_portrait_ref(project_id, name, episode_no) is not None:
            return {"status": "has_portrait", "name": name}  # 并发已补
        latest = conn.execute(
            "SELECT bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            portrait = await _generate_discovered_character_portrait(
                project_id,
                name,
                bible.world.visual_style_canonical,
                character.appearance_canonical,
                ep_start=episode_no,
                bible_version=int(latest["bible_version"] or 0) if latest else 0,
            )
        except Exception as exc:  # noqa: BLE001 -- 补图失败必须可见，不吞
            public = code_ref(
                exc,
                action="backfill_known_character_portrait",
                context={"project_id": project_id, "name": name, "episode_no": episode_no},
            )
            return {"status": "failed", "name": name, "portrait_error": public}
    return {"status": "backfilled", "name": name, **portrait}


async def _backfill_known_character_portraits(
    conn, project_id: str, episode_no: int, bible: Bible,
    known_named_candidates: list[dict],
    *, generate_portraits: bool, write_guard: Callable[[], None] | None,
) -> tuple[list[dict], list[str]]:
    """``known_named_candidates`` 去重后逐个补图检查——拆出为独立函数，不塞进
    ``ensure_cards_for_text`` 自己的函数体（该函数已经在
    ``app/FILE_CONVENTIONS.toml`` 的 function_lines 棘轮基线上，新增逻辑只能
    另起函数，不能继续加长它）。返回 ``(portraits_backfilled, warning_lines)``
    供调用方合并进自己的 ``warnings`` 列表。
    """
    known_names_needing_check = dict.fromkeys(
        str(item.get("name") or "").strip() for item in known_named_candidates
    )
    backfilled: list[dict] = []
    warning_lines: list[str] = []
    for name in known_names_needing_check:
        if not name:
            continue
        outcome = await _ensure_known_character_portrait(
            conn, project_id, name, episode_no, bible,
            generate_portraits=generate_portraits, write_guard=write_guard,
        )
        status = outcome["status"]
        if status == "backfilled":
            backfilled.append(outcome)
        elif status == "failed":
            warning_lines.append(
                f"{name}：人物卡已存在，但本集定妆照缺失且自动补图失败"
                f"{outcome.get('portrait_error') or ''}"
            )
        elif status == "deferred":
            warning_lines.append(
                f"{name}：人物卡已存在但本集缺少可用定妆照，"
                "将在资产准备阶段自动补齐"
            )
    return backfilled, warning_lines


def _reference_identity_resolutions(mentioned_only_candidates: list[dict]) -> list[dict]:
    """A stable referenced identity still needs an authority even when it never
    appears visually and therefore must not create a character card."""
    out: list[dict] = []
    for item in mentioned_only_candidates:
        source_label = str(item.get("source_label") or item.get("name") or "").strip()
        canonical_name = str(item.get("name") or source_label).strip()
        if source_label and canonical_name:
            out.append(_identity_resolution(
                item, canonical_name, "reference_identity",
                reason="来源或蓝图引用该稳定身份，但当前集不需要人物卡或视觉资产",
            ))
    return out


def _future_identity_resolutions(known_named_candidates: list[dict]) -> list[dict]:
    """本集用了新称谓，但人物谱已有该角色——记一条决议，供剧本消歧表复用。"""
    out: list[dict] = []
    for item in known_named_candidates:
        source_label = str(item.get("source_label") or "").strip()
        canonical_name = str(item.get("name") or "").strip()
        if source_label and canonical_name and source_label != canonical_name:
            out.append(_identity_resolution(
                item, canonical_name, "future_identity",
                reason="后续章节已确认该称谓属于人物谱已有角色",
            ))
    return out


async def ensure_cards_for_text(
    project_id: str,
    episode_no: int,
    source_text: str,
    bible: Bible,
    *,
    draft_text: str = "",
    generate_portraits: bool = True,
    _precomputed_candidates: list[dict] | None = None,
    write_guard: Callable[[], None] | None = None,
) -> dict:
    """发现并补人物卡；同时输出供剧本使用的姓名消歧表。"""
    conn = get_conn()
    episode_row = (
        conn.execute(
            "SELECT id FROM episodes WHERE project_id=? AND episode_no=?",
            (project_id, episode_no),
        ).fetchone()
        if _has_column(conn, "episodes", "id")
        else None
    )
    existing_resolutions = (
        load_screenplay_character_resolutions(conn, episode_row["id"])
        if episode_row
        else []
    )
    identity_scope_fingerprint = screenplay_identity_scope_fingerprint(
        episode_no, source_text
    )
    # Automatic decisions are inputs to the next discovery pass only when all
    # three authority fences match the current owned source.  Older coverage,
    # future-wire or source epochs must be re-adjudicated before influencing a
    # the current strict prompt; explicitly durable manual/Bible decisions survive.
    existing_resolutions = [
        item for item in existing_resolutions
        if screenplay_identity_resolution_is_current_for_source(
            item,
            episode_no=episode_no,
            source_text=source_text,
        )
    ]
    future_text, future_label = _future_chapter_context(conn, project_id, episode_no)
    candidates = (
        [dict(item) for item in _precomputed_candidates]
        if _precomputed_candidates is not None
        else await discover_character_candidates(
            source_text, bible, episode_no, draft_text=draft_text,
            future_text=future_text, future_label=future_label,
            existing_resolutions=existing_resolutions,
            scope_id=str(episode_row["id"]) if episode_row else None,
            project_id=project_id,
        )
    )
    candidates = [
        {
            **item,
            "identity_scope_fingerprint": str(
                item.get("identity_scope_fingerprint")
                or identity_scope_fingerprint
            ),
        }
        for item in candidates
        if isinstance(item, dict)
    ]
    for item in candidates:
        provenance = str(
            item.get("source_label_provenance") or ""
        ).strip()
        has_bundle_fields = bool(
            item.get("source_evidence_receipt") is not None
            or item.get("source_evidence_receipts") is not None
        )
        if has_bundle_fields or provenance in {
            CURRENT_IDENTITY_LITERAL_PROVENANCE,
            CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
        }:
            bundle = _validate_current_identity_receipt_bundle(
                item,
                source_text=source_text,
                draft_text=draft_text,
            )
            if bundle is None:
                raise ContentGenerationError(
                    "current identity candidate 缺少 v2 evidence receipt bundle"
                )
    if write_guard:
        write_guard()
    known = bible_known_labels(bible)
    unknown_by_name: dict[str, list[dict]] = {}
    functional_candidates: list[dict] = []
    known_named_candidates: list[dict] = []
    mentioned_only_candidates: list[dict] = []
    errors: list[str] = []
    for item in candidates:
        if item.get("identity_kind") == "functional":
            functional_candidates.append(item)
            continue
        name = str(item.get("name") or "").strip()
        if not _named_candidate_materialization_compatible(item):
            if item.get("kind") == "mentioned" and name and name not in known:
                mentioned_only_candidates.append(item)
            else:
                errors.append(
                    "named authority 不可直接物化人物卡："
                    f"{str(item.get('source_label') or name).strip()}->{name}"
                )
            continue
        if name in known:
            known_named_candidates.append(item)
        elif _candidate_requires_identity_card(item, known):
            unknown_by_name.setdefault(name, []).append(item)
        elif name:
            mentioned_only_candidates.append(item)
    added: list[dict] = []
    provisional_characters: list[dict] = []
    skipped: list[dict] = [
        {
            "status": "mentioned_only",
            "name": str(item.get("name") or "").strip(),
            "reason": "本集仅提及且未出镜/开口，不创建人物卡",
        }
        for item in mentioned_only_candidates
    ]
    warnings: list[str] = []
    resolutions: list[dict] = []
    assigned_extra_names: dict[str, str] = {}
    assigned_identity_groups: dict[str, str] = {}
    # 真实第20轮 EP4 回归 ERR-20260824-407c9b 结构性排查命中：identity_group
    # 已经是可靠的按人区分键，但当两个不同的 identity_group 都退回同一个
    # 裸 source_label 当 route_name（比如两个不同的"外宗弟子"）时，两者的
    # route_name 字符串会变成完全相同的值——route_name 是这个函数唯一往
    # 外传的东西，下游（app.production.prep_pack 的 functional_extras，按
    # 这个字符串当 key 聚合 event_ids）拿到手就已经分不清是谁了，会把两个
    # 人的出场事件悄悄合并进同一条群演记录。用确定性序号区分（"外宗弟子
    # （乙）"），不是"路人甲/乙/丙"式的泛化替换——原有的功能性描述原样
    # 保留，只在真的撞车时追加后缀（见函数上方"不得通过改成路人甲/乙/丙
    # 来...抹掉来源身份"的既有原则，这里遵循同一原则：只加后缀，不换描述）。
    _route_name_first_owner: dict[str, str] = {}
    _route_name_collisions: dict[str, int] = {}

    resolutions.extend(_reference_identity_resolutions(mentioned_only_candidates))
    resolutions.extend(_future_identity_resolutions(known_named_candidates))

    # 已在人物谱里的角色映射进本集时不能只做称谓归一：卡可能是之前哪一集
    # 建成后出图失败留下的（供应商错误/内容审核/并发中断），如果只写决议、
    # 不检查有没有图，分镜台就永久拿不到这个角色的素材（见
    # _backfill_known_character_portraits / _ensure_known_character_portrait）。
    portraits_backfilled, portrait_backfill_warnings = await _backfill_known_character_portraits(
        conn, project_id, episode_no, bible, known_named_candidates,
        generate_portraits=generate_portraits, write_guard=write_guard,
    )
    warnings.extend(portrait_backfill_warnings)

    # 功能身份保留原文稳定称谓。是否需要人物卡与是否具备真名是两件事，
    # 不得通过改成“路人甲/乙/丙”来降低角色重要性或抹掉来源身份。
    for item in functional_candidates:
        source_label = str(item.get("source_label") or item.get("name") or "").strip()
        identity_group = str(
            item.get("identity_group") or f"source:{source_label}"
        ).strip()
        route_name = str(item.get("existing_route_name") or "").strip()
        if not route_name:
            route_name = assigned_identity_groups.get(identity_group, "")
        if not route_name:
            first_owner = _route_name_first_owner.setdefault(
                source_label, identity_group,
            )
            if first_owner == identity_group:
                route_name = source_label
            else:
                _route_name_collisions[source_label] = (
                    _route_name_collisions.get(source_label, 1) + 1
                )
                route_name = (
                    f"{source_label}"
                    f"（{_identity_disambiguating_suffix(_route_name_collisions[source_label])}）"
                )
        assigned_identity_groups[identity_group] = route_name
        assigned_extra_names[source_label] = route_name
        resolutions.append(_identity_resolution(
            item,
            route_name,
            "functional_identity",
            reason="模型依据当前来源确认该实体为本集功能身份",
        ))

    for name, items in unknown_by_name.items():
        ensure_kwargs = {
            "generate_portrait": generate_portraits,
            "require_identity_card": True,
            "identity_source_labels": [str(item.get("source_label") or "") for item in items],
        }
        if write_guard is not None:
            ensure_kwargs["write_guard"] = write_guard
        result = await ensure_character_card(
            project_id,
            name,
            episode_no,
            **ensure_kwargs,
        )
        if result.get("status") == "added":
            added.append(result)
            if not result.get("has_portrait"):
                warnings.append(
                    f"{name}：人物卡已添加，定妆资产将在独立资产环节补齐"
                    if result.get("portrait_deferred")
                    else f"{name}：人物卡已添加，定妆照生成失败，需稍后重试"
                )
        elif result.get("status") == "pending_review":
            # 兼容旧实现返回值；新流程不应再产生用户待审项。
            errors.append(f"{name}：自动建卡流程未完成")
        elif result.get("status") in {
            "skipped_minor", "exists", "skipped_not_person",
        }:
            skipped.append(result)
            if result.get("status") == "skipped_not_person":
                # 非人（宗门、器物）以及非故事角色（作者笔名出现在章末旁白）
                # 本来就不该进人物谱，这是正常终态而不是错误。但它们不能继续
                # 保持 named：结构人物 coverage 会要求每个具名身份都有已物化的
                # 人物卡，于是"正确地拒绝建卡"反而让整集硬失败（生产上 EP3 卡在
                # 「耳根」——作者笔名）。降级为功能身份，让两边重新一致。
                for item in items:
                    item["identity_kind"] = "functional"
                    item["authority_id"] = ""
                    item["materialization_compatible"] = False
            # 非人（宗门/器物/地点）本来就不该进人物谱，这是正常终态而不是错误。
            if result.get("status") == "skipped_minor":
                # identity_kind=named 已由身份模型给出可靠同一性证据。
                # 不能再用“戏份不足”把真名降回路人；卡片不完整就留在剧本闸门修复。
                errors.append(
                    f"{name}：真名已确认，但人物卡未完成："
                    f"{result.get('reason') or 'unknown reason'}"
                )
        else:
            errors.append(f"{name}：{result.get('reason') or result.get('status') or '补卡失败'}")

        if result.get("status") in {"added", "exists"}:
            # "exists" 现在带回的是归属者的规范名（app.portraits.card_owner），
            # 不是被查询的标签：以别名"小胖子"命中的归属者是"李富贵"，决议必须
            # 指向人物谱里真实存在的角色，不能沿用查询标签本身。
            canonical_name = str(result.get("name") or name).strip()
            for item in items:
                source_label = str(item.get("source_label") or name).strip()
                if source_label != canonical_name:
                    resolutions.append(_identity_resolution(
                        item,
                        canonical_name,
                        "future_identity",
                        reason="后续章节已确认该称谓的稳定真名",
                    ))
    return {
        "checked": len(unknown_by_name),
        "candidates": candidates,
        "added": added,
        "provisional_characters": provisional_characters,
        "skipped": skipped,
        "resolutions": resolutions,
        "future_context_label": future_label,
        "errors": errors,
        "warnings": warnings,
        "portraits_backfilled": portraits_backfilled,
    }

