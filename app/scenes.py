"""场景图素材库工作流（跨集场景一致性的核心，与 app.refs/app.portraits 同构）。

场景圣经定稿后为每个规范场景生成 Seedream 定场图，存入 projects/<id>/scene_refs/，并登记到
scene_references（按"适用集区间"分段，ep_end=NULL 表示开区间=当前最新版）。生成镜头/关键帧时，
按 shot.scene_name 取覆盖该集的场景图，作为 scene 型参考图注入——同一场景的所有镜头、所有集
都吃同一张场景图 → 整片场景一致。

两条产生路径（完全复刻 app.portraits 的角色定妆照机制）：
  ① 初始批量：generate_scene_refs（场景圣经定稿后，适用集 1~ 至今）。
  ② 分镜阶段反应式发现：ensure_scenes_for_storyboard——剧本里出现、场景库里没有、够戏份的
     新场景 → 评估后补进 bible.scenes + 出图，适用集从首次出场那集起开放。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from app import config, hiagent
from app.errors import code_ref
from app.db import get_conn, new_id, now
from app.refs import _safe_name
from app.schemas import Bible, Scene, extract_json
from app.validators import match_scene_name

SCENE_CANONICAL_MIN = 30
SCENE_CANONICAL_MAX = 80

# 初始场景图只覆盖前 N 章的场景（按钮批量出图的范围）；更靠后才出现的新场景留到分镜阶段反应式补图。
SCENE_BIBLE_CHAPTER_WINDOW = 20


# ---------- 落盘 / 提示词 ----------

def _scene_dir(project_id: str) -> Path:
    d = config.PROJECTS_DIR / project_id / "scene_refs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def scene_ref_path(project_id: str, scene_name: str, ep_start: int | None = None) -> str:
    suffix = f"__ep{ep_start}" if ep_start else ""
    return str(_scene_dir(project_id) / f"{_safe_name(scene_name)}{suffix}.jpg")


def scene_ref_prompt(visual_style: str, scene_canonical: str) -> str:
    """场景定场图生成词：纯环境、无人物，作为跨集复用的场景锚点。"""
    return (
        f"{visual_style}。场景定场图（环境为主、画面中不出现任何人物）：{scene_canonical}。"
        "9:16 竖屏，构图完整的环境定场镜头，空间纵深清晰，光影与色调统一，电影质感，高清，"
        "无人物，无文字，无字幕，无水印，无 logo"
    )


async def _save_image_item(item: dict, dest: str) -> None:
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        with open(dest, "wb") as f:
            f.write(base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


def same_scene_anchor(conn, project_id: str, name: str) -> str | None:
    """该场景【自己】已落盘的最新一张图路径，作为同名场景跨集/重出时的 i2i 锚点；无则 None。

    只在同名场景内部参考——绝不跨场景。Seedream 的参考图是【全图 i2i】，会把锚点的构图与陈设整体带过来：
    同一地点（如某场景跨集演化、或重出微调）拿自己的旧图当锚点能保持一致；但拿【别的场景】的图当锚点
    会把那个场景的石碑/围栏等带进来导致撞图，是错的。"""
    rows = conn.execute(
        "SELECT image_path FROM scene_references WHERE project_id=? AND scene_name=? "
        "ORDER BY ep_start DESC, id DESC", (project_id, name)).fetchall()
    for r in rows:
        if r["image_path"] and Path(r["image_path"]).exists():
            return r["image_path"]
    return None


async def _generate_scene_image(prompt: str, anchor_url: str | None = None, *,
                                call_meta: dict | None = None) -> dict:
    """出一张场景图。anchor_url 仅用于【同场景】的 i2i 锚点（由 same_scene_anchor 取该场景自己的旧图），
    绝不传别的场景的图。带参考图失败则回退纯文生图（与 generate_image 文档约定一致）。"""
    if anchor_url:
        try:
            return await hiagent.generate_image(
                prompt, size=config.REF_IMAGE_SIZE, image_inputs=[anchor_url], call_meta=call_meta)
        except Exception:  # noqa: BLE001 带参考图失败 → 不带重试
            pass
    return await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE, call_meta=call_meta)


async def _review_scene_ref(image_path: str, scene: "Scene | dict") -> dict:
    """复用 stages.review_scene_image 对场景图做 QA（无人物，锚点传空）。失败时返回保守满分，不阻断。"""
    from app.stages import review_scene_image
    name = scene["name"] if isinstance(scene, dict) else scene.name
    canonical = scene["scene_canonical"] if isinstance(scene, dict) else scene.scene_canonical
    try:
        return await review_scene_image(
            hiagent.encode_image_file(image_path), canonical, name, [], kind="head")
    except Exception:  # noqa: BLE001 QA 失败不阻断入库（与定妆照一致：图能用就用）
        return {"overall": 1.0, "issues": ["qa_skipped"]}


# ---------- scene_references 分段表读写（对照 app.portraits） ----------

def register_initial_scene_ref(conn, project_id: str, name: str, image_path: str,
                               scene_canonical: str, prompt: str, qa: dict, bible_version: int) -> None:
    """初次出图后登记场景图（适用集 1~ 至今）。覆盖式：先清掉该场景全部旧分段。"""
    conn.execute("DELETE FROM scene_references WHERE project_id=? AND scene_name=?", (project_id, name))
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, scene_canonical, "
        "prompt, image_path, qa_json, base_scene_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("scene"), project_id, name, 1, None, scene_canonical, prompt, image_path,
         json.dumps(qa, ensure_ascii=False), None, bible_version, now()))
    conn.commit()


def scene_ref_exists(conn, project_id: str, name: str) -> bool:
    """该场景是否已有一张落盘可用的场景图（已登记 scene_references 且文件还在）。
    批量出图据此跳过已生成的场景，使按钮可幂等重复点击。"""
    rows = conn.execute(
        "SELECT image_path FROM scene_references WHERE project_id=? AND scene_name=?",
        (project_id, name)).fetchall()
    return any(r["image_path"] and Path(r["image_path"]).exists() for r in rows)


def _open_scene_ref(conn, project_id: str, name: str):
    return conn.execute(
        "SELECT * FROM scene_references WHERE project_id=? AND scene_name=? AND ep_end IS NULL "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, name)).fetchone()


def scene_ref_for_episode(project_id: str, name: str, episode_no: int | None) -> str | None:
    """返回覆盖该集的场景图落盘路径；未命中返回 None。"""
    if not name:
        return None
    ep = episode_no if episode_no is not None else 1
    row = get_conn().execute(
        "SELECT image_path FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, name, ep, ep)).fetchone()
    if row and row["image_path"] and Path(row["image_path"]).exists():
        return row["image_path"]
    return None


def scene_ref_qa_for_episode(project_id: str, name: str, episode_no: int | None) -> dict | None:
    if not name:
        return None
    ep = episode_no if episode_no is not None else 1
    row = get_conn().execute(
        "SELECT qa_json FROM scene_references "
        "WHERE project_id=? AND scene_name=? AND ep_start<=? AND (ep_end IS NULL OR ep_end>=?) "
        "ORDER BY ep_start DESC LIMIT 1", (project_id, name, ep, ep)).fetchone()
    if row and row["qa_json"]:
        try:
            return json.loads(row["qa_json"])
        except (TypeError, ValueError):
            return None
    return None


def scene_refs_as_image_inputs(bible: Bible, scene_names: list[str], limit: int,
                               *, project_id: str | None = None,
                               episode_no: int | None = None) -> list[tuple[str, str]]:
    """规范场景名 →(data_url, "reference_image") 列表，最多 limit 张。
    优先 scene_references 按集分段图；未命中回退 bible.scenes 的 ref_image_path。"""
    out: list[tuple[str, str]] = []
    by_name = {s.name: s for s in (getattr(bible, "scenes", None) or [])}
    seen: set[str] = set()
    for name in scene_names:
        if len(out) >= max(limit, 0):
            break
        if not name or name in seen:
            continue
        seen.add(name)
        path = scene_ref_for_episode(project_id, name, episode_no) if project_id else None
        if not path:
            sc = by_name.get(name)
            path = getattr(sc, "ref_image_path", None) if sc else None
        if path and Path(path).exists():
            try:
                out.append((hiagent.data_url_from_file(path), "reference_image"))
            except OSError:
                continue
    return out


# ---------- 初始批量出图 ----------

async def generate_scene_refs(project_id: str, only_scene: str | None = None) -> None:
    """为项目全部（或指定）场景生成定场图，写回 bible_json 的 scenes[*].ref_image_path。"""
    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        raise ValueError("项目不存在或还没有角色圣经")
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    if not bible.scenes:
        raise ValueError("还没有场景圣经，请先生成场景清单")
    style = bible.world.visual_style_canonical
    bible_version = project["bible_version"] or 0

    targets = [s for s in bible.scenes if (only_scene is None or s.name == only_scene)]
    if not targets:
        raise ValueError(f"场景不存在：{only_scene}")

    # 批量出图（only_scene=None）：只补还没出过图的场景，已生成的跳过 → 按钮可重复点击而不重复出图。
    # 单场景重做（only_scene 指定）：强制重出，不跳过。
    if only_scene is None:
        targets = [s for s in targets if not scene_ref_exists(conn, project_id, s.name)]
        if not targets:
            return  # 当前场景库里的场景图都已就绪，无需重出

    errors: list[str] = []
    for sc in targets:
        try:
            path = scene_ref_path(project_id, sc.name)
            # 重出=纯文生图，绝不拿该场景【自己的旧图】做 i2i 锚点：旧图可能本就画错/串味（如早期跨场景 i2i
            # 把别的场景的围栏带了进来），自参考会把错误复制下去、越修越像。跨场景一致性交给画风串保证。
            try:
                Path(path).unlink()
            except OSError:
                pass
            sc.ref_image_path = None
            prompt = (sc.scene_prompt_override or "").strip() or scene_ref_prompt(style, sc.scene_canonical)
            item = await _generate_scene_image(
                prompt,
                call_meta={
                    "asset_kind": "scene_reference",
                    "scene_name": sc.name,
                    "episode_no": 1,
                    "scene_ref_mode": "initial",
                })
            await _save_image_item(item, path)
            sc.ref_image_path = path
            qa = await _review_scene_ref(path, sc)
            register_initial_scene_ref(conn, project_id, sc.name, path, sc.scene_canonical,
                                       prompt, qa, bible_version)
        except Exception as exc:  # noqa: BLE001 失败要响：逐场景记录，最后汇总抛出
            errors.append(f"{sc.name}：{exc}")

    conn.execute("UPDATE projects SET bible_json=? WHERE id=?", (bible.model_dump_json(), project_id))
    conn.commit()
    if errors:
        raise RuntimeError("部分场景图失败：" + "；".join(errors)[:600])


# ---------- 分镜阶段反应式发现新场景（对照 portraits.ensure_character_card 的新角色路径） ----------

async def assess_new_scene(label: str, context: str, *, style: str,
                           known_names: list[str], ep_label: str) -> dict:
    """判断剧本里出现、场景库里没有的地点是否值得【单独建场景并出图】，并产出场景字段。
    返回 {important, reason, name, scene_canonical, location_kind}。"""
    known = "、".join(known_names) or "（无）"
    prompt = f"""任务：判断漫剧里出现的地点「{label}」是否值得【加入场景图素材库并单独出一张场景定场图】（用作跨集复用的环境锚点）。

