"""场景状态图：按分镜段落「场景状态与场景卡是否一致」连续段分组、算指纹、查状态。

背景（2026-10-02，用户拍板「场景状态图」）：``app.video_modes.scene_state_
selection.resolve_scene_reference_entry`` 在本段 ``scene_state_matches_card``
为 ``no``/``unsure`` 时省略场景卡参考图（卡片是默认状态，发了会把灾后场景画成
日常）。《顾念长安》EP1 第 15-19 段（出租屋被水淹）因此完全没有场景参考，视频
模型每次凭文字编出不同环境。这里补一张"这个场景此刻这个状态"的状态图：以场景
卡主图为种子图图生图，供同一段状态连续的所有镜头复用。

``group_scene_state_runs`` 是生成（``scene_state_ensure`` 扫描整集需求）与装配
（本模块 ``resolve_scene_state_view_for_shot``，供 ``app.video_modes.
scene_state_assembly`` 调用）共用的唯一分组函数——两侧判据必须是同一份代码，
不会漂移（CLAUDE.md「指纹单一来源」同一思路）。纯函数，不做任何 I/O：是否
"场景卡有 ready 主图"由调用方算好、以 ``ready_scene_reference_ids`` 集合传入，
分组逻辑本身只读 ``shot_rows`` 已解析出的 ``storyboard_pack_segment.resources.
scenes[]``。

分组规则：按 ``shot_no`` 顺序遍历；同一 ``scene_reference_id`` 连续出现
``scene_state_matches_card`` ∈ {no, unsure} 的段落组成一个"状态段串"——中间不
提这个场景的段落不打断（没触碰 ``open_runs`` 里这个 key，下一次提到时继续用
同一个串）；该场景出现 ``matches_card`` 为 ``yes`` 或空字符串（存量未评估，不是
新证据，按 CLAUDE.md「不得兜底填充」不能当成"确认不一致"）都结束当前串，之后
再出现 no/unsure 另起新串。串的状态描述取串内第一段该场景条目的 ``description``
（为空则依次取下一段非空的）；``state_key`` 由 ``scene_reference_id`` + 串首段
``shot_no`` + 描述结构归一化后的哈希前 16 位决定——串首 ``shot_no`` 入指纹是为了
让同一场景在同一集里出现两段不同的"水退了又涨了"状态串时取到不同 key，不会
因为描述文字偶然相同而被误判成同一个状态。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.props.text_match import matched_props_in_text
from app.video_modes.scene_state_views_store import get_scene_state_view

# 2026-10-05 状态图提示词新增道具卡外观陈述（见 prop_appearance_notes_for_
# description）时没有涨版本号：只有描述里点到道具卡名的状态图提示词变了，它们
# 的指纹会因为 material 里多出非空的 prop_appearance_notes 而变化、自动重画；
# 没点到任何卡的状态图提示词逐字不变，指纹 material 也逐字不变（空字符串不写进
# material），不应该因为这次规则变化被全部判过期重画——涨版本号会让所有项目的
# 全部状态图白白重出一遍，并在重出完成前挡住各自的视频生成。
PROMPT_VERSION = "v1"


def normalize_state_description(text: str) -> str:
    """结构归一化：合并连续空白、去首尾空白与句末标点。不做同义改写或词表
    替换——与 ``app.video_modes.character_look_views.normalize_look_key_text``
    同一类"只做结构归一、不做语义归并"的处理。"""
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    return collapsed.rstrip("。.!！?？，,、；;：:")


def state_key_for(scene_reference_id: str, start_shot_no: int, description: str) -> str:
    material = f"{scene_reference_id}:{start_shot_no}:{normalize_state_description(description)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def scene_state_input_fingerprint(
    *, scene_reference_id: str, establishing_image_path: str, description: str, visual_style: str,
    prop_appearance_notes: str, prop_state_notes: str,
) -> str:
    """状态图「这张图该长什么样」的期望指纹——生成（``scene_state_ensure.
    claim_or_get``）与扫描/装配（本模块）必须调用同一份计算。种子图路径变了
    （场景卡重新生成拿到新文件）、画风/描述变了，或者本段命中的道具卡外观文字
    变了（``prop_appearance_notes``，见 ``prop_appearance_notes_for_description``），
    或者上一段同场景的道具位置/状态文字变了（``prop_state_notes``，见
    ``app.video_modes.scene_state_prop_states``），指纹都要跟着变，否则旧状态图
    会被当成仍然正确而永远不重出。"""
    fields: dict[str, str] = {
        "scene_reference_id": scene_reference_id,
        "establishing_image_path": establishing_image_path,
        "description": normalize_state_description(description),
        "visual_style": visual_style,
        "version": PROMPT_VERSION,
    }
    if prop_appearance_notes:
        # 只在命中道具卡时写进 material：没命中的状态图提示词与引入本字段前
        # 逐字相同，指纹也必须逐字相同，存量图不该因此被判过期（见 PROMPT_VERSION 注释）。
        fields["prop_appearance_notes"] = prop_appearance_notes
    if prop_state_notes:
        # 同上同一纪律：没有可取的上一段道具位置/状态时这段文字是空字符串，
        # 不写进 material，存量状态图的指纹不受本次新增维度影响。
        fields["prop_state_notes"] = prop_state_notes
    material = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def prop_appearance_notes_for_description(description: str, props: list[Any]) -> str:
    """状态描述原文里逐字出现的道具卡，各追加一句正面陈述，告诉图像模型外观
    按卡画、位置与状态仍按描述走（2026-10-05，《顾念长安》EP1 真实故障：场景
    状态图把「绿萝」画成酒红陶盆、「鞋柜」画成高木柜，压过了道具卡外观）。
    文本里没提到的道具不提——判据从这段描述本身推导，不是维护一张道具名单。
    """
    matched = matched_props_in_text(description, props)
    sentences = [
        f"画面里的「{prop.name}」外观（颜色、材质、款式）按道具卡画："
        f"{prop.appearance_canonical}；它此刻的位置与状态按上面的描述。"
        for prop in matched
    ]
    return " ".join(sentences)


def _segment_from_shot_row(row: Any) -> dict[str, Any] | None:
    """从 ``shots`` 表一行解析出 ``storyboard_pack_segment`` 字典；旧分镜（没有
    这个字段）或解析失败返回 ``None``。"""
    try:
        raw = row["shot_contract_json"]
    except (IndexError, KeyError):
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    segment = data.get("storyboard_pack_segment")
    return segment if isinstance(segment, dict) else None


def segment_props_for_shot_row(row: Any) -> list[dict[str, Any]]:
    """本段 ``continuity_memo.props[]``（见 ``app.production.storyboard_
    continuity_memo._AiPropState``：``name``/``form``/``location``/``state``）；
    没有分镜段、没有备忘或解析失败返回空列表——供
    ``app.video_modes.scene_state_prop_states`` 取"上一段结束时道具在哪、是
    什么状态"用。"""
    segment = _segment_from_shot_row(row)
    if not segment:
        return []
    memo = segment.get("continuity_memo") or {}
    return [p for p in (memo.get("props") or []) if isinstance(p, dict)]


