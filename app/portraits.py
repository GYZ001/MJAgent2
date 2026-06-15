"""人物定妆照按集分段刷新（跨集一致性增强，PRD §5.4 第 2 层的时间维扩展）。

每满 config.PORTRAIT_REFRESH_INTERVAL 集为一段。对每个角色，从该段对应章节里抽取提及该角色的
原文片段，交给模型判断外观相比【当前定妆照】是否发生明显视觉变化：
  - 变化不大 → 沿用当前定妆照（其适用集为开区间，自然向后覆盖），不重绘、不花钱；
  - 变化很大 → 关闭当前定妆照右区间（= 本段起点-1），并以当前定妆照为底【图生图】重绘新定妆照，
    左区间 = 本段起点、右区间开放。

定妆照写入 character_portraits 表（ep_start/ep_end/base_portrait_id）。评审墙出视频时按集号选用
覆盖该集的定妆照（见 app.refs.refs_as_image_inputs / video_modes.character_reference_assets）。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

from pydantic import ValidationError

from app import config, hiagent
from app.db import get_conn, get_setting, new_id, now, rows_to_dicts, set_setting
from app.refs import _safe_name, portrait_prompt
from app.schemas import Bible, Character, extract_json

FRAGMENT_WINDOW = 220   # 命中角色名前后各取多少字
FRAGMENT_BUDGET = 4000  # 单角色单段送审片段总字数预算
APPEARANCE_MIN = 30     # 外观锚点串下限（与 validate_bible 一致）
APPEARANCE_MAX = 80     # 外观锚点串上限


def _processed_through_key(project_id: str) -> str:
    return f"portrait_block_done:{project_id}"


# ---------- 原文片段抽取（纯本地，不调模型） ----------

def extract_character_fragments(text: str, name: str, *, window: int = FRAGMENT_WINDOW,
                                budget: int = FRAGMENT_BUDGET) -> str:
    """从正文里抽取提及 name 的片段（命中处前后 window 字），合并重叠区间，封顶 budget 字。"""
    if not name or not text:
        return ""
    spans: list[tuple[int, int]] = []
    for m in re.finditer(re.escape(name), text):
        spans.append((max(0, m.start() - window), min(len(text), m.end() + window)))
    if not spans:
        return ""
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out: list[str] = []
    used = 0
    for s, e in merged:
        if used >= budget:
            break
        piece = text[s:e].strip()[: max(0, budget - used)]
        if piece:
            out.append(piece)
            used += len(piece)
    return "\n……\n".join(out)


# ---------- 外观变化判定（调模型） ----------

async def judge_appearance_change(name: str, current_appearance: str, fragments: str,
                                  ep_range_label: str) -> dict:
    """问模型：相比当前定妆照外观锚点，这一段原文里该角色外观是否明显变化。
    返回 {changed: bool, new_appearance: str, reason: str}。"""
    prompt = f"""任务：判断小说人物「{name}」的【外观】在新一段剧情（{ep_range_label}）里相比既有定妆照是否发生【明显视觉变化】。

既有定妆照外观锚点（当前画的样子）：
{current_appearance or '（无）'}

新一段原文中提及「{name}」的片段：
{fragments or '（本段未明显提及该角色）'}

判断口径（只看会改变定妆照画面的外观要素）：
- 算明显变化：发型/发色大改、换了标志性服装造型、明显变老或变小、增加显著外观标记（疤痕/义眼/纹身/残肢等）、整体形象转变（如落魄→华服、人→异化形态）。
- 不算明显变化：表情、姿态、临时脏污/受伤、光线、心情、所处场景，以及原文本段没有正面描写其外观时。
- 没有把握时一律判为未明显变化（changed=false），避免无意义重绘。

若 changed=true，请给出整合后的【新外观锚点串】new_appearance：40~60 字，沿用既有锚点未变部分，只改真正变化处；保留性别年龄感/发型发色/服装款式与颜色/标志性特征。

