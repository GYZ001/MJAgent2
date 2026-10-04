"""道具库反应式登记编排：判据 → 模型写外观锚点 → 出图 → 落 world bible + prop_references。

与 ``app.scenes`` 的分工镜像：判据/出图/落库拆到 ``judge.py``/``image.py``/
``store.py``，本文件只做编排，单函数体量照顾 CLAUDE.md 的「单函数 ≤50 代码行」。
"""
from __future__ import annotations

import json
import logging
import sqlite3

from app.bible_store import mutate_bible_json
from app.db import get_conn
from app.schemas import Bible, Prop
from app.source_excerpt import index_source_segments

from .card_match import evidence_text_for_segments, match_existing_prop_card
from .image import generate_prop_reference_image, prop_ref_prompt
from .labels import normalize_prop_label
from .judge import assess_prop_appearance, is_key_prop_mention
from .store import ensure_schema, latest_prop_reference_status, upsert_prop_reference

log = logging.getLogger(__name__)


def _load_bible(conn: sqlite3.Connection, project_id: str) -> Bible | None:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    raw = (row["bible_json"] or "").strip() if row else ""
    if not raw:
        return None
    return Bible.model_validate(json.loads(raw))


def _known_prop_names(props: list[Prop]) -> set[str]:
    names: set[str] = set()
    for prop in props:
        names.add(prop.name.strip())
        names.update(a.strip() for a in prop.aliases if a.strip())
    return names


def _append_prop_to_bible(conn: sqlite3.Connection, project_id: str, prop: Prop) -> bool:
    def mutate(data: dict) -> bool:
        existing = {p.get("name") for p in data.get("props", [])}
        if prop.name in existing:
            return False
        data.setdefault("props", []).append(prop.model_dump(mode="json"))
        return True

    return mutate_bible_json(conn, project_id, mutate)


def _append_prop_alias(conn: sqlite3.Connection, project_id: str, name: str, alias: str) -> bool:
    """把归一前的原标签登记为本体道具的别名（「两只野鸡」→「野鸡」），下游按别名仍能查到图。"""
    def mutate(data: dict) -> bool:
        for entry in data.get("props", []):
            if entry.get("name") == name:
                aliases = [str(a).strip() for a in entry.get("aliases") or [] if str(a).strip()]
                if alias in aliases or alias == name:
                    return False
                entry["aliases"] = [*aliases, alias]
                return True
        return False
    return mutate_bible_json(conn, project_id, mutate)


def _set_prop_ref_image_path(conn: sqlite3.Connection, project_id: str, name: str, path: str) -> bool:
    def mutate(data: dict) -> bool:
        for entry in data.get("props", []):
            if entry.get("name") == name:
                if entry.get("ref_image_path") == path:
                    return False
                entry["ref_image_path"] = path
                return True
        return False

    return mutate_bible_json(conn, project_id, mutate)


async def _generate_and_persist_prop_image(
    conn: sqlite3.Connection, project_id: str, episode_no: int, prop: Prop, *, style: str,
) -> str | None:
    prompt = prop_ref_prompt(style, prop.appearance_canonical, name=prop.name)
    image_path = await generate_prop_reference_image(project_id, prop.name, prompt)
    if image_path:
        _set_prop_ref_image_path(conn, project_id, prop.name, image_path)
    upsert_prop_reference(
        conn, project_id, prop.name, episode_no,
        appearance=prop.appearance_canonical, image_path=image_path, prompt=prompt,
        status="ready" if image_path else "failed", qa={},
    )
    # 连接归本模块所有（get_conn()），登记行必须在这里提交：EP1 回填实测图出来了、世界书
    # 条目也进了，prop_references 却一行没有——store 不提交、调用方进程退出即丢。
    conn.commit()
    return image_path


def _bind_known_base(
    conn: sqlite3.Connection, project_id: str, label: str, base: str, canonical: str, known: set[str],
) -> None:
    """``base``（连同原始 ``label``，若与它不同）都已归到既有卡片 ``canonical``：
    把尚未登记过的那个原文写法补成该卡别名（幂等，见 ``_append_prop_alias``），
    并计入 ``known``——不新建卡、不发模型调用。"""
    for alias in {base, label}:
        if alias and alias != canonical and _append_prop_alias(conn, project_id, canonical, alias):
            known.add(alias)
    known.add(canonical)


def bind_existing_prop_alias(conn: sqlite3.Connection, project_id: str, canonical_name: str, alias: str) -> bool:
    """供 ``app.props.card_pending_ensure``（分镜阶段补卡，label 命中既有卡时
    只登记别名、不新建卡）复用同一条别名登记路径，不得另写一份——与
    ``ensure_props_for_labels`` 内部调用 ``_append_prop_alias`` 的语义完全一致。
    """
    return _append_prop_alias(conn, project_id, canonical_name, alias)


