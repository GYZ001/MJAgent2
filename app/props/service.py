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

from .image import generate_prop_reference_image, prop_ref_prompt
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
    added: list[dict] = []
    errors: list[str] = []
    for mention in mentions:
        label = str(mention.get("label") or "").strip()
        if not label or label in known:
            continue
        if not is_key_prop_mention(mention, source_text=source_text):
            continue
        try:
            result = await _register_one_prop(
                conn, project_id, episode_no, mention, style=style, ep_label=ep_label,
            )
        except Exception as exc:  # noqa: BLE001 单个道具登记失败不影响其它道具继续
            errors.append(f"{label}：道具库登记失败：{exc}")
            continue
        if result:
            added.append(result)
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
