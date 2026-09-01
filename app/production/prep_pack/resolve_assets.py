"""_resolve_assets: the main per-episode asset-resolution pass (characters,
scenes, props, functional extras) plus the prop-manifest builder.

Split out of app/production/prep_pack.py. _resolve_assets is kept as one
function verbatim (moved, not rewritten) -- it is ~970 lines, so this file
exceeds the usual 600-line/200-function-line file-shape targets; see the
package's split report for why further splitting was out of scope here.
"""
from __future__ import annotations

from app.identity_authority import visual_entity_id_for_resolution
from app.source_excerpt import (
    SourceSegment,
    index_source_segments,
)
from typing import Any

from .alias_resolution import (
    _prep_pack_cross_episode_alias_conflict,
    _prep_pack_lookup_character_alias_canonical_name,
)
from .asset_lookup import (
    _prep_pack_register_scene_alias_if_new,
    _prep_pack_resolve_scene_reference_with_alias,
    _resolve_portrait_id,
    _resolve_scene_reference_id,
)
from .chunk_extraction import _run_async_step
from .discovery import (
    _character_discovery_dispositions,
    _discover_new_characters,
    _discover_new_scenes,
    _discovery_errored_names,
    _load_project_bible,
)
from .functional_candidate_verdict import _prep_pack_resolve_functional_extra_candidate
from .provenance import (
    _prep_pack_first_evidence_segment,
    _prep_pack_local_text_anchor,
    _prep_pack_mention_has_text_evidence,
    _prep_pack_provenance,
    _prep_pack_scene_alias_provenance,
)
from .true_name import (
    _prep_pack_collect_true_name_verification_requests,
    _prep_pack_gather_concurrent,
    _prep_pack_verify_true_name_hypothesis,
)