async def register_prop_card_for_label(
    conn: sqlite3.Connection, project_id: str, episode_no: int, label: str, description: str,
    *, style: str, ep_label: str, allowed_aliases: frozenset[str],
) -> dict | None:
    """供 ``app.props.card_pending_ensure``（分镜阶段补卡）新建一张道具卡：
    复用 ``_register_one_prop`` 同一条写入路径（模型写外观锚点 + 出图 + 世界书
    + ``prop_references`` 登记），不另起一份建卡逻辑。``description`` 已由
    调用方把本集跨段的全部描述 + 原文原句拼好，这里原样传给
    ``assess_prop_appearance`` 的 ``description`` 入参。

    ``allowed_aliases``（2026-10-03，必传、无默认值——CLAUDE.md「Ownership
    Must Be Explicit」）：调用方须传本集分镜 ``resources.props`` 里实际出现过
    的全部 label 集合（见 ``app.props.card_pending_scan.label_shot_occurrences``），
    模型提议的别名只有落在这个集合里才登记，见 ``_register_one_prop`` 的过滤
    说明与案情。"""
    return await _register_one_prop(
        conn, project_id, episode_no, {"label": label, "description": description},
        style=style, ep_label=ep_label, allowed_aliases=allowed_aliases,
    )


async def _register_one_prop(
    conn: sqlite3.Connection, project_id: str, episode_no: int, mention: dict,
    *, style: str, ep_label: str, allowed_aliases: frozenset[str] | None,
) -> dict | None:
    """``allowed_aliases``（必传、无默认值）：``None`` 表示调用方明确选择不收紧
    （映射台 ``ensure_props_for_labels`` 既有行为，别名来自模型申报即登记，不
    改动），非 ``None`` 时表示调用方（分镜阶段补卡）要求别名必须能在数据里
    找到依据——只保留落在该集合里的别名，其余按 CLAUDE.md「不得兜底填充」丢弃
    并记日志，不默默吞掉（真实事故：模型把"椅子""杯子""毛衫"这类只剩品类名的
    泛称报成别名，登记后以后任何一集提到同品类的另一件东西都会错误复用这张卡
    的参考图）。"""
    label = str(mention.get("label") or "").strip()
    verdict = await assess_prop_appearance(
        label, str(mention.get("description") or ""), style=style, ep_label=ep_label,
    )
    aliases = verdict["aliases"]
    if allowed_aliases is not None:
        dropped = [a for a in aliases if a not in allowed_aliases]
        if dropped:
            log.info(
                "[PROP_STORYBOARD_CARD_ALIAS_DROPPED] label=%s 丢弃别名=%s"
                "（本集分镜未把它们用作任何道具的 label，不采信）",
                label, dropped,
            )
        aliases = [a for a in aliases if a in allowed_aliases]
    prop = Prop(
        name=label, appearance_canonical=verdict["appearance_canonical"],
        aliases=aliases, first_episode_no=episode_no,
    )
    if not _append_prop_to_bible(conn, project_id, prop):
        return None  # 并发下已被抢先登记（重读会看到别的调用刚写入的同名道具），不重复建
    image_path = await _generate_and_persist_prop_image(conn, project_id, episode_no, prop, style=style)
    await _audit_new_card_now(project_id, label)
    return {"name": label, "has_image": bool(image_path)}


async def _audit_new_card_now(project_id: str, prop_name: str) -> None:
    """新卡创建后立即按现行规则复核一次（触发点①，2026-10-03）：提示词规则
    ≠生效（同类教训见 CLAUDE.md「分镜正文复核后处理」），新卡落库后立刻核
    一遍能当场纠正模型没完全照提示词写的外观/别名。两条建卡路径（映射台
    ``ensure_props_for_labels`` 与分镜补卡 ``register_prop_card_for_label``）
    都走本函数所在的 ``_register_one_prop``，复核只需接这一处。

    同步等待（不是后台 fire-and-forget）：复核是一次文本模型调用（CLAUDE.md
    「文本免费但视频有额度」——不占视频生成额度，只有延迟成本），同步跑完能
    保证卡创建返回时复核记录已经是 ``ready``/``failed``，避免
    ``app.props.card_audit_ensure`` 的生成前懒复核把"刚建好、还没来得及后台
    复核"的新卡误判成需要再拦一轮 409——否则用户建完卡立刻点生成会撞上一条
    自己制造的"正在复核"提示。失败只记日志，不影响建卡本身已经成功（复核
    记录会落 ``failed``，按既有重试额度机制处理，不会无限期挡住生成）。"""
    # 函数内导入：card_audit 拉入 card_audit_rules 的模型调用契约，只有真正建卡
    # 成功这一刻才需要它，避免 service 模块加载期就背上这条链。
    from .card_audit import audit_one_prop_card
    try:
        await audit_one_prop_card(project_id, prop_name, dry_run=False)
    except Exception:  # noqa: BLE001 - 复核失败不影响新卡已经建成这件事
        log.exception("[PROP_CARD_AUDIT_AFTER_CREATE_FAILED] project=%s prop=%s", project_id, prop_name)