只输出一个 JSON 对象：{{"changed": true/false, "new_appearance": "", "reason": "一句话依据"}}"""
    raw = await hiagent.chat([{"role": "user", "content": prompt}], temperature=0.2, max_tokens=600)
    obj = extract_json(raw)
    changed = bool(obj.get("changed"))
    new_appearance = (obj.get("new_appearance") or "").strip()
    if changed and not new_appearance:
        changed = False  # 说变了却没给新锚点 → 保守沿用，不重绘
    return {"changed": changed, "new_appearance": new_appearance,
            "reason": (obj.get("reason") or "").strip()}


# ---------- 新角色发现（剧本阶段反应式：按需检索原文判断戏份，够分量才建卡） ----------
#
# 设计：人物谱只在进项目时谱写一次；之后由剧本阶段触发——剧本里出现、人物谱里没有的名字，
# 向后检索若干章原文判断戏份，画面够多才单独建卡 + 定妆。必须在【分镜展开前】完成，
# 否则 validate_storyboard 会因"角色圣经中不存在"把新角色从分镜里刷掉。

DISCOVERY_FORWARD_CHAPTERS = 20   # 判断戏份时，从本集所在章节再往后检索多少章原文
DISCOVERY_REJUDGE_WINDOW = 20     # 判过"戏份不足"的名字，隔多少集才重新评估一次（避免对龙套反复调模型）

# 同名角色卡的建卡互斥锁（逐集分镜并行时，两集可能同时发现同一新角色）。
_card_locks: dict[tuple[str, str], asyncio.Lock] = {}
_card_locks_guard = asyncio.Lock()


async def _card_lock(project_id: str, name: str) -> asyncio.Lock:
    async with _card_locks_guard:
        key = (project_id, name)
        lock = _card_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _card_locks[key] = lock
        return lock


def _discovery_skip_key(project_id: str, name: str) -> str:
    return f"char_discovery_skip:{project_id}:{name}"


def _name_in_bible(conn, project_id: str, name: str) -> bool:
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    return any((c.get("name") or "") == name for c in json.loads(row["bible_json"]).get("characters", []))


def _forward_fragments(conn, project_id: str, name: str, from_episode_no: int) -> tuple[str, str]:
    """取本集所在章节起、向后 DISCOVERY_FORWARD_CHAPTERS 章的原文，抽出提及 name 的片段。"""
    ep = conn.execute(
        "SELECT source_chapters FROM episodes WHERE project_id=? AND episode_no=?",
        (project_id, from_episode_no)).fetchone()
    src = json.loads(ep["source_chapters"] or "[]") if ep and ep["source_chapters"] else []
    lo, hi = (min(src), max(src)) if src else (0, 0)
    rows = conn.execute(
        "SELECT content FROM chapters WHERE project_id=? AND idx>=? AND idx<=? ORDER BY idx",
        (project_id, lo, hi + DISCOVERY_FORWARD_CHAPTERS)).fetchall()
    text = "\n".join((r["content"] or "") for r in rows)
    return extract_character_fragments(text, name), f"第 {from_episode_no} 集相关章节 +{DISCOVERY_FORWARD_CHAPTERS} 章"


async def assess_new_character(name: str, fragments: str, *, style: str,
                               known_names: list[str], ep_label: str) -> dict:
    """针对一个【具体名字】判断是否值得单独建卡（戏份够 / 画面多），并产出角色卡字段。
    返回 {important, reason, role, appearance_canonical, personality, speech_style, relationships}。"""
    known = "、".join(known_names) or "（无）"
    prompt = f"""任务：判断小说角色「{name}」是否值得【单独建人物卡并定妆】（用作漫剧出镜的一致性锚点）。

已有角色（若「{name}」其实是这些人的别名/外号/尊称，则 important=false）：
{known}

下面是原文中提及「{name}」的片段（{ep_label}）：
{fragments[:12000]}

判定口径：
- important=true 仅当：「{name}」是【真正的新角色】，且在这段剧情里【反复出场 / 有正面戏份 / 画面感强】，值得稳定其外观。
- important=false：路人、只被提及一两次、纯功能性提及，或其实是已有角色的别名/外号/尊称。
- appearance_canonical 是"固定外观锚点串"：40~60 字，须含 性别年龄感/发型发色/服装款式与颜色/1 个标志性特征；只写视觉可见信息，不写性格。原著未写处按画风（{style}）合理补全并保持内部一致。

