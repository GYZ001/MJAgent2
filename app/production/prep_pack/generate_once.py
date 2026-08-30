"""One full generation attempt: coverage-ledger/scene-coverage/appellation-map
construction and _generate_prep_pack_once, the orchestrator that chunks the
episode, calls extraction, and resolves assets into the final payload.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from app.db import get_conn
from app.source_excerpt import (
    chapter_title_segment_indexes,
    index_source_segments,
)
from app.validators import assert_prep_pack_coverage_complete
from typing import Any

from .chunk_extraction import (
    _extract_chunk,
    _run_async_step,
)
from .chunking import (
    _chunk_segments,
    _known_character_names,
    _known_scene_names,
    _prep_pack_chapter_titles,
    _prep_pack_gate_segment_indexes,
)
from .contracts import (
    PREP_PACK_VERSION,
    PrepPackGateError,
)
from .provenance import _prep_pack_verify_manifest_provenance
from .resolve_assets import _resolve_assets
from .true_name import _prep_pack_gather_concurrent


def _prep_pack_build_coverage_ledger(
    total_segments: int,
    delivered_indexes: set[int],
    paratext_indexes: set[int],
) -> tuple[dict[str, Any], list[int]]:
    all_indexes = set(range(1, total_segments + 1))
    delivered = delivered_indexes & all_indexes
    paratext_claims = paratext_indexes & all_indexes
    rejected_paratext_claims = sorted(paratext_claims & delivered)
    paratext = paratext_claims - delivered
    retained = all_indexes - delivered - paratext
    uncovered = all_indexes - delivered - paratext - retained
    ledger = {
        "total_segments": total_segments,
        "delivered": sorted(delivered),
        "merged": [],
        "retained_as_context": sorted(retained),
        "proven_duplicates": [],
        "paratext": sorted(paratext),
        "uncovered": sorted(uncovered),
    }
    return ledger, rejected_paratext_claims


# 2.0.3 新增（见 PREP_PACK_VERSION 上方 2.0.3 大注释的完整案情）：场景专项
# 覆盖账，跟上面五账并列、互不干扰的独立视角——五账的 delivered 只要角色/
# 场景/道具任一维度覆盖到某段就算 delivered，天然看不见"场景这一个维度
# 单独漏覆盖、角色/道具仍覆盖"的情形，这正是 EP4 真实回归暴露的缺陷：54
# 段章节里 scenes 只覆盖到 20 段，21~54 段因为角色提及（主角孟浩本人）
# 仍然贯穿在场，五账的 delivered/uncovered 完全看不出场景那部分已经断供，
# 这个缺口一路悄悄传导到分镜台三态告警才第一次现形。本账目让它在映射台
# 自己的产出里就可见。
#
# 不做的事（刻意）：不拦截、不重新定义"delivered"的既有语义、不往
# assert_prep_pack_coverage_complete 那道门禁塞新的阻断条件（该门禁签名
# 只读 ledger["uncovered"]，本账目是全新键，结构上不可能触发它）、不对
# scene_uncovered 做任何解释性判断——scene_uncovered 非空可能是真的漏报，
# 也可能是这些段落本来就没有场景描写（纯心理活动、纯对白），两种情况在
# 数据层面无法区分，交付判据仍然是逐条对原文，这里只负责让分母/分子可见，
# 不越权下结论。也不做"没覆盖就借用上一个场景的 segment_indexes 顺延"这
# 类兜底——那是编造场景归属，比空着更危险，比空着更难被发现是假的。
def _prep_pack_scene_coverage_account(
    total_segments: int,
    scene_delivered_indexes: set[int],
    paratext_indexes: set[int],
) -> dict[str, Any]:
    all_indexes = set(range(1, total_segments + 1))
    scene_delivered = scene_delivered_indexes & all_indexes
    paratext = paratext_indexes & all_indexes
    scene_uncovered = all_indexes - scene_delivered - paratext
    return {
        "total_segments": total_segments,
        "scene_delivered": sorted(scene_delivered),
        "scene_uncovered": sorted(scene_uncovered),
    }


# 2.0.0 新增，2.0.1 重做真源（协调方复核确认的 bug，见下段"2.0.1 根因"）：
# appellation_map 把每条原文里的模糊称谓摊平成逐段的 (raw_mention,
# segment_index) -> (identity_id, canonical_appellation) 映射表。
#
# 2.0.1 根因（测试缺口补齐过程中发现，协调方独立复现确认）：2.0.0 最初实现
# 拿 characters[].aliases 当"这个身份在本集被叫过的全部说法"的真源反查
# character_mentions——但 aliases 只登记逐字出现于原文的称谓（_resolve_
# assets 内 came_via_resolution and literal_evidence 双重门槛，见
# test_composite_description_resolved_via_discovery_bypasses_literal_gate：
# "穿杂役衫的魁梧大汉"经消歧正确解析到赵武刚、真实发布进 asset_manifest.
# characters，但明确不进 aliases）。aliases 担保的是"能不能安全进跨集别名
# 注册表而不污染它"，不是"这条提及有没有解析出身份"——拿前者的真源冒充
# 后者用，漏掉的恰好是模糊/描述性称谓，而那正是这张表存在的全部理由（"那
# 少年""小胖子""李管事"这类）。
#
# 修法：appellation_map 不再对 characters[]/character_mentions 做事后
# 反查，直接消费 _resolve_assets 在解析过程中就已经算出的结论——每条
# 提及在 _pass() 里真正解析到 portrait_id、通过称谓证据闸的那一刻，就地
# 记一行（见 _resolve_assets 内 character_appellation_rows 与其
# docstring 的 ``appellation_resolutions`` 出参说明），identity_id/
# canonical_appellation 直接读自它自己刚写入的 manifest entry——跟
# asset_manifest.characters[] 发布的是同一个字典对象，结构上保证两处不会
# 各说各话，不是靠这里再校验一遍。aliases 的既有语义（跨集别名注册表的
# 保护门槛）完全不受影响，未解析到身份的提及（落 functional_extras 的
# 那些）在 _pass() 里从未走到记录这一步，天然不出现在这张表里。
def _prep_pack_build_appellation_map(
    character_appellation_resolutions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for resolution in character_appellation_resolutions:
        raw_mention = str(resolution.get("raw_mention") or "").strip()
        identity_id = str(resolution.get("identity_id") or "")
        canonical_appellation = str(resolution.get("canonical_appellation") or "")
        if not raw_mention or not identity_id or not canonical_appellation:
            continue
        for segment_index in resolution.get("segment_indexes") or []:
            rows.append({
                "raw_mention": raw_mention,
                "segment_index": int(segment_index),
                "identity_id": identity_id,
                "canonical_appellation": canonical_appellation,
            })
    return rows


async def _generate_prep_pack_once(
    *,
    episode_id: str,
    episode_no: int,
    project_id: str,
    chapter_indexes: list[int],
    source_text: str,
    run_id: str | None,
    attempt_hint: str,
) -> tuple[
    dict[str, Any], list[int], list[dict[str, Any]],
    list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None,
]:
    conn = get_conn()
    segments = index_source_segments(source_text)
    chunks = _chunk_segments(segments)
    known_characters = _known_character_names(conn, project_id, episode_no)
    known_scenes = _known_scene_names(conn, project_id, episode_no)
    # 1.9.0 (kept in 2.0.0, see PREP_PACK_VERSION's 1.9.0 note above):
    # DB-anchored chapter titles for this episode's own chapters -- fed to
    # both _extract_chunk (prompt injection, told to the model as an
    # already-decided fact) and _prep_pack_build_coverage_ledger (the
    # actual deterministic paratext account). Chapters with no DB title
    # contribute nothing here.
    chapter_titles = _prep_pack_chapter_titles(conn, project_id, chapter_indexes)
    deterministic_title_indexes = chapter_title_segment_indexes(segments, chapter_titles)

    # paratext（2.0.4，见 PREP_PACK_VERSION 上方大注释 +
    # logs/paratext_single_source_plan.md）：按章持久化偏移，翻译到本集
    # source_text 坐标——不再让模型每个 chunk 自报一遍。fail-closed：
    # 重建的拼接结果对不上传入的 source_text 时（理论上不该发生，
    # _episode_chapters/_episode_source_blocks 与 app.domain.common.
    # _episode_source_text 是同一份实现）放弃这次投影，退回"没有 paratext
    # 信号"，不猜、不强行平移出可能错位的偏移。
    from app.domain.common import _episode_chapters, _episode_source_blocks
    from app.source_paratext import (
        chapter_paratext_offsets,
        paratext_segment_indexes,
        remove_offsets,
    )

    paratext_regions: list[tuple[int, int]] = []
    paratext_chapter_rows = _episode_chapters(
        conn, {"source_chapters": chapter_indexes, "project_id": project_id},
    )
    rebuilt_text, content_offsets = _episode_source_blocks(paratext_chapter_rows)
    if rebuilt_text == source_text:
        paratext_results = await _prep_pack_gather_concurrent([
            chapter_paratext_offsets(
                conn, chapter_row,
                operation_id=f"episode_prep_pack.paratext:{chapter_row['id']}",
            )
            for chapter_row in paratext_chapter_rows
        ])
        for (regions, _cache_hit), content_start in zip(
            paratext_results, content_offsets, strict=True,
        ):
            paratext_regions.extend(
                (content_start + start, content_start + end) for start, end in regions
            )
    deterministic_paratext_segments = paratext_segment_indexes(segments, paratext_regions)
    # _discover_new_characters 用来构造发现输入的"净化后全文"——跟世界书
    # 消费的是同一份持久化偏移，谁先算过这一章，谁就替对方省下一次模型调用。
    discovery_text = remove_offsets(source_text, paratext_regions)

    character_mentions: list[dict[str, Any]] = []
    scene_mentions: list[dict[str, Any]] = []
    prop_mentions: list[dict[str, Any]] = []

    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_global_indexes = {index for index, _segment in chunk}
        chunk_by_index = {index: segment for index, segment in chunk}
        response = await _extract_chunk(
            episode_id=episode_id,
            episode_no=episode_no,
            chunk_index=chunk_index,
            chunk=chunk,
            known_characters=known_characters,
            known_scenes=known_scenes,
            attempt_hint=attempt_hint,
            run_id=run_id,
            confirmed_title_indexes=deterministic_title_indexes,
        )
        for mention in response.characters:
            valid_indexes = _prep_pack_gate_segment_indexes(
                mention.display_name, mention.segment_indexes,
                chunk_global_indexes, chunk_by_index,
            )
            if not valid_indexes:
                continue
            character_mentions.append({
                "display_name": mention.display_name.strip(),
                "suspected_true_name": mention.suspected_true_name,
                "segment_indexes": valid_indexes,
            })
        for mention in response.scenes:
            valid_indexes = _prep_pack_gate_segment_indexes(
                mention.display_name, mention.segment_indexes,
                chunk_global_indexes, chunk_by_index,
            )
            if not valid_indexes:
                continue
            scene_mentions.append({
                "display_name": mention.display_name.strip(),
                "suspected_true_name": mention.suspected_true_name,
                "segment_indexes": valid_indexes,
                # 2.0.2：该提及自己申报的逐字引文，见 _ModelSceneMention.quote
                # 上方注释与 PREP_PACK_VERSION 上方 2.0.2 大注释。不做结构闸
                # （不要求落在 valid_indexes 范围内）——它本来就要在下游经
                # _prep_pack_local_text_anchor 全书逐字复核，跟 canonical_
                # scene_name/name 两个既有候选走的是同一条核验路径，不重复
                # 造一遍。
                "quote": mention.quote.strip(),
            })
        for mention in response.props:
            valid_indexes = _prep_pack_gate_segment_indexes(
                mention.label, mention.segment_indexes,
                chunk_global_indexes, chunk_by_index,
            )
            if not valid_indexes:
                continue
            prop_mentions.append({
                "label": mention.label.strip(),
                "description": mention.description.strip(),
                "segment_indexes": valid_indexes,
            })

    if not character_mentions and not scene_mentions and not prop_mentions:
        raise PrepPackGateError("本集未发现任何人物/场景/道具", had_events=False)

    # 角色单项退化的可见性信号（第31轮真实回归 EP7，ep_621d93ac1231；不
    # 拦截，只留痕）：上面这道门禁是 OR 判据——character_mentions/scene_
    # mentions/prop_mentions 任一非空就放行，天然覆盖不到"角色这一项单独
    # 退化为零、其它维度仍有实质内容"的情形。真实事故正是这种：本集主角
    # 在原文单块 45 段内出场约 43 次，chunk 抽取的原始响应第一次调用本已
    # 正确报出该角色，但那次调用所在的 run 中途被打断，同一 run_id 重新
    # 整体起跑后，chunk 抽取的原始 JSON 结构中途缺了一段，本地格式修复
    # candidate 被 app.harness.model_gateway._latest_json_authority_root
    # 误判成末尾一个只含 scenes 的孤立片段（详见该函数与
    # ERR-20260824-7ab7cb 的既有说明），格式修复调用据此"忠实"地只交回
    # scenes、把 characters/props 一并修没了——scene_mentions 非空使上面
    # 那道门禁直接放行，角色维度归零这件事从此再没有任何信号能被看见。
    # 判据纯数据推导，不认名字：known_characters 非空说明本项目已有登记
    # 角色谱、这一集理应有角色可映射；scene_mentions/prop_mentions 任一
    # 非空说明这段原文确有实质内容被成功抽取，不是"这段原文本来就没有
    # 角色出场"（例如纯风景过场）——两个条件同时成立时 character_mentions
    # 仍整段为空就是可疑信号。只记录进 _publish_prep_pack 的 evaluation.
    # evidence（同 rejected_paratext_claims 等既有观测字段的路子），不
    # raise：既定方向是必被看见，不是必被拦住，交付判据仍然是逐条对原文。
    character_manifest_anomaly = (
        {
            "known_character_count": len(known_characters),
            "scene_mention_count": len(scene_mentions),
            "prop_mention_count": len(prop_mentions),
        }
        if known_characters and not character_mentions and (scene_mentions or prop_mentions)
        else None
    )

    delivered_indexes: set[int] = set()
    for mention in (*character_mentions, *scene_mentions, *prop_mentions):
        delivered_indexes.update(mention["segment_indexes"])
    paratext_indexes = set(deterministic_title_indexes) | deterministic_paratext_segments
    ledger, rejected_paratext_claims = _prep_pack_build_coverage_ledger(
        len(segments), delivered_indexes, paratext_indexes,
    )
    # 2.0.3（见 PREP_PACK_VERSION 上方 2.0.3 大注释）：跟上面五账并列的
    # 场景专项覆盖账，读的是 scene_mentions 自己的 segment_indexes 并集
    # ——跟 delivered_indexes 同一个数据源（模型申报、已过结构闸），不是
    # 发布后的 asset_manifest.scenes 重新算一遍；两者在一次成功发布里
    # 恒等（_resolve_assets 只会把已声明的 mention 解析进 manifest 或
    # 让整个生成因 asset_errors 失败重试，不会把已声明的 mention 悄悄
    # 丢弃却仍然发布成功），用前者可以在 _resolve_assets 调用之前就算好，
    # 不需要为了这一个账目改动下面的调用顺序。
    scene_delivered_indexes: set[int] = set()
    for mention in scene_mentions:
        scene_delivered_indexes.update(mention["segment_indexes"])
    ledger["scene_coverage"] = _prep_pack_scene_coverage_account(
        len(segments), scene_delivered_indexes, paratext_indexes,
    )
    try:
        assert_prep_pack_coverage_complete(ledger)
    except ValueError as exc:
        # 结构上不应发生（见 _prep_pack_build_coverage_ledger 的三分穷尽
        # 论证）——留作纵深防御，不静默吞掉一个理论上不可能出现的账本矛盾。
        raise PrepPackGateError(str(exc)) from exc

    # appellation_map 真源出参（2.0.1 bug fix，见 _prep_pack_build_
    # appellation_map 上方大注释）：这个函数是 _resolve_assets 的唯一
    # 生产调用点，传一份空列表进去，_resolve_assets 在解析每条角色提及
    # 时原地写入，调用返回后就是这一集完整、真实的解析结论。
    character_appellation_resolutions: list[dict[str, Any]] = []
    (
        characters, scenes, props, functional_extras, asset_errors, discovery_stats,
        true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    ) = await _run_async_step(
        run_id, "episode_prep_pack_asset_mapping",
        lambda: _resolve_assets(
            conn, project_id=project_id, episode_id=episode_id, episode_no=episode_no,
            source_text=source_text, discovery_text=discovery_text,
            character_mentions=character_mentions, scene_mentions=scene_mentions,
            prop_mentions=prop_mentions, run_id=run_id,
            appellation_resolutions=character_appellation_resolutions,
        ),
    )
    if asset_errors:
        raise PrepPackGateError(
            "资产映射未能 100% 解析（已尝试身份/场景发现，调用次数："
            f"角色 {discovery_stats['character_discovery_calls']}、"
            f"场景 {discovery_stats['scene_discovery_calls']}）："
            + "；".join(asset_errors[:10])
        )

    asset_manifest = {
        "characters": characters, "scenes": scenes, "props": props,
        "functional_extras": functional_extras,
    }
    # provenance 发布前自校验（1.6.0，第25轮收口）：见
    # _prep_pack_verify_manifest_provenance 上方完整说明——每一条非空
    # anchor_phrase 必须真的逐字命中它自己 anchor_segments 指向的原文段，
    # 不成立即门禁拦，不静默发布一份自称有证据、实际验不过的 manifest。
    provenance_errors = _prep_pack_verify_manifest_provenance(
        segments, asset_manifest, source_text,
    )
    if provenance_errors:
        raise PrepPackGateError(
            "资产来源证明自校验失败：" + "；".join(provenance_errors[:10])
        )

    appellation_map = _prep_pack_build_appellation_map(character_appellation_resolutions)

    payload = {
        "prep_pack_version": PREP_PACK_VERSION,
        "episode_no": episode_no,
        "episode_scope": {
            "chapter_indexes": chapter_indexes,
            "source_segment_count": len(segments),
        },
        "asset_manifest": asset_manifest,
        "appellation_map": appellation_map,
        "coverage_ledger": ledger,
    }
    return (
        payload, rejected_paratext_claims, true_name_hints,
        scene_alias_anchors, rejected_alias_conflicts, character_manifest_anomaly,
    )


# ---------------------------------------------------------------------------
# Atomic publish (原子发布 + 完成证书)
# ---------------------------------------------------------------------------