async def _resolve_assets(
    conn, *, project_id: str, episode_id: str, episode_no: int,
    source_text: str,
    character_mentions: list[dict[str, Any]],
    scene_mentions: list[dict[str, Any]],
    prop_mentions: list[dict[str, Any]],
    run_id: str | None,
    appellation_resolutions: list[dict[str, Any]] | None = None,
    discovery_text: str | None = None,
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
    list[str], dict[str, int],
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
]:
    """Resolve every character/scene mention (invariant③); merge props.

    2.0.0: ``events`` (grouped-by-event) became three flat mention lists
    (``character_mentions``/``scene_mentions``/``prop_mentions`` -- see
    PREP_PACK_VERSION's 2.0.0 note). Every mention already carries its own
    ``segment_indexes`` (structurally chunk-scope-verified by
    _prep_pack_gate_segment_indexes before it ever reaches this function --
    NOT literal-text-verified, that stays this function's own job via the
    existing per-method gates below, unchanged from pre-2.0.0) --
    what used to be ``event_ids`` bookkeeping on each manifest entry is now
    the union of every contributing mention's ``segment_indexes``.

    Every character/scene mention goes through the same resolution attempt.
    The old chunk-extraction model's own ``is_background_extra`` guess (a
    *different*, earlier model call that never looked at the bible) is gone
    from the 2.0.0 schema entirely -- treating it as an exemption from
    resolution was exactly the bug a real EP2 run surfaced:
    "小胖子" (7 on-screen appearances, real dialogue) was tagged
    is_background_extra=true by the chunk extractor and, under the previous
    build of this function, skipped before ever reaching resolution -- even
    though "小胖子" is 李富贵, already in the bible with a portrait. The
    correct flow (per invariant③: resolve to an existing asset OR a
    deterministic generic-extra class, never a third "silently absent"
    option) is: direct name match against character_portraits/scene_references
    first (cheap, no model call) -> unresolved mentions (whatever the
    extractor guessed about them) go through the discovery/disambiguation
    mechanism inherited from the heavy pipeline (_discover_new_characters /
    _discover_new_scenes below) -> only discovery explicitly failing *on that
    specific name* (_discovery_errored_names) may still hard-block it. Pass 1
    is direct-match only; pass 2 re-resolves after discovery using whatever it
    newly registered (new cards+portraits, a known-alias -> canonical-name
    rename e.g. "小胖子" -> "李富贵", or a functional/no-asset disposition).

    A resolved character's manifest entry carries an ``aliases`` list: the
    distinct raw mention strings (e.g. ["小胖子"]) that resolved to it via a
    rename, for P1 storyboard prompts to use.

    Second real-run finding (EP13, coordinator-reviewed): app.portraits'
    identity discovery does its own independent read of the source text and
    phrases/scopes its own candidates differently from prep_pack's chunk
    extraction -- discovery resolved "外宗弟子" as functional_identity while
    the published chunk extraction said "一名外宗弟子" for what is plainly the
    same one-off crowd concept; several other occupation-title mentions
    ("养丹坊掌柜", "宝阁执事", "围观弟子") got no matching disposition at all
    by exact string, even though discovery ran cleanly (its own ``errors``
    was empty) and *did* resolve the real new character "曹阳" (a portrait was
    generated) in the very same call. Per app.portraits' own long-standing
    rule (portraits.py:1727,7340 -- an unconfirmed-real-name one-off keeps its
    own source label and gets a typed functional identity, never silently
    dropped nor renamed to something generic), a mention that discovery
    neither resolved nor explicitly failed on defaults to a functional extra
    under its own raw text -- not a card, not a portrait, not a gate error.
    The only thing that still hard-blocks after discovery runs is
    _discovery_errored_names: discovery said something concrete about that
    *specific* name (a confirmed real identity whose card generation itself
    failed, or an exception) -- "消歧和发现都没能给出任何归类结论" is the one
    state this function will not paper over.

    Episodes where pass 1 already resolves every mention by exact name never
    call discovery at all (``stats``' counters stay at 0) -- but note this is
    now a narrower case than "no new characters": any mention that is not an
    exact known name (a genuine one-off extra with no real name, not just a
    new named character) also routes through discovery so it can receive a
    real disposition instead of being assumed one way or the other.

    ``appellation_resolutions`` (2.0.1 bug fix, see the note above
    ``_prep_pack_build_appellation_map``): an optional out-parameter -- when
    the caller passes a list, this function extends it in place with one
    record per successfully-resolved character mention (``raw_mention``,
    ``segment_indexes``, ``identity_id``, ``canonical_appellation``), read
    directly off the same manifest entry each mention just resolved to
    inside ``_pass()``. This is deliberately NOT folded into this function's
    own return tuple: that tuple is asserted against verbatim (fixed arity,
    no ``*_``) by dozens of existing tests, and this is a pure additive
    capability for one caller (``_generate_prep_pack_once``'s appellation_map
    construction) -- an optional, ignore-by-default parameter keeps every
    existing caller's contract untouched. Defaults to ``None`` (no
    recording), which is what every call site except
    ``_generate_prep_pack_once`` uses.

    ``discovery_text`` (2.0.4, paratext 归一，见 PREP_PACK_VERSION 上方
    2.0.4 大注释): the paratext-stripped copy of ``source_text`` fed to
    ``_discover_new_characters`` -- only the discovery-facing copy is
    stripped, ``source_text`` itself (event-chain evidence, segment
    offsets) is never touched. The sole production caller
    (``_generate_prep_pack_once``) always computes this from persisted
    ``chapters.paratext_json`` and passes it explicitly. Defaults to
    ``None``, in which case this function falls back to ``source_text``
    unchanged (equivalent to "nothing to strip" -- the same fail-closed
    behavior ``strip_paratext`` itself has always had when it can't
    determine any paratext spans) so that direct unit-test callers of this
    function, which never exercise real paratext content, do not need to
    thread this parameter through.
    """
    stats = {"character_discovery_calls": 0, "scene_discovery_calls": 0}
    discovery_text = discovery_text if discovery_text is not None else source_text
    # 场景别名锚定（1.5.1，task①）：一次性加载，供 _pass 内的场景解析复用。
    bible = _load_project_bible(conn, project_id)
    segments = index_source_segments(source_text)
    scene_alias_anchors: list[dict[str, Any]] = []
    # 跨集别名一致性（1.5.2，task②）：见 _prep_pack_cross_episode_alias_conflict
    # 上方注释。
    rejected_alias_conflicts: list[dict[str, Any]] = []
    # 身份绑定审判程序的进程内判决缓存（第29轮，见 _prep_pack_verify_
    # true_name_hypothesis 上方完整说明第 4 点）：同一次 _resolve_assets
    # 调用内，同一个 (subject_kind, alias, suspected_true_name) 组合只真正
    # 发起一次模型裁决调用，两遍 _pass()（pass1/pass2）与同一遍内的多个
    # 事件共用同一份缓存；角色分支与场景分支也共用这同一个字典对象，键里
    # 的 subject_kind 就是两域的隔离手段（独立评审发现的 minor：旧版键只有
    # (alias, suspected_true_name)，角色域与场景域撞名会复用错误域的裁决）。
    true_name_verdict_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def _pass(
        skip_character_names: set[str],
        character_rename: dict[str, str],
        scene_rename: dict[str, str],
        non_person_names: set[str] = frozenset(),
        *,
        newly_added_character_names: frozenset[str] = frozenset(),
        newly_added_scene_names: frozenset[str] = frozenset(),
        resolution_evidence_by_label: dict[str, str] | None = None,
        candidate_verdict_pins: dict[str, dict[str, Any]] | None = None,
        functional_candidate_attempted_names: frozenset[str] = frozenset(),
    ) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], list[str], list[str], list[str],
        list[dict[str, Any]], list[dict[str, Any]],
    ]:
        resolution_evidence_by_label = resolution_evidence_by_label or {}
        # 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大注释、
        # _prep_pack_resolve_functional_extra_candidate 的完整说明）：
        # label -> 钉证命中的卷宗记录（{"segment_index", "text"}），供下面
        # method 判定分支单独标记 "candidate_verdict"、并直接复用钉证段落
        # 本身（代码检索出的真实原文）作为 anchor_phrase，不依赖模型转录。
        candidate_verdict_pins = candidate_verdict_pins or {}
        # functional_candidate_attempted_names（1.10.0，缺陷 A 顺带修复的
        # 可观测性缺口，见 PREP_PACK_VERSION 上方大注释）：这批标签虽然没被
        # 候选判别选中（否则会在 candidate_verdict_pins 里），但确实发起过
        # 一次模型调用——供 method="discovery" 的 functional_extras 条目
        # 标注 candidate_verdict_attempted=True，跟"候选集/卷宗为空，从未
        # 获得候选判别机会"（此参数不含该标签，provenance 里这个字段留空）
        # 区分开。
        characters: dict[str, dict[str, Any]] = {}
        scenes: dict[str, dict[str, Any]] = {}
        functional_extras: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        unresolved_characters: list[str] = []
        unresolved_scenes: list[str] = []
        # appellation_map 真源（2.0.1 bug fix，见 _prep_pack_build_
        # appellation_map 上方大注释）：每条真正走到 characters.setdefault
        # 那一步的角色提及，在这里原地记一行它自己刚刚解析出的结论
        # （identity_id/canonical_appellation 直接取自那个 entry，不是
        # 事后另算）。跟 characters/scenes 同一条"pass2 整体替换 pass1"
        # 规则——这个列表每次 _pass() 调用都是全新的，不跨两遍累加。
        character_appellation_rows: list[dict[str, Any]] = []
        # 1.5.0 观测记录：每条模型申报的 suspected_true_name 假设最终是被核验
        # 采信还是拒绝，都记一条（不影响门禁本身，见函数上方注释）。
        true_name_hints: list[dict[str, Any]] = []
        # K 并发预热（任务②，见 _prep_pack_collect_true_name_verification_
        # requests 上方大注释）：先把这一遍会用到的全部 (subject_kind, alias,
        # suspected_true_name) 三元组去重收集齐，减掉 true_name_verdict_cache
        # 里已经有的（pass2 复用 pass1 缓存，语义不变），剩下的一次性并发
        # 核验，把结果写进同一份 true_name_verdict_cache。下面主循环逐条
        # await 的既有调用不动，命中的是已经写热的缓存。
        pending_true_name_requests = [
            key for key in _prep_pack_collect_true_name_verification_requests(
                character_mentions, scene_mentions, character_rename, scene_rename,
            )
            if key not in true_name_verdict_cache
        ]
        if pending_true_name_requests:
            await _prep_pack_gather_concurrent([
                _prep_pack_verify_true_name_hypothesis(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text,
                    alias=alias, suspected_true_name=suspected_true_name,
                    subject_kind=subject_kind, bible=bible,
                    resolve_fn=(
                        _resolve_portrait_id if subject_kind == "character"
                        else _resolve_scene_reference_id
                    ),
                    run_id=run_id, verdict_cache=true_name_verdict_cache,
                )
                for subject_kind, alias, suspected_true_name in pending_true_name_requests
            ])
        for mention in character_mentions:
            name = str(mention["display_name"] or "").strip()
            if not name:
                errors.append("存在空白角色名")
                continue
            mention_segment_indexes = sorted(
                {int(index) for index in mention.get("segment_indexes") or []}
            )
            resolved_name = character_rename.get(name, name)
            # provenance（1.6.0）：记录这次改名到底走的是哪条路径——
            # via_alias_registry 单独标记 task① 的注册表命中（跟
            # character_rename/suspected_true_name 都不是同一件事，
            # method 要区分 alias vs resolution）。
            via_alias_registry = False
            # 第30轮 RCA（真实 EP2/6/8 回归：resolution_forward 空
            # forward_chapter_label/anchor_phrase）：这里曾经用
            # "resolved_name == suspected_true_name" 反推"是否经过真名
            # 核验"，但 resolved_name 也可能通过 character_rename（角色
            # 发现/消歧，完全独立的另一条路径）恰好也算出同一个真名——
            # 两条路径殊途同归到同一个名字，不代表这次核验真的跑过；一旦
            # 走的是 character_rename 这条路，下面 if 判据里的
            # suspected_true_name != resolved_name 从一开始就是 False，
            # 核验分支被跳过，true_name_pinned_quote/chapter_idx 从未被
            # 赋值，停在初始空值——但后面的 method 判定分支当年只看
            # "resolved_name == suspected_true_name" 这个结果状态，认定
            # 它是核验通过，于是带着两个空值直接产出 resolution_forward。
            # 跟场景侧的 via_suspected_true_name 同一个修法：只在真正跑
            # 过核验且 accepted 时才置位，用这个专用布尔判定，不再用状态
            # 反推过程。
            via_suspected_true_name = False
            true_name_dual_anchor: bool | None = None
            suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
            true_name_pinned_quote = ""
            true_name_pinned_chapter_idx: int | None = None
            if suspected_true_name and suspected_true_name != resolved_name:
                verification = await _prep_pack_verify_true_name_hypothesis(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text,
                    alias=name, suspected_true_name=suspected_true_name,
                    subject_kind="character", bible=bible,
                    resolve_fn=_resolve_portrait_id, run_id=run_id,
                    verdict_cache=true_name_verdict_cache,
                )
                if verification["accepted"]:
                    resolved_name = suspected_true_name
                    via_suspected_true_name = True
                    true_name_pinned_quote = verification["pinned_quote"]
                    true_name_pinned_chapter_idx = verification["pinned_chapter_idx"]
                    true_name_dual_anchor = verification["dual_anchor"]
                    true_name_hints.append({
                        "kind": "character", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "accepted",
                        "dual_anchor": true_name_dual_anchor,
                    })
                else:
                    true_name_hints.append({
                        "kind": "character", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "rejected",
                        "reason": verification["reason"],
                    })
            # 缺陷 B 修复（1.10.0，见 PREP_PACK_VERSION 上方大注释的完整
            # 根因）：skip_character_names 短路必须排在 suspected_true_
            # name 核验之后、且核验通过时不得短路。角色发现
            # （_discover_new_characters）是本函数之外一次独立的全集
            # 重新通读，产出的 source_label 只是字符串，可能跟本提及的
            # 原始 name 恰好撞同一个字面量却指向完全不相关的判定（真实
            # EP5 事故：pass1 已核验通过"许姓女子"→"许清"、钉证成功、
            # accepted=True；同一轮 pass2 因角色发现独立判定另一处
            # "许姓女子"字面为功能性群演，把这个字符串加进了
            # skip_character_names——旧代码里这个 continue 排在核验之前
            # 执行，短路掉了本该重新核验（或至少复用 true_name_verdict_
            # cache 里 pass1 已经算好的 accepted 判决）的机会，pass1 的
            # 结论被静默作废，4 条提及从未进 unresolved_characters，也就
            # 从未有机会触发以"许姓女子"为标的的候选判别）。已核验通过的
            # 信号必须优先于"角色发现独立通读凑巧撞出同名功能簇"这个更
            # 弱的兜底信号，故 continue 的条件追加
            # `and not via_suspected_true_name`；缓存复用则由上面
            # _prep_pack_verify_true_name_hypothesis 内部的
            # true_name_verdict_cache 命中自动生效（pass1 已经缓存过的
            # (subject_kind, alias, suspected_true_name) 组合，pass2
            # 重新执行到这里时直接命中缓存，不会真的再发一次模型调用）。
            if name in skip_character_names and not via_suspected_true_name:
                if name not in non_person_names:
                    # provenance（1.6.0）：这批 functional_extras 全部
                    # 来自本轮的角色发现（要么发现明确判定 skip，要么是
                    # 发现跑过之后"既没归类也没报错"的默认兜底——两种
                    # 情形都必然经过了发现调用，method 统一记 discovery，
                    # 锚点用触发发现的原始描述 name 自身在本集原文里的
                    # 出现位置，找不到就是空锚点（见 _prep_pack_local_
                    # text_anchor）。
                    extra_anchor_segments, extra_anchor_phrase = (
                        _prep_pack_local_text_anchor(segments, [name])
                    )
                    # visual_entity_id（1.7.0，层三）：未具名/群演分支，
                    # 种子只取 source_label（这批群演自己的原文标签，即
                    # dict 键 name）+ scope_qualifier（prep_pack 自己的
                    # 事件链抽取模型不产出这个字段，留空——跟 K 决议提示
                    # 词规则8"留空即唯一指向一个人"同一语义，不是遗漏）。
                    # 同一原文标签跨集重复出现时这里恒定，是"未具名角色
                    # 也有跨集稳定视觉实体"这条设计判据的落地点。
                    # label 本身不替换（不同于角色侧 display_appellation）：
                    # 见 _prep_pack_provenance 上方 2.0.0 说明——label 是
                    # 这个群演唯一的展示/连接键。label_literal（1.11.0）
                    # 已在 2.0.0 撤下（纯范围收窄，不是结构性恒真——见
                    # _prep_pack_gate_segment_indexes 上方说明）。
                    extra = functional_extras.setdefault(name, {
                        "segment_indexes": [],
                        "visual_entity_id": visual_entity_id_for_resolution({
                            "source_label": name, "scope_qualifier": "",
                        }),
                        "provenance": _prep_pack_provenance(
                            "discovery", extra_anchor_segments, extra_anchor_phrase,
                            candidate_verdict_attempted=(
                                name in functional_candidate_attempted_names
                            ),
                        ),
                    })
                    extra["segment_indexes"] = sorted(
                        set(extra["segment_indexes"]) | set(mention_segment_indexes)
                    )
                continue
            # 跨集别名一致性（task②，见 _prep_pack_cross_episode_alias_
            # conflict 上方注释，真实 EP3 回归："小胖子"曾被误改绑到项目内
            # 另一个已发布分集绑定的"王有材"，本集完全没有"王有材"的文本
            # 依据）：任何改名（不管来自身份消歧的 character_rename 还是
            # 上面刚核验通过的 suspected_true_name）落地前，检查这个称谓
            # 是否已经在项目内被绑定给了别人——是就拒绝这次改绑，回退到
            # name 自己的常规解析路线（不静默接受任何一边），并记入
            # rejected_alias_conflicts（观测）。
            if resolved_name != name:
                conflicting_name = _prep_pack_cross_episode_alias_conflict(
                    conn, project_id, episode_id,
                    alias=name, canonical_name=resolved_name, bible=bible,
                )
                if conflicting_name:
                    rejected_alias_conflicts.append({
                        "mention": name,
                        "attempted_canonical_name": resolved_name,
                        "conflicting_canonical_name": conflicting_name,
                    })
                    resolved_name = name
            portrait_id = _resolve_portrait_id(conn, project_id, resolved_name, episode_no)
            if not portrait_id:
                # 角色别名注册表读侧（task①，真实第24轮 EP3 回归
                # ERR-20260824-d0830a，见 _prep_pack_lookup_character_
                # alias_canonical_name 上方注释）：裸精确匹配失败后，查
                # 项目内已发布分集的别名注册表——这个称谓是否已经在别的
                # 分集里被确立过归属（"小胖子"经 EP2 一次消歧确立即归
                # 李富贵，EP3+ 应直接复用，不必每集重新赌一次消歧模型
                # 调用）。复用 _prep_pack_cross_episode_alias_conflict
                # 同一套冲突拒绝逻辑守住多目标——命中但被判定跟别的分集
                # 已确立的归属冲突就不绑定，回退到常规解析路线（回炉
                # discovery）。
                aliased_name = _prep_pack_lookup_character_alias_canonical_name(
                    conn, project_id, episode_id, name, bible=bible,
                )
                if aliased_name and aliased_name != resolved_name:
                    conflicting_name = _prep_pack_cross_episode_alias_conflict(
                        conn, project_id, episode_id,
                        alias=name, canonical_name=aliased_name, bible=bible,
                    )
                    if conflicting_name:
                        rejected_alias_conflicts.append({
                            "mention": name,
                            "attempted_canonical_name": aliased_name,
                            "conflicting_canonical_name": conflicting_name,
                        })
                    else:
                        resolved_name = aliased_name
                        via_alias_registry = True
                        portrait_id = _resolve_portrait_id(
                            conn, project_id, resolved_name, episode_no,
                        )
            if not portrait_id:
                errors.append(
                    f"角色「{name}」（段 {mention_segment_indexes}）未解析到已有 "
                    "portrait_id，身份消歧也未能将其归类为已有角色或确定性群演"
                )
                if name not in unresolved_characters:
                    unresolved_characters.append(name)
                continue
            # 称谓证据闸语义精化（1.5.x task②，真实第24轮 EP3 回归
            # ERR-20260824-d0830a）：「穿杂役衫的魁梧大汉」经消歧正确解析到
            # 赵武刚，却被本闸拦下——原文对这个人只有分散的描述性叙述，模型
            # 综合出的这个名词短语天然不可能逐字出现在原文里，这不是幻觉
            # 归属的形状。resolved_name != name（came_via_resolution）精确
            # 区分两种情形，用的正是已经在算的同一个信号，不需要另外记状态：
            #   - 裸直接命中（resolved_name == name，没有经过 character_
            #     rename/suspected_true_name/别名注册表任何一条改名路径）：
            #     反幻觉主防线不动，称谓本身仍必须逐字出现在原文——这是
            #     真实 EP5 丹鬼案要拦的唯一形状（模型直接写下一个谱内已有
            #     名字，未经任何消歧）。
            #   - 经消歧/发现/别名注册表解析绑定（resolved_name != name）：
            #     合法性由那条解析路径自身的证据链承担（身份消歧模型看的
            #     就是本集原文；suspected_true_name 有自己独立的逐字/前瞻
            #     窗口核验；别名注册表命中继承的是别的分集当时已经过同一套
            #     证据核验的结论）——不再重复要求 name 本身逐字出现。
            came_via_resolution = resolved_name != name
            literal_evidence = _prep_pack_mention_has_text_evidence(name, source_text)
            if not came_via_resolution and not literal_evidence:
                errors.append(
                    f"角色「{name}」（段 {mention_segment_indexes}）解析到已有角色"
                    f"「{resolved_name}」（portrait_id={portrait_id}），但称谓「{name}」"
                    "未逐字出现在本集原文中，缺少称谓证据，门禁具名拦截"
                )
                continue
            # provenance method（1.6.0，第25轮收口）：discovery（本次
            # 新建卡）优先于其它任何改名信号判定——即便新建卡的同时也发生
            # 了改名（罕见但可能：消歧把"大汉"判成新建角色"赵武刚"），
            # "这是一张这次才出现的新卡"是更具体、更有信息量的事实。
            # 其次是 alias（task① 注册表命中）、resolution（消歧/真名
            # 核验改名）、最后是 direct（裸命中，未经任何改名）。
            forward_chapter_label = ""
            if resolved_name in newly_added_character_names:
                method = "discovery"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments, [name],
                )
            elif via_alias_registry:
                method = "alias"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments, [name],
                )
            elif via_suspected_true_name:
                # 身份绑定审判程序核验通过（第29轮，见
                # _prep_pack_verify_true_name_hypothesis 上方完整
                # 说明）：钉住的支撑句（true_name_pinned_quote）如果
                # 真的落在本集自己的段落里，就是最贴切的本地锚点
                # （它已经在裁决时被模型独立确认"确定是同一人"，比
                # 泛泛的候选搜索更有信息量）；如果钉住的支撑句来自
                # 全书别的章节（本集原文里找不到这句话），说明这条
                # 绑定的真实依据不在本集，method 单独标记
                # "resolution_forward"，空锚（anchor_segments）豁免
                # 合法，但 anchor_phrase 仍然记这句被钉住的支撑句本身
                # （第30轮 RCA 修正：过去这里误写成空字符串——anchor_
                # phrase 是"引用的是哪句话"，anchor_segments 才是"这句
                # 话在不在本集本地"，两者不是一回事，空 anchor_segments
                # 不代表 anchor_phrase 也该是空的），连同裁决真正引用的
                # 章节号一并写进 provenance，供审计核对。
                local_index = _prep_pack_first_evidence_segment(
                    segments, true_name_pinned_quote,
                )
                if local_index is not None:
                    method = "resolution"
                    anchor_segments = [local_index]
                    anchor_phrase = true_name_pinned_quote
                else:
                    method = "resolution_forward"
                    anchor_segments, anchor_phrase = [], true_name_pinned_quote
                    if true_name_pinned_chapter_idx is not None:
                        forward_chapter_label = f"第 {true_name_pinned_chapter_idx} 章"
            elif name in candidate_verdict_pins:
                # 未解析角色标签候选判别命中（1.8.0，见 PREP_PACK_
                # VERSION 上方大注释、_prep_pack_resolve_functional_
                # extra_candidate 的完整说明）：method 单独标记，不
                # 复用泛化的 "resolution"——那个标签专指 discovery 自身
                # 消歧结果（resolution_evidence_by_label 的来源），这里
                # 走的是另一条独立证据链：本函数内代码检索卷宗 → 候选
                # 选择题 → 段号钉证，provenance 要如实标注两者的区别，
                # 供审计区分走哪条核验路径复核。anchor_phrase 直接取
                # 钉证命中的卷宗段落原文本身（代码检索出的真实原文，
                # 不是模型转录），天然满足自校验的逐字命中要求。这个
                # elif 分支必须排在 via_suspected_true_name 之后、
                # came_via_resolution 之前——同一提及若恰好也核验通过
                # 了 suspected_true_name，那条更具体的证据链优先（elif
                # 链短路，不会执行到这里）。
                method = "candidate_verdict"
                pin = candidate_verdict_pins[name]
                anchor_segments = [pin["segment_index"]]
                anchor_phrase = pin["text"]
            elif came_via_resolution:
                method = "resolution"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments,
                    [resolution_evidence_by_label.get(name, ""), suspected_true_name, name],
                )
            else:
                method = "direct"
                anchor_segments, anchor_phrase = _prep_pack_local_text_anchor(
                    segments, [name],
                )
            # visual_entity_id / display_appellation（1.7.0，层三，见设计
            # 文档 §4.3）：characters[] 条目永远是已解析到 portrait_id 的
            # 具名角色（1.3.0 冻结的既有含义），resolution="future_identity"
            # + canonical_name=resolved_name 走 visual_entity_id_for_
            # resolution 的具名分支，恒等于 f"bible:{resolved_name}"——跟
            # identity_id 同格式，是刻意的（设计文档 §4.2"零迁移成本"）。
            # display_appellation 取这个 portrait_id 在本集第一次出现时的
            # 原始提及文本 name（画面取图看 visual_entity_id，字幕/称呼看
            # display_appellation，两者分离——本集只说本集措辞，不提前
            # 剧透 display_name 这个全局规范名）。
            # 标签接地（1.11.0 引入，1.11.1 撤回替换手段，2.0.0 撤下整个
            # label_literal 字段——纯范围收窄，不是结构性恒真，见
            # PREP_PACK_VERSION 上方 2.0.0 大注释与 _prep_pack_gate_
            # segment_indexes 上方说明）：display_appellation 永远保持
            # name（不替换，跟 1.11.1 撤回后的最终结论一致）：唯一可能的
            # 确定性替换来源是 anchor_phrase，但 anchor_phrase 是"钉证命中
            # 的证据段落"，不是"称谓"，真实 EP1 数据坐实二者不可互换。
            display_appellation = name
            entry = characters.setdefault(portrait_id, {
                "identity_id": f"bible:{resolved_name}",
                "display_name": resolved_name,
                "portrait_id": portrait_id,
                "segment_indexes": [],
                "aliases": [],
                "visual_entity_id": visual_entity_id_for_resolution({
                    "resolution": "future_identity",
                    "canonical_name": resolved_name,
                }),
                "display_appellation": display_appellation,
                "provenance": _prep_pack_provenance(
                    method, anchor_segments, anchor_phrase,
                    forward_chapter_label=forward_chapter_label,
                    dual_anchor=(true_name_dual_anchor if via_suspected_true_name else None),
                ),
            })
            entry["segment_indexes"] = sorted(
                set(entry["segment_indexes"]) | set(mention_segment_indexes)
            )
            # appellation_map 真源（2.0.1 bug fix）：这条提及已经真正走到
            # 这里——有 portrait_id、通过了称谓证据闸——就是一条真实的
            # "模糊称谓 -> 身份"结论，读的是 entry 自己（跟 asset_manifest.
            # characters[] 发布出去的是同一个字典对象）刚定下的
            # identity_id/display_name，不是另算一遍。这一行故意不看
            # aliases（下面几行）：aliases 的字面证据门槛是为了保护跨集
            # 别名注册表不被合成标签污染，是另一个维度的判据，"能不能安全
            # 进注册表"不等于"这条提及有没有解析出身份"——把 aliases 当成
            # 后者的真源用，正是"穿杂役衫的魁梧大汉"这类合成描述提及在旧
            # 实现里从 appellation_map 里静默消失的根因。
            character_appellation_rows.append({
                "raw_mention": name,
                "segment_indexes": list(mention_segment_indexes),
                "identity_id": entry["identity_id"],
                "canonical_appellation": entry["display_name"],
            })
            # 别名注册仍只登记逐字出现于原文的称谓（task②，见上方门禁
            # 注释）：组合/综合描述短语（"穿杂役衫的魁梧大汉"）合法通过了
            # 门禁，但绝不能进别名库——别名注册表是 task① 直接信任的读侧
            # 数据源，一旦被模型综合出的合成词污染，将来会被当成"这就是
            # 原文真实用过的称呼"重新播种给别的分集。
            if came_via_resolution and literal_evidence and name not in entry["aliases"]:
                entry["aliases"].append(name)
        for mention in scene_mentions:
            name = str(mention["display_name"] or "").strip()
            if not name:
                errors.append("存在空白场景名")
                continue
            mention_segment_indexes = sorted(
                {int(index) for index in mention.get("segment_indexes") or []}
            )
            # 2.0.2：这条提及自己申报的逐字引文（见 _ModelSceneMention.quote
            # 与 PREP_PACK_VERSION 上方 2.0.2 大注释）——resolution/discovery
            # 锚点候选表下面会用到，独立于 name/canonical_scene_name 本身。
            scene_quote = str(mention.get("quote") or "").strip()
            resolved_via_discovery = name in scene_rename
            resolved_name = scene_rename.get(name, name)
            via_suspected_true_name = False
            true_name_dual_anchor: bool | None = None
            true_name_pinned_quote = ""
            true_name_pinned_chapter_idx: int | None = None
            suspected_true_name = str(mention.get("suspected_true_name") or "").strip()
            if (
                suspected_true_name
                and suspected_true_name != resolved_name
                and not resolved_via_discovery
            ):
                verification = await _prep_pack_verify_true_name_hypothesis(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text,
                    alias=name, suspected_true_name=suspected_true_name,
                    subject_kind="scene", bible=bible,
                    resolve_fn=_resolve_scene_reference_id, run_id=run_id,
                    verdict_cache=true_name_verdict_cache,
                )
                if verification["accepted"]:
                    resolved_name = suspected_true_name
                    via_suspected_true_name = True
                    true_name_pinned_quote = verification["pinned_quote"]
                    true_name_pinned_chapter_idx = verification["pinned_chapter_idx"]
                    true_name_dual_anchor = verification["dual_anchor"]
                    true_name_hints.append({
                        "kind": "scene", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "accepted",
                        "dual_anchor": true_name_dual_anchor,
                    })
                else:
                    true_name_hints.append({
                        "kind": "scene", "mention": name,
                        "suspected_true_name": suspected_true_name, "status": "rejected",
                        "reason": verification["reason"],
                    })
            scene_reference_id, canonical_scene_name = (
                _prep_pack_resolve_scene_reference_with_alias(
                    conn, project_id, episode_no, resolved_name, bible,
                )
            )
            # 场景证据闸（1.4.2，见 _prep_pack_mention_has_text_evidence 上方
            # 注释）：只在"裸直接命中"（这个 label 从未被场景发现处理过，即
            # 不在 scene_rename 里——用"是否是该字典的 key"判定，不能用
            # resolved_name == name 的字符串比较：发现判定新场景的规范名恰好
            # 与原始 label 相同也是完全合法的结果，比如新建场景直接沿用了
            # 提及原文，字符串相等不代表没被发现处理过）时核验——一旦这个
            # label 真的经过发现，就信任发现自己更细致的判定，不再重复核验
            # （新建场景的规范名通常是 AI 综合描述出的标签，本就不会逐字出现
            # 在原文里，用同一条子串检查会误伤合法的新建）。没证据 → 当作未
            # 解析，走场景发现（本例应新建"靠山宗外围山峰"），不是直接拒绝
            # ——场景侧允许回炉重新判定，跟角色侧"具名拦截"不对称是刻意的：
            # 新建场景的代价低，发现机制本身就是给"裸命中没证据"设计的下一步。
            if (
                scene_reference_id
                and not resolved_via_discovery
                and not _prep_pack_mention_has_text_evidence(name, source_text)
            ):
                scene_reference_id = None
            if not scene_reference_id:
                errors.append(
                    f"场景「{name}」（段 {mention_segment_indexes}）未解析到已有 "
                    "scene_reference_id"
                )
                if name not in unresolved_scenes:
                    unresolved_scenes.append(name)
                continue
            # 场景别名锚定（task①）：证据闸已经确认这次绑定成立——把本集
            # 实际用到的原文措辞（name）记为该场景的新别名（幂等，见
            # _prep_pack_register_scene_alias_if_new），别名库随集数增长；
            # 观测元数据记录锚定来源段号，不影响任何门禁判断。
            registered = _prep_pack_register_scene_alias_if_new(
                conn, project_id,
                canonical_name=canonical_scene_name, wording=name,
            )
            if registered:
                scene_alias_anchors.append({
                    "canonical_scene_name": canonical_scene_name,
                    "alias": name,
                    "anchor_segment": _prep_pack_first_evidence_segment(segments, name),
                })
            resolved_name = canonical_scene_name
            # provenance method（1.6.0）：跟角色侧同一优先级顺序——
            # discovery（本次新建场景）优先于其它任何信号；然后是经
            # 发现匹配到已有场景（resolution）；然后是 suspected_
            # true_name 核验通过（resolution）；然后是场景别名回退命中
            # （alias，_prep_pack_resolve_scene_reference_with_alias 内部
            # 的 match_scene_name 回退分支）；最后是裸直接命中（direct）。
            scene_forward_chapter_label = ""
            scene_source_episode_no: int | None = None
            # 场景绑定的锚点候选（第28轮 ERR-20260824，v3 审计
            # A2_scene_no_text_evidence 25 条；2.0.2 恢复第三路候选，见
            # PREP_PACK_VERSION 上方 2.0.2 大注释——48e01ff 砍 event_chain
            # 时曾把这里收窄成只剩 [canonical_scene_name, name]，两路都是
            # 模型综合出的合成标签时结构上必然两路皆空，是当晚引入的真实
            # 回归，不是本条注释历史上就接受的设计）：resolution/discovery
            # 两支试 [canonical_scene_name, name, scene_quote]——发现新建
            # 场景、或消歧把一个提及判给已有场景时，模型申报的规范名
            # （canonical_scene_name）本身可能才是原文里真正出现的措辞
            # （label 是提及方式，canonical 是模型综合出的标签，反之亦然，
            # 取决于具体场景），scene_quote 是这条提及自己申报、经
            # _prep_pack_local_text_anchor 逐字核验的独立证据（isomorphic
            # 于旧 event_chain[].source_evidence[].quote，见 _ModelSceneMention.
            # quote 上方注释）——不是同义反复：它不是 name/canonical_scene_
            # name 的重复或变体，是模型对"这段原文写的是不是这个地点"这个
            # 独立问题的另一次单独申报，真假不由申报本身决定，由它是否
            # 逐字命中原文决定。三路候选都试过仍找不到，才是真的没有本集
            # 依据（下面 has_scene_anchor 会拦截，不再像 1.6.0 最初实现
            # 那样静默放行空锚）。
            if canonical_scene_name in newly_added_scene_names:
                scene_method = "discovery"
                scene_anchor_segments, scene_anchor_phrase = (
                    _prep_pack_local_text_anchor(
                        segments, [canonical_scene_name, name, scene_quote],
                    )
                )
            elif resolved_via_discovery:
                scene_method = "resolution"
                scene_anchor_segments, scene_anchor_phrase = (
                    _prep_pack_local_text_anchor(
                        segments, [canonical_scene_name, name, scene_quote],
                    )
                )
            elif via_suspected_true_name:
                # 跟角色侧同一套判定（第29轮，见
                # _prep_pack_verify_true_name_hypothesis 上方完整
                # 说明）：钉住的支撑句落在本集自己的段落里就是最贴切
                # 的本地锚点；落在全书别的章节，本集内没有锚点是
                # 正确的，不是缺陷，method 单独标记
                # "resolution_forward"，anchor_phrase 仍记这句被钉住的
                # 支撑句本身（第30轮 RCA 修正，跟角色侧同一处漏改：空的
                # 只该是 anchor_segments 这个"本地段号"，不该连带着把
                # anchor_phrase 这句话本身也清空），把裁决真正引用的
                # 章节号写进 provenance。
                local_index = _prep_pack_first_evidence_segment(
                    segments, true_name_pinned_quote,
                )
                if local_index is not None:
                    scene_method = "resolution"
                    scene_anchor_segments = [local_index]
                    scene_anchor_phrase = true_name_pinned_quote
                else:
                    scene_method = "resolution_forward"
                    scene_anchor_segments, scene_anchor_phrase = [], true_name_pinned_quote
                    if true_name_pinned_chapter_idx is not None:
                        scene_forward_chapter_label = (
                            f"第 {true_name_pinned_chapter_idx} 章"
                        )
            elif canonical_scene_name != name:
                # 2.0.2：恢复传入这条提及自己的 quote 作为第三方参数
                # scene_event_evidence_quotes（该形参名未改——语义仍是
                # "候选独立证据引文列表"，只是来源粒度从"事件"下沉到
                # "提及"，见该函数 docstring 与 PREP_PACK_VERSION 上方
                # 2.0.2 大注释）；找不到独立证据时函数自身仍会诚实降级为
                # alias_inherited，不在这里改判据。
                (
                    scene_method, scene_anchor_segments, scene_anchor_phrase,
                    scene_source_episode_no,
                ) = _prep_pack_scene_alias_provenance(
                    conn, segments, scene_reference_id,
                    canonical_scene_name, [scene_quote],
                )
            else:
                scene_method = "direct"
                scene_anchor_segments, scene_anchor_phrase = (
                    _prep_pack_local_text_anchor(segments, [name])
                )
            entry = scenes.setdefault(scene_reference_id, {
                "scene_id": f"scene:{resolved_name}",
                "display_name": resolved_name,
                "scene_reference_id": scene_reference_id,
                "segment_indexes": [],
                "provenance": _prep_pack_provenance(
                    scene_method, scene_anchor_segments, scene_anchor_phrase,
                    forward_chapter_label=scene_forward_chapter_label,
                    source_episode_no=scene_source_episode_no,
                    dual_anchor=(true_name_dual_anchor if via_suspected_true_name else None),
                ),
            })
            entry["segment_indexes"] = sorted(
                set(entry["segment_indexes"]) | set(mention_segment_indexes)
            )
        return (
            characters, scenes, functional_extras, errors,
            unresolved_characters, unresolved_scenes, true_name_hints,
            character_appellation_rows,
        )

    (
        characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes,
        true_name_hints, character_appellation_rows,
    ) = await _pass(set(), {}, {})
    # 1.5.0：假设核验发生在每一遍 _pass() 内部；一个假设被拒绝的提及若同时触发
    # 发现（走到下面这个分支），第二遍 _pass() 的返回值会整体替换第一遍的——
    # 但第一遍已经记下的 true_name_hints 不该因此凭空消失（红灯 4b 明确要求
    # "rejected 计数=1" 且"走新场景发现"同时成立）。第一遍的记录单独保留，
    # 最后跟第二遍的合并去重。
    true_name_hints_pass1 = true_name_hints

    if unresolved_chars or unresolved_scenes:
        skip_character_names: set[str] = set()
        character_rename: dict[str, str] = {}
        scene_rename: dict[str, str] = {}
        non_person_names: set[str] = set()
        discovery_diagnostics: list[str] = []
        # provenance（1.6.0）：discovery 新建的角色/场景名单，以及角色消歧
        # 自带的证据引文（source_quote/evidence），供 _pass 判定 method 与
        # 计算 resolution 分支的锚点——见调用点上方各自的完整说明。
        newly_added_character_names: frozenset[str] = frozenset()
        newly_added_scene_names: frozenset[str] = frozenset()
        resolution_evidence_by_label: dict[str, str] = {}
        # 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大注释、
        # _prep_pack_resolve_functional_extra_candidate 的完整说明）：
        # label -> 钉证命中的卷宗记录，供下面 _pass() 的 method 判定分支
        # 单独标记 "candidate_verdict"。
        candidate_verdict_pins: dict[str, dict[str, Any]] = {}
        # 发起过候选判别模型调用、但没有选中任何候选的标签集合（1.10.0，
        # 缺陷 A 顺带修复的可观测性缺口，见 PREP_PACK_VERSION 上方大注释）：
        # 供 _pass() 把 functional_extras 条目的 provenance 标注
        # candidate_verdict_attempted=True，跟"候选集/卷宗为空、从未获得
        # 候选判别机会"的 method="discovery" 条目区分开。
        functional_candidate_attempted_names: set[str] = set()

        if unresolved_chars:
            stats["character_discovery_calls"] += 1
            discovery_result = await _run_async_step(
                run_id, "episode_prep_pack_character_discovery",
                lambda: _discover_new_characters(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, source_text=source_text,
                    discovery_text=discovery_text, run_id=run_id,
                ),
            )
            skip_character_names, character_rename, non_person_names = (
                _character_discovery_dispositions(discovery_result)
            )
            # discovery 就地补录人物谱（_discover_new_characters ->
            # ensure_cards_for_text 用同一个 conn 建卡建图），而函数开头那份
            # bible 是本次 _resolve_assets 开跑时读的，看不到刚补进去的人。
            # 下面的候选判别拿它构造候选集、pass2 拿它查别名，用陈旧快照会把
            # 刚补录的角色排除在候选之外：真实事故里王有材 17:59:30 建卡完成，
            # 18:00:30 的候选判别候选集仍是「孟浩、小虎、许清」，系统于是拿
            # 「王有材」这个标签去问模型"他是这三个里的哪一个"，模型只能答
            # "都不是"，本人反倒落进群演。
            bible = _load_project_bible(conn, project_id)
            newly_added_character_names = frozenset(
                str(item.get("name") or "").strip()
                for item in (discovery_result.get("added") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )
            resolution_evidence_by_label = {
                str(item.get("source_label") or "").strip(): str(
                    item.get("source_quote") or item.get("evidence") or ""
                ).strip()
                for item in (discovery_result.get("resolutions") or [])
                if isinstance(item, dict) and str(item.get("source_label") or "").strip()
            }
            errored_names = _discovery_errored_names(discovery_result, unresolved_chars)
            # Coordinator-mandated default: anything discovery neither
            # resolved (rename) nor explicitly disposed of (skip) nor
            # explicitly failed on (errored_names) is a typed functional
            # identity under its own source label, not a block -- but only
            # once directly-resolvable names are ruled out first (discovery
            # may have just committed a portrait under this exact raw name,
            # e.g. a genuinely new character discovery carded this call; that
            # must resolve normally, not get swept into the fallback).
            bible_character_names = {character.name for character in bible.characters}
            for name in unresolved_chars:
                if name in character_rename or name in errored_names:
                    continue
                if name in skip_character_names:
                    # discovery 判了「无需卡」（群演/仅被引用）。这条判据不看
                    # 它的结论字段，只看产物信号：人物谱在册 + 本集有可绑定的
                    # 定妆照。两条同时成立就说明这是有名有姓的具名角色，
                    # discovery 判错了，必须走具名解析而不是落群演——真实事故：
                    # 王有材人物谱在册、定妆照已生成，仍被判 functional_identity
                    # 扫进 functional_extras。discovery 明确判定的非人物
                    # （宗门/法器/笔名）不在此列，它们本就不该绑定到任何真人。
                    if name not in non_person_names and name in bible_character_names:
                        skip_character_names.discard(name)
                    continue
                # 判据挂"人物谱里有没有这张卡"，不挂"现在有没有图"：出图已解耦
                # 到后台，建清单这一刻图往往还没出来，用有没有图判会把连主角在内
                # 的全部具名角色打成群演（实测 characters 空、6 个全落
                # functional_extras）。有没有图是发起付费视频前那道闸门的事。
                if name in bible_character_names:
                    continue
                skip_character_names.add(name)
            discovery_diagnostics.extend(str(e) for e in discovery_result.get("errors") or [])

            # 未解析角色标签候选判别（1.8.0，见 PREP_PACK_VERSION 上方大
            # 注释、_prep_pack_resolve_functional_extra_candidate 的完整
            # 说明）：discovery 与上面的兜底默认都没能给出任何归类结论、
            # 即将落 functional_extras 的标签（skip_character_names 减去
            # non_person_names——非人物标签不该被判给任何真人候选），补一
            # 次代码检索卷宗 + 模型候选判别 + 段号钉证。命中就把
            # skip_character_names 里的这条移进 character_rename，让它
            # 重新走既有的具名解析路线（自然带出正确的 portrait_id/
            # identity_id/visual_entity_id；display_appellation 仍由下面
            # _pass 内的 name 本身承担，本集原文措辞不受这次改名影响，不
            # 提前剧透）。
            #
            # M 并发化（任务②，见 PREP_PACK_VERSION 上方大注释；10 集测量：
            # 38 次调用、串行 166.8 秒，6 并发估算 49.2 秒）：每个候选标签
            # 各自独立——_prep_pack_resolve_functional_extra_candidate 只读
            # DB/本集只读材料（bible/segments/character_mentions），不读、不写
            # skip_character_names/character_rename/candidate_verdict_pins
            # 这些正在本函数里改的共享状态，标签之间没有互相依赖，可以安全
            # 并发发起。但共享写回必须保持确定性（不得让并发完成顺序决定
            # 最终产物，跟 K 的"先聚合、再按序写"同一条纪律）：先按
            # unresolved_chars 的原始出场顺序筛出候选名单，asyncio.gather
            # 并发拿到全部结果（gather 按传入顺序、不是完成顺序返回，见
            # _prep_pack_gather_concurrent 上方注释），再用 zip 按同一份
            # 原始顺序逐个把结果写回 skip_character_names/character_rename/
            # candidate_verdict_pins/functional_candidate_attempted_names
            # 这几个共享容器——同一输入不管并发时哪个任务先完成，写回这一步
            # 本身单线程顺序执行，产物逐字节可复现。
            candidate_names = [
                name for name in unresolved_chars
                if name in skip_character_names and name not in non_person_names
            ]
            candidate_resolutions = await _prep_pack_gather_concurrent([
                _prep_pack_resolve_functional_extra_candidate(
                    conn, project_id=project_id, episode_id=episode_id,
                    episode_no=episode_no, label=name, source_text=source_text,
                    segments=segments, bible=bible,
                    character_mentions=character_mentions,
                )
                for name in candidate_names
            ])
            for name, resolution in zip(candidate_names, candidate_resolutions, strict=True):
                if resolution["attempted"]:
                    functional_candidate_attempted_names.add(name)
                if not resolution["resolved"]:
                    continue
                skip_character_names.discard(name)
                character_rename[name] = resolution["canonical_name"]
                candidate_verdict_pins[name] = resolution

        if unresolved_scenes:
            stats["scene_discovery_calls"] += 1
            scene_discovery_result = await _run_async_step(
                run_id, "episode_prep_pack_scene_discovery",
                lambda: _discover_new_scenes(
                    conn, project_id=project_id, episode_no=episode_no,
                    labels=unresolved_scenes,
                ),
            )
            scene_rename = dict(scene_discovery_result.get("resolved_names") or {})
            newly_added_scene_names = frozenset(
                str(item.get("name") or "").strip()
                for item in (scene_discovery_result.get("added") or [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            )
            discovery_diagnostics.extend(str(e) for e in scene_discovery_result.get("errors") or [])

        (
            characters, scenes, functional_extras, errors, unresolved_chars, unresolved_scenes,
            true_name_hints_pass2, character_appellation_rows,
        ) = await _pass(
            skip_character_names, character_rename, scene_rename, non_person_names,
            newly_added_character_names=newly_added_character_names,
            newly_added_scene_names=newly_added_scene_names,
            resolution_evidence_by_label=resolution_evidence_by_label,
            candidate_verdict_pins=candidate_verdict_pins,
            functional_candidate_attempted_names=frozenset(
                functional_candidate_attempted_names,
            ),
        )
        # 合并两遍，按内容去重（同一个提及在两遍里都核验出相同结论是正常的、
        # 无害的重复计算，不该在观测数据里出现两条一模一样的记录）。
        combined = true_name_hints_pass1 + true_name_hints_pass2
        seen: set[tuple[str, str, str, str]] = set()
        true_name_hints = []
        for hint in combined:
            key = (hint["kind"], hint["mention"], hint["suspected_true_name"], hint["status"])
            if key not in seen:
                seen.add(key)
                true_name_hints.append(hint)
        if errors and discovery_diagnostics:
            errors = list(errors) + [
                f"发现阶段诊断：{message}" for message in discovery_diagnostics[:5]
            ]

    functional_extras_payload = [
        {
            "label": label,
            "segment_indexes": data["segment_indexes"],
            "visual_entity_id": data["visual_entity_id"],
            "provenance": data["provenance"],
        }
        for label, data in functional_extras.items()
    ]
    props_payload = _prep_pack_build_prop_manifest(prop_mentions, segments)
    # appellation_map 真源出参（2.0.1 bug fix，见本函数 docstring
    # ``appellation_resolutions`` 一节与 _prep_pack_build_appellation_map
    # 上方大注释）：只在调用方真的传了列表时才写，默认 None 不记录，
    # 不影响这个函数自己的返回元组形状。
    if appellation_resolutions is not None:
        appellation_resolutions.extend(character_appellation_rows)
    return (
        list(characters.values()), list(scenes.values()), props_payload, functional_extras_payload,
        errors, stats, true_name_hints, scene_alias_anchors, rejected_alias_conflicts,
    )


# 2.0.0 新增：道具没有世界书图像素材库，不需要身份消歧/发现，也不需要
# suspected_true_name 声明-核验通道——一个道具就是它自己（结构判据，零
# 语义），按 label 精确字符串去重合并 segment_indexes 即可。
#
# 道具没有 characters/scenes 那样的"经解析路径绑定可豁免逐字"这条路
# （没有身份消歧、没有候选判别——道具的 label 就是它唯一的名字，不存在
# "解析成另一个规范名"这件事），因此每一个道具都等价于角色侧的"裸直接
# 命中"，反幻觉主防线必须适用：只保留 label 真的逐字出现在该段落原文里的
# segment_indexes（跟角色侧"称谓证据闸"同一判据，_prep_pack_gate_segment_
# indexes 的结构闸不做这一步是因为它对全部三种资产统一处理、且要给
# characters/scenes 的解析路径留豁免空间——道具没有这个豁免需求，在这里
# 单独把关不冲突）。一个道具的全部段号都验不过字面证据，整条提及丢弃（不
# 计入清单，不阻断发布——跟 scene 侧"没证据就当未解析"同一处置，不是
# "空口提名也发布"）。
def _prep_pack_build_prop_manifest(
    prop_mentions: list[dict[str, Any]], segments: list[SourceSegment],
) -> list[dict[str, Any]]:
    props: dict[str, dict[str, Any]] = {}
    for mention in prop_mentions:
        label = str(mention.get("label") or "").strip()
        if not label:
            continue
        segment_indexes = sorted(
            index for index in {int(i) for i in mention.get("segment_indexes") or []}
            if 1 <= index <= len(segments) and label in segments[index - 1].text
        )
        if not segment_indexes:
            continue
        entry = props.setdefault(label, {
            "label": label,
            "description": str(mention.get("description") or "").strip(),
            "segment_indexes": [],
            "provenance": _prep_pack_provenance("direct", [segment_indexes[0]], label),
        })
        entry["segment_indexes"] = sorted(
            set(entry["segment_indexes"]) | set(segment_indexes)
        )
    return list(props.values())