只输出一个 JSON 对象：
{{"important": true/false, "reason": "一句话依据", "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}]}}"""
    raw = await hiagent.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=900)
    obj = extract_json(raw)
    important = bool(obj.get("important"))
    appearance = (obj.get("appearance_canonical") or "").strip()
    if len(appearance) > APPEARANCE_MAX:
        appearance = appearance[:APPEARANCE_MAX]
    if important and len(appearance) < APPEARANCE_MIN:
        important = False  # 外观太稀薄不足以稳定定妆 → 不建卡
    known_set = set(known_names)
    # 只保留指向【已知角色】且 relation 非空的关系；Relationship.to/relation 必填，漏 relation 会让校验崩。
    rels = [
        {"to": r["to"], "relation": str(r.get("relation") or "").strip()}
        for r in (obj.get("relationships") or [])
        if isinstance(r, dict) and r.get("to") in known_set and str(r.get("relation") or "").strip()
    ]
    return {
        "important": important,
        "reason": (obj.get("reason") or "").strip(),
        "role": (obj.get("role") or "重要配角").strip() or "重要配角",
        "appearance_canonical": appearance,
        "personality": (obj.get("personality") or "").strip(),
        "speech_style": (obj.get("speech_style") or "").strip(),
        "relationships": rels,
    }


async def ensure_character_card(project_id: str, name: str, from_episode_no: int) -> dict:
    """确保「name」在人物谱里有卡：已有→直接返回；没有→向后检索原文判断戏份，够分量才补卡 + 定妆
    （出图失败仍补卡，按集选图时回退到无该角色参考图）。带 (project,name) 锁，幂等可并发。
    返回 {status: exists|added|skipped_minor|skipped|error, name, ...}。"""
    name = (name or "").strip()
    if not name:
        return {"status": "skipped", "reason": "empty"}
    conn = get_conn()
    if _name_in_bible(conn, project_id, name):
        return {"status": "exists", "name": name}
    lock = await _card_lock(project_id, name)
    async with lock:
        if _name_in_bible(conn, project_id, name):  # 拿到锁后复查（并发兜底）
            return {"status": "exists", "name": name}
        # 负缓存：近 DISCOVERY_REJUDGE_WINDOW 集内判过"戏份不足"就先不重判；隔得够远会重新评估
        # （龙套后期可能转重要）。
        skip_raw = get_setting(_discovery_skip_key(project_id, name))
        if skip_raw:
            try:
                last = int(skip_raw)
            except (TypeError, ValueError):
                last = 0
            if 0 < from_episode_no - last < DISCOVERY_REJUDGE_WINDOW:
                return {"status": "skipped_minor", "name": name, "reason": "recently judged minor"}
        project = conn.execute("SELECT bible_json, bible_version FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project or not project["bible_json"]:
            return {"status": "skipped", "name": name, "reason": "no bible"}
        bible = Bible.model_validate(json.loads(project["bible_json"]))
        style = bible.world.visual_style_canonical
        known = [c.name for c in bible.characters]
        fragments, ep_label = _forward_fragments(conn, project_id, name, from_episode_no)
        if not fragments:
            # 原文里根本检索不到这个名字（多半是剧本臆造/称谓）→ 记负缓存、不建卡
            set_setting(_discovery_skip_key(project_id, name), str(from_episode_no))
            return {"status": "skipped_minor", "name": name, "reason": "no fragments in novel"}
        try:
            verdict = await assess_new_character(name, fragments, style=style, known_names=known, ep_label=ep_label)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "name": name, "reason": str(exc)[:240]}
        if not verdict["important"]:
            set_setting(_discovery_skip_key(project_id, name), str(from_episode_no))
            return {"status": "skipped_minor", "name": name, "reason": verdict["reason"]}
        try:
            char_obj = Character.model_validate({
                "name": name, "role": verdict["role"],
                "appearance_canonical": verdict["appearance_canonical"],
                "personality": verdict["personality"], "speech_style": verdict["speech_style"],
                "relationships": verdict["relationships"], "portrait_prompt_override": None})
        except ValidationError as exc:
            return {"status": "error", "name": name, "reason": f"card invalid {exc}"[:240]}
        bible_version = project["bible_version"] or 0
        # 出图失败也要补卡（重试一次吸收瞬时失败）：定妆照适用集从 from_episode_no 起。
        new_path = new_prompt = None
        for attempt in range(2):
            try:
                new_path, new_prompt = await _generate_fresh_portrait(
                    project_id, name, style, char_obj.appearance_canonical, ep_start=from_episode_no)
                break
            except Exception:  # noqa: BLE001
                continue
        if new_path:
            char_obj.ref_image_path = new_path
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("portrait"), project_id, name, from_episode_no, None, char_obj.appearance_canonical,
                 new_prompt, new_path, None, bible_version, now()))
            conn.commit()
        _append_character_to_bible(conn, project_id, char_obj.model_dump())
        set_setting(_discovery_skip_key(project_id, name), "")  # 已建卡，清掉历史负缓存
        return {"status": "added", "name": name, "has_portrait": bool(new_path), "reason": verdict["reason"]}