def scene_entries_for_shot(row: Any) -> list[dict[str, Any]]:
    """本段 ``resources.scenes[]``；没有分镜段/解析失败返回空列表。"""
    segment = _segment_from_shot_row(row)
    if not segment:
        return []
    resources = segment.get("resources") or {}
    return [entry for entry in (resources.get("scenes") or []) if isinstance(entry, dict)]


def ready_scene_card_ids(conn: Any, scene_reference_ids: set[str]) -> set[str]:
    """场景卡存在且主图（establishing）文件仍在磁盘上的 ``scene_reference_id``
    集合；状态图分组只对这些场景生效（CLAUDE.md「不得兜底填充」：没有主图就没有
    "默认状态"这个参照系，谈不上"此刻是否偏离默认"）。"""
    ids = {sid for sid in scene_reference_ids if sid}
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, image_path FROM scene_references WHERE id IN ({placeholders})", list(ids),
    ).fetchall()
    return {row["id"] for row in rows if row["image_path"] and Path(row["image_path"]).is_file()}


def _close_run(open_runs: dict[str, dict[str, Any]], finished: list[dict[str, Any]], scene_reference_id: str) -> None:
    run = open_runs.pop(scene_reference_id, None)
    if run is not None:
        finished.append(run)


def group_scene_state_runs(
    shot_rows: list[Any], *, ready_scene_reference_ids: set[str],
) -> list[dict[str, Any]]:
    """按 ``shot_rows``（必须已按 ``shot_no`` 升序）分组出全部状态段串，见模块
    文档。返回每串 ``{scene_reference_id, start_shot_no, shot_nos, description,
    state_key}``。"""
    open_runs: dict[str, dict[str, Any]] = {}
    finished: list[dict[str, Any]] = []
    for row in shot_rows:
        shot_no = int(row["shot_no"])
        for entry in scene_entries_for_shot(row):
            scene_reference_id = str(entry.get("scene_reference_id") or "")
            if scene_reference_id not in ready_scene_reference_ids:
                continue
            matches = str(entry.get("scene_state_matches_card") or "")
            if matches not in ("no", "unsure"):
                _close_run(open_runs, finished, scene_reference_id)
                continue
            description = str(entry.get("description") or "").strip()
            run = open_runs.get(scene_reference_id)
            if run is None:
                run = {
                    "scene_reference_id": scene_reference_id, "start_shot_no": shot_no,
                    "shot_nos": [], "description": description,
                }
                open_runs[scene_reference_id] = run
            elif not run["description"] and description:
                run["description"] = description
            run["shot_nos"].append(shot_no)
    finished.extend(open_runs.values())
    for run in finished:
        run["state_key"] = state_key_for(run["scene_reference_id"], run["start_shot_no"], run["description"])
    return finished


