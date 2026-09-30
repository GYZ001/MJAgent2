"""道具库反应式登记编排：判据 → 模型写外观锚点 → 出图 → 落 world bible + prop_references。

与 ``app.scenes`` 的分工镜像：判据/出图/落库拆到 ``judge.py``/``image.py``/
``store.py``，本文件只做编排，单函数体量照顾 CLAUDE.md 的「单函数 ≤50 代码行」。
"""
from __future__ import annotations

import json
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


async def _register_one_prop(
    conn: sqlite3.Connection, project_id: str, episode_no: int, mention: dict,
    *, style: str, ep_label: str,
) -> dict | None:
    label = str(mention.get("label") or "").strip()
    verdict = await assess_prop_appearance(
        label, str(mention.get("description") or ""), style=style, ep_label=ep_label,
    )
    prop = Prop(
        name=label, appearance_canonical=verdict["appearance_canonical"],
        aliases=verdict["aliases"], first_episode_no=episode_no,
    )
    if not _append_prop_to_bible(conn, project_id, prop):
        return None  # 并发下已被抢先登记（重读会看到别的调用刚写入的同名道具），不重复建
    image_path = await _generate_and_persist_prop_image(conn, project_id, episode_no, prop, style=style)
    return {"name": label, "has_image": bool(image_path)}


async def ensure_props_for_labels(
    project_id: str, episode_no: int, mentions: list[dict], *, source_text: str = "",
) -> dict:
    """反应式道具库登记，供映射台（episode_prep_pack）在 props 抽取完成后调用。

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
    for mention in mentions:
        label = str(mention.get("label") or "").strip()
        if not label or label in known:
            continue
        if not is_key_prop_mention(mention, source_text=source_text):
            continue
        evidence_text = evidence_text_for_segments(segments, mention.get("segment_indexes") or [])
        # 标签先归一成物件本体（「两只野鸡」→野鸡、「凝灵丹与半块灵石」→凝灵丹+灵石，见 props.labels）：
        # 本体已登记（精确命中，或对照既有卡片的包含关系唯一胜者，见 _bind_known_base）
        # 就只补别名，不再另建一件；两者都不成立才以本体名建卡、原标签作别名。
        for base in normalize_prop_label(label):
            canonical = base if base in known else None
            if canonical is None:
                card = match_existing_prop_card(base, evidence_text, bible.props)
                canonical = card.name if card else None
            if canonical is not None:
                _bind_known_base(conn, project_id, label, base, canonical, known)
                continue
            try:
                result = await _register_one_prop(
                    conn, project_id, episode_no, {**mention, "label": base}, style=style, ep_label=ep_label,
                )
            except Exception as exc:  # noqa: BLE001 单个道具登记失败不影响其它道具继续
                errors.append(f"{label}：道具库登记失败：{exc}")
                continue
            if result:
                added.append(result)
                known.add(base)
                if base != label and _append_prop_alias(conn, project_id, base, label):
                    known.add(label)
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