async def ensure_cards_for_screenplay(project_id: str, episode_no: int, screenplay, bible) -> dict:
    """剧本就绪后：把剧本里出现、人物谱里没有、且戏份够的角色补进人物谱（在分镜展开前调用）。
    逐个吞错——某个角色发现失败不应阻断分镜。返回 {checked, added:[...]}。"""
    bible_names = {c.name for c in bible.characters}
    names: list[str] = []
    seen: set[str] = set()

    def _collect(lst) -> None:
        for n in lst or []:
            n = (n or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)

    for sc in getattr(screenplay, "scene_outline", None) or []:
        _collect(getattr(sc, "characters", None))
    for b in getattr(screenplay, "beats", None) or []:
        _collect(getattr(b, "characters", None))

    unknown = [n for n in names if n not in bible_names]
    added: list[dict] = []
    for n in unknown:
        try:
            res = await ensure_character_card(project_id, n, episode_no)
        except Exception as exc:  # noqa: BLE001
            res = {"status": "error", "name": n, "reason": str(exc)[:200]}
        if res.get("status") == "added":
            added.append(res)
    return {"checked": len(unknown), "added": added}


# ---------- 定妆照落盘 / 登记 ----------

async def _save_image_item(item: dict, dest: str) -> None:
    """把 hiagent.generate_image 的返回落盘到 dest（url 优先下载，其次写 b64）。"""
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        with open(dest, "wb") as f:
            f.write(base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def _portrait_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _new_portrait_path(project_id: str, name: str, ep_start: int) -> str:
    return str(_portrait_dir(project_id) / f"{_safe_name(name)}__ep{ep_start}.jpg")


def register_initial_portrait(conn, project_id: str, name: str, image_path: str,
                              appearance: str, prompt: str, bible_version: int) -> None:
    """初次定妆后登记角色首张定妆照（适用集 1~ 至今）。覆盖式：先清掉该角色全部旧分段。"""
    conn.execute("DELETE FROM character_portraits WHERE project_id=? AND character_name=?",
                 (project_id, name))
    conn.execute(
        "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
        "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("portrait"), project_id, name, 1, None, appearance, prompt, image_path, None, bible_version, now()))
    conn.commit()


def reset_processed_blocks(project_id: str) -> None:
    """重新全量定妆 / 画风变更后调用：清空"已处理到第几集"，让下次刷新从第二段重新判定。"""
    set_setting(_processed_through_key(project_id), "0")


def _open_portrait(conn, project_id: str, name: str):
    """该角色当前开区间（ep_end IS NULL）的最新定妆照。"""
    return conn.execute(
        "SELECT * FROM character_portraits WHERE project_id=? AND character_name=? AND ep_end IS NULL "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, name)).fetchone()


def portrait_for_episode(project_id: str, name: str, episode_no: int | None) -> str | None:
    """返回覆盖该集的定妆照落盘路径；未命中返回 None（调用方回退到 bible.ref_image_path）。"""
    if episode_no is None:
        return None
    row = get_conn().execute(
        "SELECT image_path FROM character_portraits "
        "WHERE project_id=? AND character_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY ep_start DESC LIMIT 1",
        (project_id, name, episode_no, episode_no)).fetchone()
    if row and row["image_path"] and Path(row["image_path"]).exists():
        return row["image_path"]
    return None


def redraw_prompt(style: str, appearance: str) -> str:
    """图生图重绘提示词：以参考图（旧定妆照）为身份锚点，只按新外观调整。"""
    return (
        f"{style}。参考图是同一角色的既有定妆照，请在保持【同一个人、同一角色身份】的前提下，"
        f"按新外观重绘其全身定妆照：{appearance}。"
        "正面站立，中性表情，双臂自然下垂，纯浅米色背景，全身完整可见，无文字无水印"
    )


async def _redraw_portrait(project_id: str, name: str, style: str, appearance: str,
                           *, base_path: str | None, ep_start: int) -> tuple[str, str]:
    """以上一张定妆照为底【图生图】重绘新定妆照，落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = redraw_prompt(style, appearance)
    image_inputs = None
    if base_path and Path(base_path).exists():
        image_inputs = [hiagent.data_url_from_file(base_path)]
    item = await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE, image_inputs=image_inputs)
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt


async def _generate_fresh_portrait(project_id: str, name: str, style: str, appearance: str,
                                   *, ep_start: int) -> tuple[str, str]:
    """为新登场角色生成一张全新定妆照（无底图，不走图生图），落盘。返回 (落盘路径, 生成 prompt)。"""
    prompt = portrait_prompt(style, appearance)
    item = await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE)
    dest = _new_portrait_path(project_id, name, ep_start)
    await _save_image_item(item, dest)
    return dest, prompt


def _append_character_to_bible(conn, project_id: str, char: dict) -> None:
    """把新发现的角色追加进 bible_json.characters（按名去重，重读再写以免覆盖并发编辑的其它字段）。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return
    data = json.loads(row["bible_json"])
    if char.get("name") in {c.get("name") for c in data.get("characters", [])}:
        return
    data.setdefault("characters", []).append(char)
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(data, ensure_ascii=False), project_id))
    conn.commit()