def find_run_for_shot(
    runs: list[dict[str, Any]], scene_reference_id: str, shot_no: int,
) -> dict[str, Any] | None:
    for run in runs:
        if run["scene_reference_id"] == scene_reference_id and shot_no in run["shot_nos"]:
            return run
    return None


def scene_state_view_status(
    conn: Any, *, scene_reference_id: str, episode_id: str, state_key: str, expected_fingerprint: str,
) -> dict[str, Any]:
    """归一化状态图当前状态，供整集扫描/GET 状态接口展示。``ready`` 要求文件
    仍在磁盘上且指纹匹配，否则按 ``missing``/``stale`` 处理——与 ``app.video_
    modes.character_look_views.look_view_status`` 同一套取舍。"""
    view = get_scene_state_view(conn, scene_reference_id=scene_reference_id, episode_id=episode_id, state_key=state_key)
    if not view:
        return {"status": "missing", "image_path": None, "error": None}
    status = view.get("status")
    if status == "ready":
        path = view.get("image_path")
        if not path or not Path(path).is_file():
            return {"status": "missing", "image_path": None, "error": None}
        if view.get("input_fingerprint") != expected_fingerprint:
            return {"status": "stale", "image_path": path, "error": None}
        return {"status": "ready", "image_path": path, "error": None}
    if status in ("running", "failed"):
        return {"status": status, "image_path": None, "error": view.get("error")}
    return {"status": "missing", "image_path": None, "error": None}


def load_episode_shot_rows(conn: Any, episode_id: str) -> list[Any]:
    return conn.execute(
        "SELECT id, shot_no, shot_contract_json FROM shots WHERE episode_id=? ORDER BY shot_no",
        (episode_id,),
    ).fetchall()