def _prop_mention_skip_reason(
    label: str, known: set[str], mention: dict, source_text: str,
) -> str | None:
    """这条申报候选在进入归一/绑卡流程前被跳过的原因，供 ``_log_prop_registry_
    summary`` 的可观测性聚合；返回 ``None`` 表示应当继续处理，不跳过。"""
    if not label:
        return "缺少 label"
    if label in known:
        return "已在世界书登记（name/alias 逐字比对命中）"
    if not is_key_prop_mention(mention, source_text=source_text):
        return "未过结构判据 judge.is_key_prop_mention"
    return None


def _log_prop_registry_summary(
    candidate_count: int, added: list[dict], bound_existing: int, skip_reasons: dict[str, int],
) -> None:
    """``[PREP_PACK_PROP_REGISTRY_SUMMARY]`` 固定前缀集计日志（2026-10-01，用户
    反馈"分镜台道具大多没图"三路只读调查第④项新增可观测性）：本集 ``ensure_
    props_for_labels`` 一次调用处理了多少候选、新建了多少张卡、归并进了多少张
    既有卡、因何种原因在判定阶段被跳过——不设任何数量上限、不拦截任何候选，
    纯观测，供人工核查申报候选为什么没有变成可用道具卡。"""
    log.info(
        "[PREP_PACK_PROP_REGISTRY_SUMMARY] 候选=%s 新建卡=%s 归到既有卡=%s "
        "未过判定跳过=%s 跳过原因分布=%s",
        candidate_count, len(added), bound_existing, sum(skip_reasons.values()), skip_reasons,
    )