全片画风（场景锚点必须与之一致）：{style}
已有规范场景（若「{label}」其实是这些场景的同一地点/别称，则 important=false）：
{known}

本场景相关剧本上下文（{ep_label}）：
{context[:4000]}

判定口径：
- important=true 仅当：「{label}」是【真正的新地点】，且【反复出现 / 有戏份 / 画面感强】，值得稳定其环境外观。
- important=false：一次性过场、只被提及、或其实是已有场景的同一地点。
- name：稳定的场景短标签（4~10 字），不要与已有场景重名。
- scene_canonical 是"固定场景锚点串"：30~60 字，须含 地点/室内外/光线时段/标志陈设/氛围色调；只写视觉可见的环境信息，不写人物、不写剧情动作。必须贴合画风「{style}」，是 CG/动画/漫画类非真人渲染场景，严禁真人实拍/实景照片描述。

只输出一个 JSON 对象：
{{"important": true/false, "reason": "一句话依据", "name": str, "scene_canonical": str, "location_kind": "室内|室外|其他"}}"""
    raw = await hiagent.chat([{"role": "user", "content": prompt}], temperature=0.3, max_tokens=600)
    obj = extract_json(raw)
    important = bool(obj.get("important"))
    name = (obj.get("name") or "").strip() or label.strip()
    canonical = (obj.get("scene_canonical") or "").strip()
    if len(canonical) > SCENE_CANONICAL_MAX:
        canonical = canonical[:SCENE_CANONICAL_MAX]
    if important and len(canonical) < SCENE_CANONICAL_MIN:
        important = False  # 锚点太稀薄不足以稳定定场 → 不入库
    return {
        "important": important,
        "reason": (obj.get("reason") or "").strip(),
        "name": name,
        "scene_canonical": canonical,
        "location_kind": (obj.get("location_kind") or "其他").strip() or "其他",
    }


def _append_scene_to_bible(conn, project_id: str, scene: dict) -> bool:
    """把新场景追加进 bible_json.scenes（按 name 去重，重读再写以免覆盖并发编辑）。返回是否新增。"""
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return False
    data = json.loads(row["bible_json"])
    if scene.get("name") in {s.get("name") for s in data.get("scenes", [])}:
        return False
    data.setdefault("scenes", []).append(scene)
    conn.execute("UPDATE projects SET bible_json=? WHERE id=?",
                 (json.dumps(data, ensure_ascii=False), project_id))
    conn.commit()
    return True


async def _generate_and_register_scene(project_id: str, name: str, scene_canonical: str,
                                       style: str, *, ep_start: int, bible_version: int) -> str | None:
    """为新场景出一张定场图并登记到 scene_references（适用集 ep_start~ 至今）。出图失败返回 None。"""
    prompt = scene_ref_prompt(style, scene_canonical)
    dest = scene_ref_path(project_id, name, ep_start)
    conn = get_conn()
    # 同场景参考：若该场景已有更早分段的图（同一地点跨集演化），以它做 i2i 锚点保持一致；全新场景则为 None → 纯文生图。
    prior = same_scene_anchor(conn, project_id, name)
    anchor_url = hiagent.data_url_from_file(prior) if prior else None
    try:
        item = await _generate_scene_image(
            prompt,
            anchor_url,
            call_meta={
                "asset_kind": "scene_reference",
                "scene_name": name,
                "episode_no": ep_start,
                "scene_ref_mode": "reactive",
            })
        await _save_image_item(item, dest)
    except Exception:  # noqa: BLE001 出图失败仍入库（文字锚点兜底），按集选图时回退
        return None
    qa = await _review_scene_ref(dest, {"name": name, "scene_canonical": scene_canonical})
    conn.execute(
        "INSERT INTO scene_references(id, project_id, scene_name, ep_start, ep_end, scene_canonical, "
        "prompt, image_path, qa_json, base_scene_id, bible_version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("scene"), project_id, name, ep_start, None, scene_canonical, prompt, dest,
         json.dumps(qa, ensure_ascii=False), None, bible_version, now()))
    conn.commit()
    return dest


def _collect_scene_labels(screenplay) -> list[str]:
    """从剧本场次结构/节拍里收集出现过的地点标签。"""
    labels: list[str] = []
    seen: set[str] = set()

    def _add(v: str) -> None:
        v = (v or "").strip()
        if v and v not in seen:
            seen.add(v)
            labels.append(v)

    for sc in getattr(screenplay, "scene_outline", None) or []:
        _add(getattr(sc, "scene_heading", ""))
    for b in getattr(screenplay, "beats", None) or []:
        _add(getattr(b, "location", ""))
    return labels


async def ensure_scenes_for_storyboard(project_id: str, episode_no: int, screenplay, bible) -> dict:
    """剧本就绪后（分镜展开前）反应式维护场景库：剧本里出现、库里没有、够戏份的新场景 → 评估后
    补进 bible.scenes + 出图，适用集从本集起开放。逐项吞错——单场景失败不阻断分镜。
    返回 {checked, added:[...], errors:[...]}。"""
    scenes = list(getattr(bible, "scenes", None) or [])
    style = bible.world.visual_style_canonical
    conn = get_conn()
    proj = conn.execute("SELECT bible_version FROM projects WHERE id=?", (project_id,)).fetchone()
    bible_version = (proj["bible_version"] if proj else 0) or 0

    labels = _collect_scene_labels(screenplay)
    # 已能映射到库内场景的标签直接跳过；映射结果用场景 summary 作为评估上下文。
    summary_by_heading = {
        (getattr(sc, "scene_heading", "") or "").strip(): (getattr(sc, "summary", "") or "")
        for sc in (getattr(screenplay, "scene_outline", None) or [])
    }
    unmatched = [lb for lb in labels if not match_scene_name(lb, scenes)]

    added: list[dict] = []
    errors: list[str] = []
    for label in unmatched:
        context = f"{label}：{summary_by_heading.get(label, '')}".strip()
        try:
            verdict = await assess_new_scene(
                label, context, style=style, known_names=[s.name for s in scenes],
                ep_label=f"第 {episode_no} 集")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}：评估失败"
                          + code_ref(exc, action="assess_new_scene",
                                     context={"project_id": project_id, "scene": label, "episode_no": episode_no}))
            continue
        if not verdict["important"]:
            continue
        name = verdict["name"]
        # 评估给的 name 可能已映射到库内（同地点别称）→ 跳过；否则入库 + 出图。
        if match_scene_name(name, scenes) or name in {s.name for s in scenes}:
            continue
        new_scene = {"name": name, "scene_canonical": verdict["scene_canonical"],
                     "location_kind": verdict["location_kind"], "ref_image_path": None,
                     "scene_prompt_override": None}
        try:
            path = await _generate_and_register_scene(
                project_id, name, verdict["scene_canonical"], style,
                ep_start=episode_no, bible_version=bible_version)
            new_scene["ref_image_path"] = path
        except Exception as exc:  # noqa: BLE001 出图失败不阻断，文字锚点仍入库
            errors.append(f"{name}@第{episode_no}集出图失败"
                          + code_ref(exc, action="generate_scene_image",
                                     context={"project_id": project_id, "scene": name, "episode_no": episode_no}))
        if _append_scene_to_bible(conn, project_id, new_scene):
            scenes.append(Scene.model_validate(new_scene))
            added.append({"name": name, "reason": verdict["reason"], "has_image": bool(new_scene["ref_image_path"])})

    return {"checked": len(unmatched), "added": added, "errors": errors}