def scan_episode_scene_state_needs(
    *, conn: Any, bible: Any, project_id: str, episode_id: str, shot_rows: list[Any],
) -> list[dict[str, Any]]:
    """遍历整集分镜，解析出每一处"需要状态图"的需求与当前状态。只读，不触发
    生成——生成由 ``scene_state_ensure.ensure_scene_state_views`` 调用本函数拿到
    缺口后编排；GET 状态接口直接复用本函数，保证两处判据同源。"""
    all_scene_ids = {
        str(entry.get("scene_reference_id") or "")
        for row in shot_rows for entry in scene_entries_for_shot(row)
    }
    ready_ids = ready_scene_card_ids(conn, all_scene_ids)
    runs = group_scene_state_runs(shot_rows, ready_scene_reference_ids=ready_ids)
    visual_style = bible.world.visual_style_canonical
    # 函数内导入：app.video_modes.scene_state_prop_states 依赖本模块的
    # scene_entries_for_shot/segment_props_for_shot_row/matched_props_in_text，
    # 模块级互相 import 会形成循环（本模块是数据层，新模块是建在它之上的
    # 派生功能），推到调用时才解析即可破环，不影响任何行为。
    from app.video_modes.scene_state_prop_states import character_display_names_from_bible, prop_state_notes_for_run

    character_display_names = character_display_names_from_bible(bible)
    items: list[dict[str, Any]] = []
    for run in runs:
        card = conn.execute(
            "SELECT scene_name, image_path FROM scene_references WHERE id=?", (run["scene_reference_id"],),
        ).fetchone()
        if not card:
            continue
        establishing_path = str(card["image_path"] or "")
        prop_notes = prop_appearance_notes_for_description(run["description"], bible.props)
        prop_state_notes = prop_state_notes_for_run(
            shot_rows=shot_rows, description=run["description"], start_shot_no=run["start_shot_no"],
            scene_reference_id=run["scene_reference_id"], props=bible.props,
            character_display_names=character_display_names,
        )
        fingerprint = scene_state_input_fingerprint(
            scene_reference_id=run["scene_reference_id"], establishing_image_path=establishing_path,
            description=run["description"], visual_style=visual_style, prop_appearance_notes=prop_notes,
            prop_state_notes=prop_state_notes,
        )
        state = scene_state_view_status(
            conn, scene_reference_id=run["scene_reference_id"], episode_id=episode_id,
            state_key=run["state_key"], expected_fingerprint=fingerprint,
        )
        items.append({
            "project_id": project_id, "episode_id": episode_id,
            "scene_reference_id": run["scene_reference_id"], "scene_name": str(card["scene_name"] or ""),
            "state_key": run["state_key"], "description": run["description"], "shot_nos": list(run["shot_nos"]),
            "establishing_image_path": establishing_path, "fingerprint": fingerprint,
            "prop_appearance_notes": prop_notes, "prop_state_notes": prop_state_notes,
            "status": state["status"], "image_path": state["image_path"], "error": state["error"],
        })
    return items


def resolve_scene_state_view_for_shot(
    *, conn: Any, episode_id: str, shot_no: int, scene_reference_id: str,
    establishing_image_path: str, visual_style: str, props: list[Any], character_display_names: set[str],
) -> dict[str, Any] | None:
    """装配期（``app.video_modes.scene_state_assembly``）只读查询：本段所属的
    状态段串若有 ready 且指纹匹配的状态图，返回该行；否则 ``None``，调用方据此
    保持现有省略行为。``conn`` 必填：装配期身处调用方自己的事务连接里。``props``
    是本集 Bible 的道具卡列表（``bible.props``），用来算与扫描/生成同一份的
    ``prop_appearance_notes``；必传——漏传会让装配期算出不带道具外观的指纹，与
    扫描/生成侧对不上，状态图永远判不中。``character_display_names`` 同理必传，
    供 ``scene_state_prop_states`` 过滤"在人手里/穿在身上"的道具条目。"""
    ready_ids = ready_scene_card_ids(conn, {scene_reference_id})
    if scene_reference_id not in ready_ids:
        return None
    shot_rows = load_episode_shot_rows(conn, episode_id)
    runs = group_scene_state_runs(shot_rows, ready_scene_reference_ids=ready_ids)
    run = find_run_for_shot(runs, scene_reference_id, shot_no)
    if run is None:
        return None
    # 函数内导入：理由同 scan_episode_scene_state_needs 上方那处。
    from app.video_modes.scene_state_prop_states import prop_state_notes_for_run

    prop_notes = prop_appearance_notes_for_description(run["description"], props)
    prop_state_notes = prop_state_notes_for_run(
        shot_rows=shot_rows, description=run["description"], start_shot_no=run["start_shot_no"],
        scene_reference_id=scene_reference_id, props=props, character_display_names=character_display_names,
    )
    fingerprint = scene_state_input_fingerprint(
        scene_reference_id=scene_reference_id, establishing_image_path=establishing_image_path,
        description=run["description"], visual_style=visual_style, prop_appearance_notes=prop_notes,
        prop_state_notes=prop_state_notes,
    )
    view = get_scene_state_view(conn, scene_reference_id=scene_reference_id, episode_id=episode_id, state_key=run["state_key"])
    if not view or view.get("status") != "ready" or view.get("input_fingerprint") != fingerprint:
        return None
    path = view.get("image_path")
    return view if path and Path(path).is_file() else None