async def ensure_props_for_labels(
    project_id: str, episode_no: int, mentions: list[dict], *, source_text: str = "",
    cards_with_prior_evidence: frozenset[str],
) -> dict:
    """反应式道具库登记，供映射台（episode_prep_pack）在 props 抽取完成后调用。

    ``cards_with_prior_evidence``（2026-10-01，必传、无默认值——CLAUDE.md
    「Ownership Must Be Explicit」；本函数是 ``match_existing_prop_card`` 的
    两个直接调用方之一，见 app.props.card_match 模块 docstring 完整案情）：
    调用方须用与 ``app.production.prep_pack.chunking._prep_pack_known_prop_
    names`` 同一份数据源算好（``_prep_pack_props_with_prior_appearance_
    evidence``）再传进来，原样透传给 ``match_existing_prop_card``。

    对每个未登记道具（按 name/alias 逐字比对世界书 ``props``）：先过结构判据
    （``judge.is_key_prop_mention``，不发模型调用），够格才写模型评估
    ``appearance_canonical``/``aliases``、追加进世界书、出一张定物图、登记
    ``prop_references``。人物谱尚未初始化（``bible_json`` 为空）时视为"道具库
    暂不可用"而非错误——道具库是人物/场景库之外的增量能力，不应该反过来挡住
    映射台本身（调用方按约定 advisory 处理，见 app.production.prep_pack.
    discovery._discover_new_props）。

    归一后的每个物件本体（``base``）先对照既有卡片（``card_match.
    match_existing_prop_card``：唯一胜者、包含关系判据，同
    ``app.production.prep_pack.discovery`` 清单构建共用的那一份）——命中就
    只登记别名，不新建卡（真实事故：「行李箱」第2集被模型报成新标签「旧
    行李箱」，旧逻辑按 label 精确比对，当成全新道具建了第二张卡）。

    喂给 ``match_existing_prop_card`` 的证据面收窄到每条 ``mention`` 自己声明
    的 ``segment_indexes`` 对应原文（``card_match.evidence_text_for_segments``），
    不是整集 ``source_text``——2026-09-30 修复评审实测：早先这里传整集原文，
    只要某张既有卡片的 name/alias 恰好出现在本集任意无关段落且与当前 label
    存在包含关系，就会被当成"证据"命中，把两个不相关的道具静默合并成一张
    卡（且因为只命中 1 张卡，不触发 PROP_CARD_MATCH_AMBIGUOUS 告警，完全
    静默）。窄证据与 ``app.production.prep_pack.discovery.
    _prep_pack_build_prop_manifest`` 用的范围严格对齐——同一条提及两处不再
    可能得出不同结论。
    """
    # 建表必须先于本函数下面任何一次写（mutate_bible_json 的 UPDATE）执行：
    # SQLite 连接一旦在某个事务里做过写操作，同一事务内的读写会锁定在写操作
    # 开始那一刻的 schema 快照上，看不到之后由另一条独立连接提交的 CREATE
    # TABLE——实测复现：先读 bible、mutate_bible_json 里的 UPDATE 隐式开事务，
    # 再到 upsert_prop_reference 内部才 lazy 建表时，同一个 conn 报
    # "no such table: prop_references"。提前到这里、且早于任何 conn 读写，
    # 保证 conn 第一次真正用到这张表时 schema 已经落地。
    ensure_schema()
    conn = get_conn()
    bible = _load_bible(conn, project_id)
    if bible is None:
        return {"added": [], "errors": []}
    known = _known_prop_names(bible.props)
    style = bible.world.visual_style_canonical
    ep_label = f"第 {episode_no} 集"
    # 每条 mention 各自的窄证据都从同一份 segments 切片（见函数 docstring 的
    # 证据面对齐说明）；source_text 为空时 index_source_segments 会抛错，所以
    # 只在非空时才算——空 source_text 的调用方（旧测试夹具）本就没有证据可言。
    segments = index_source_segments(source_text) if source_text else []
    added: list[dict] = []
    errors: list[str] = []
    bound_existing = 0
    skip_reasons: dict[str, int] = {}
    for mention in mentions:
        label = str(mention.get("label") or "").strip()
        reason = _prop_mention_skip_reason(label, known, mention, source_text)
        if reason:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
            continue
        evidence_text = evidence_text_for_segments(segments, mention.get("segment_indexes") or [])
        # source_wording/known_prop_name（2026-09-30）：跟 discovery.py 那侧传同一个
        # mention 字段，见 app.props.card_match 模块 docstring「两个调用点必须传同一份」。
        source_wording = str(mention.get("source_wording") or "").strip()
        nominated_card = str(mention.get("known_prop_name") or "").strip()
        # 标签先归一成物件本体（「两只野鸡」→野鸡、「凝灵丹与半块灵石」→凝灵丹+灵石，见 props.labels）：
        # 本体已登记（精确命中，或模型提名+代码核验/卡名包含关系命中既有卡，见 _bind_known_base）
        # 就只补别名，不再另建一件；两者都不成立才以本体名建卡、原标签作别名。
        for base in normalize_prop_label(label):
            canonical = base if base in known else None
            if canonical is None:
                card = match_existing_prop_card(
                    base, evidence_text, bible.props,
                    source_wording=source_wording, nominated_card=nominated_card,
                    cards_with_prior_evidence=cards_with_prior_evidence,
                )
                canonical = card.name if card else None
            if canonical is not None:
                _bind_known_base(conn, project_id, label, base, canonical, known)
                bound_existing += 1
                continue
            try:
                # allowed_aliases=None：映射台既有行为不收紧别名（本次改动只收紧分镜阶段
                # 补卡 register_prop_card_for_label 这一条路径，见该函数与 _register_one_prop
                # 的 docstring）——显式传 None 而不是省略参数，避免日后有人以为漏传。
                result = await _register_one_prop(
                    conn, project_id, episode_no, {**mention, "label": base}, style=style, ep_label=ep_label,
                    allowed_aliases=None,
                )
            except Exception as exc:  # noqa: BLE001 单个道具登记失败不影响其它道具继续
                errors.append(f"{label}：道具库登记失败：{exc}")
                continue
            if result:
                added.append(result)
                known.add(base)
                if base != label and _append_prop_alias(conn, project_id, base, label):
                    known.add(label)
    _log_prop_registry_summary(len(mentions), added, bound_existing, skip_reasons)
    return {"added": added, "errors": errors}


def props_for_project(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """道具库列表（API 用）：name/appearance/aliases/image_path/status。"""
    bible = _load_bible(conn, project_id)
    if bible is None:
        return []
    items = []
    for prop in bible.props:
        row = latest_prop_reference_status(conn, project_id, prop.name)
        items.append({
            "name": prop.name,
            "appearance": prop.appearance_canonical,
            "aliases": list(prop.aliases),
            "image_path": prop.ref_image_path,
            "status": (row["status"] if row else ("ready" if prop.ref_image_path else "failed")),
        })
    return items


async def regenerate_prop_reference(project_id: str, name: str) -> dict:
    """重生成某道具的参考图（API 用）；道具不在世界书里时抛 ``ValueError``。"""
    ensure_schema()  # 建表必须先于下面 mutate_bible_json 的写，见 ensure_props_for_labels 同一注释
    conn = get_conn()
    bible = _load_bible(conn, project_id)
    prop = next((p for p in (bible.props if bible else []) if p.name == name), None)
    if prop is None:
        raise ValueError(f"道具不存在：{name}")
    episode_no = prop.first_episode_no or 1
    image_path = await _generate_and_persist_prop_image(conn, project_id, episode_no, prop, style=bible.world.visual_style_canonical)
    return {"name": name, "status": "ready" if image_path else "failed", "image_path": image_path}