# ---------- 主流程：按段刷新 ----------

async def update_portraits_for_blocks(project_id: str) -> dict:
    """按 20 集分段刷新【已有角色】定妆照——只处理外观随剧情明显漂移、需要图生图重绘的情况。
    新角色发现已下放到剧本阶段（ensure_cards_for_screenplay）。幂等：用 setting 记录已处理到第几集。"""
    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        raise ValueError("项目不存在或还没有角色圣经")
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    bible_version = project["bible_version"] or 0
    style = bible.world.visual_style_canonical
    eps = rows_to_dicts(conn.execute(
        "SELECT episode_no, source_chapters FROM episodes WHERE project_id=? ORDER BY episode_no",
        (project_id,)).fetchall())
    if not eps:
        raise ValueError("还没有分集，请先分集后再刷新定妆照")
    last_ep = eps[-1]["episode_no"]
    interval = max(int(config.PORTRAIT_REFRESH_INTERVAL), 1)

    ch_text = {r["idx"]: (r["content"] or "")
               for r in conn.execute("SELECT idx, content FROM chapters WHERE project_id=?", (project_id,)).fetchall()}

    def block_source_text(lo: int, hi: int) -> str:
        idxs: list[int] = []
        for e in eps:
            if lo <= e["episode_no"] <= hi:
                for i in json.loads(e["source_chapters"] or "[]"):
                    if i not in idxs:
                        idxs.append(i)
        return "\n".join(ch_text.get(i, "") for i in idxs)

    done_key = _processed_through_key(project_id)
    processed_through = int(get_setting(done_key) or 0)

    changes: list[str] = []
    errors: list[str] = []
    # 第一段（1~interval）由初始定妆照覆盖、不判定；从第二段起逐段处理，已处理过的段跳过。
    block_start = max(interval + 1, processed_through + 1)
    while block_start <= last_ep:
        block_end = min(block_start + interval - 1, last_ep)
        label = f"第 {block_start}~{block_end} 集"
        src = block_source_text(block_start, block_end)
        # ① 已有角色：判断外观是否大变（大变才图生图重绘并切分适用集）
        for c in list(bible.characters):
            cur = _open_portrait(conn, project_id, c.name)
            if not cur:
                continue  # 该角色尚无定妆照（未定妆）→ 跳过
            fragments = extract_character_fragments(src, c.name)
            if not fragments:
                continue  # 本段没提到该角色 → 沿用，开区间自然覆盖
            try:
                verdict = await judge_appearance_change(
                    c.name, cur["appearance"] or c.appearance_canonical, fragments, label)
            except Exception as exc:  # noqa: BLE001 单角色判定失败不拖垮整段
                errors.append(f"{c.name}@{label}：判定失败 {exc}")
                continue
            if not verdict["changed"]:
                continue
            try:
                new_path, new_prompt = await _redraw_portrait(
                    project_id, c.name, style, verdict["new_appearance"],
                    base_path=cur["image_path"], ep_start=block_start)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{c.name}@{label}：重绘失败 {exc}")
                continue
            # 外观明显变化：关闭当前段右区间，登记图生图重绘的新段。
            conn.execute("UPDATE character_portraits SET ep_end=? WHERE id=?", (block_start - 1, cur["id"]))
            conn.execute(
                "INSERT INTO character_portraits(id, project_id, character_name, ep_start, ep_end, appearance, "
                "prompt, image_path, base_portrait_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("portrait"), project_id, c.name, block_start, None, verdict["new_appearance"],
                 new_prompt, new_path, cur["id"], bible_version, now()))
            conn.commit()
            changes.append(f"{c.name}：{label} 外观明显变化，已图生图重绘（{verdict['reason']}）")

        # 注：新角色发现已移到剧本阶段反应式处理（见 ensure_cards_for_screenplay / ensure_character_card），
        # 本段只负责"已有角色外观随剧情漂移"的重绘，不再在这里扫描新角色。
        set_setting(done_key, str(block_end))
        block_start = block_end + 1

    return {"changes": changes, "errors": errors, "blocks_through": int(get_setting(done_key) or 0)}
