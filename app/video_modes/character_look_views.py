"""人物造型照：按段 wardrobe 文本生成的「同一个人、换了本段这身衣服」正面全身照。

背景（2026-10-02）：Seedance 对写实画风的人物参考图做真人检测，单张大头近景、以及
刚上线替代它的头像九宫格（``app.multiview`` 的 ``face_closeup``/``character_
headshot_grid``）都被判定 ``InputImageSensitiveContentDetected.PrivacyInformation``
拒收；正面全身定妆照（脸在画面里很小）此前 142 次全部放行。现行
``app.video_modes.character_look_selection.pick_character_reference_view`` 在本段
``wardrobe_matches_default != "yes"``（本段穿着不是人物谱默认造型）时退回头像九宫格
——这条退路在写实项目里等于凡是换装段落都发不出去。

本模块是这个问题的数据层与解析层：以人物当前定妆照（``character_portraits.
image_path``，即 front_full）为种子图图生图，按本段 ``continuity_memo.characters[].
wardrobe`` 文本单独产出一张「脸/发型/体型沿用种子图、服装按本段文字」的正面全身照，
供 ``pick_character_reference_view`` 在非默认造型段优先选用，替代九宫格。生成与
并发编排见同包 ``character_looks_ensure.py``（拆开的理由：本模块只做查询/归一化/
需求解析的纯函数与简单 CRUD，生成那边有 I/O 与并发状态机，两者混在一个文件里会
超过单文件 500 行与单函数 50 行的红线）。

``look_key``：对 wardrobe 文本做结构归一化（合并连续空白、去首尾空白与句末标点）
后取 sha256 前 16 位——**不做任何词表替换**（CLAUDE.md「判据从数据推导，禁止黑
白名单」：不能靠猜哪些词是「衣服词」来决定要不要重新生成，文本变了就是变了）。
同一人物同一句 wardrobe 文本只生成一次，``UNIQUE(portrait_id, look_key)``。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.video_modes.character_look_views_store import get_look_view


def normalize_look_key_text(wardrobe_text: str) -> str:
    """结构归一化：合并连续空白、去首尾空白与句末标点。不做同义改写或词表替换——
    与 ``app.production.storyboard_continuity_memo._normalize_for_quote_match``
    同一类「只做结构归一、不做语义归并」的处理，但这里额外去掉句末标点，因为
    同一件衣服的描述经常只差一个句号/顿号，不应该被当成两套不同造型各生成一张图。
    """
    collapsed = re.sub(r"\s+", " ", wardrobe_text or "").strip()
    return collapsed.rstrip("。.!！?？，,、；;：:")


def look_key_for_wardrobe(wardrobe_text: str) -> str:
    normalized = normalize_look_key_text(wardrobe_text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def identity_id_core(identity_id: str) -> str:
    """与 ``app.production.storyboard_continuity_memo._identity_id_core`` 同一归一
    （去掉 ``bible:``/``entity:`` 前缀取主体），本模块独立声明一份而不是跨模块导入
    私有符号——两处都是「身份前缀集合是封闭的两种」这个数据事实的直接推导，不是
    会漂移的业务判断，重复一行比引入跨模块私有依赖更安全。"""
    return identity_id.split(":", 1)[-1].strip() if identity_id else identity_id


def segment_character_wardrobe(segment: dict[str, Any], identity_id: str) -> str:
    """从本段 ``continuity_memo.characters[]`` 里取这个人物的 wardrobe 文本；
    找不到或字段为空返回空串。``segment`` 是 ``shots.shot_contract_json`` 解析出的
    ``storyboard_pack_segment`` 字典（见 ``app.production.storyboard_pack``）。"""
    core = identity_id_core(identity_id)
    memo = segment.get("continuity_memo") or {}
    for item in memo.get("characters") or []:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("identity_id") or "")
        if candidate == identity_id or identity_id_core(candidate) == core:
            return str(item.get("wardrobe") or "").strip()
    return ""


def segment_from_shot_row(row: Any) -> dict[str, Any] | None:
    """从 ``shots`` 表一行解析出 ``storyboard_pack_segment`` 字典；旧分镜（2.x 之前，
    没有这个字段）或解析失败返回 ``None``，调用方据此跳过（旧分镜没有 continuity_memo
    可用，本来就无法判断本段 wardrobe，不是需要报错的异常状态）。"""
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


def ready_look_view(conn: Any, portrait_id: str, look_key: str) -> dict[str, Any] | None:
    """供 ``app.multiview._storyboard_pack_asset_dependencies`` 装配期只读查询：
    ``conn`` 必填、不留默认值——调用方身处自己的事务连接里，道理与
    ``app.portraits.current_ref.current_portrait_ref`` 的 ``conn`` 形参说明相同。
    """
    view = get_look_view(conn, portrait_id, look_key)
    if not view or view.get("status") != "ready":
        return None
    path = view.get("image_path")
    if not path or not Path(path).is_file():
        return None
    return view


def look_view_status(conn: Any, portrait_id: str, look_key: str) -> dict[str, Any]:
    """归一化造型照当前状态，供整集扫描/GET 状态接口展示。``ready`` 要求文件仍在
    磁盘上，否则按「相当于没生成过」处理（``missing``）——调用方
    （``character_looks_ensure.ensure_character_looks``）会把 ``missing`` 当缺口
    重新生成，与 ``ready_look_view`` 对装配期的从严判据一致，只是这里额外区分
    ``running``/``failed`` 两态用于界面展示。"""
    view = get_look_view(conn, portrait_id, look_key)
    if not view:
        return {"status": "missing", "image_path": None, "error": None}
    status = view.get("status")
    if status == "ready":
        path = view.get("image_path")
        if path and Path(path).is_file():
            return {"status": "ready", "image_path": path, "error": None}
        return {"status": "missing", "image_path": None, "error": None}
    if status in ("running", "failed"):
        return {"status": status, "image_path": None, "error": view.get("error")}
    return {"status": "missing", "image_path": None, "error": None}


def resolve_segment_look_view(
    *, conn: Any, segment: dict[str, Any], identity_id: str, portrait_id: str | None,
    usable: bool, wardrobe_matches_default: str,
) -> dict[str, Any] | None:
    """装配期（``app.multiview._storyboard_pack_asset_dependencies``）查询本段这位
    人物的 ready 造型照；人物没有可用定妆照、或本段就是默认造型、或本段没有可判断
    的 wardrobe 文本（旧分镜/模型漏填）时返回 ``None``，调用方据此退回全身定妆照。
    ``conn`` 必填：装配期身处调用方自己的事务连接里，道理同 ``ready_look_view``。
    """
    if not (usable and portrait_id and wardrobe_matches_default != "yes"):
        return None
    wardrobe_text = segment_character_wardrobe(segment, identity_id)
    if not wardrobe_text:
        return None
    return ready_look_view(conn, portrait_id, look_key_for_wardrobe(wardrobe_text))


def look_fallback_notice(
    name: str, wardrobe_matches_default: str, selected_view: dict[str, Any] | None,
) -> str | None:
    """``selected_view`` 退回全身定妆照（``costume_mode=="neutral"``）且本段并非
    默认造型时，给人工核查留一条可见提示——CLAUDE.md「拦住用户时必须给出路」：
    不只说"可能被带偏"，还要点名怎么补（分镜台「补齐造型照」）。"""
    if not selected_view or wardrobe_matches_default == "yes":
        return None
    if selected_view.get("costume_mode") != "neutral":
        return None
    return f"「{name}」本段造型照未生成，已退回定妆照，服装可能被定妆照带偏；到分镜台点「补齐造型照」"


def resolve_character_look_selection(
    *, conn: Any, segment: dict[str, Any], identity_id: str, portrait_id: str | None,
    front_full_image_path: str, usable: bool, wardrobe_matches_default: str, name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """合并「查造型照→选参考图→算退回提示」三步，返回 ``(selected_view,
    look_notice)``，供 ``app.multiview._storyboard_pack_asset_dependencies`` 一次
    调用。三步各自拆成 ``resolve_segment_look_view``/``pick_character_reference_
    view``/``look_fallback_notice`` 独立函数是为了能分别单测（见
    ``tests/test_character_look_selection.py`` 等）；但 ``app/multiview.py`` 已经
    卡在 ``app/FILE_CONVENTIONS.toml`` 的行数棘轮基线上（零余量），装配期调用点
    只应该留一次函数调用，三步的编排因此收在这里而不是摊开在 multiview.py 里。
    """
    from app.video_modes.character_look_selection import pick_character_reference_view  # 函数内导入：与本包其它互相调用的叶子模块一致，按需导入、不在模块顶层建立双向耦合面

    look_view = resolve_segment_look_view(
        conn=conn, segment=segment, identity_id=identity_id, portrait_id=portrait_id,
        usable=usable, wardrobe_matches_default=wardrobe_matches_default,
    )
    selected_view = pick_character_reference_view(
        wardrobe_matches_default=wardrobe_matches_default, portrait_id=portrait_id,
        front_full_image_path=front_full_image_path, look_view=look_view,
    ) if usable else None
    return selected_view, look_fallback_notice(name, wardrobe_matches_default, selected_view)


def character_portrait_anchor(bible: Any, name: str) -> tuple[str, str | None]:
    """按人物谱正名取外观锚点原文与用户覆盖的定妆照生成词——与
    ``app.refs._generate_one_character_portrait`` 取值同一份数据，供造型照生成
    复用同一个外观真值锚点（脸/发型/体型以它为准，只是本段服装被替换）。未命中
    （理论上不会发生，调用方已先用 ``resolve_card_owner`` 核验过 owner 存在）
    返回空锚点，交调用方按「无可用锚点」处理。"""
    for character in bible.characters:
        if character.name == name:
            return character.appearance_canonical, (character.portrait_prompt_override or None)
    return "", None


def _resolve_one_character_look_need(
    *, conn: Any, bible: Any, project_id: str, episode_no: int,
    segment: dict[str, Any], entry: dict[str, Any], shot_id: str, shot_no: int,
) -> dict[str, Any] | None:
    """判定本段这一位人物是否需要造型照、以及当前状态；不需要（默认造型/无卡/
    无定妆照/本段没写 wardrobe）时返回 ``None``——与
    ``app.multiview._storyboard_pack_asset_dependencies`` 的可见性/卡归属判据
    （``resolve_card_owner``/``subject_kind``）同一套，避免两处判据漂移。
    """
    from app.portraits.card_owner import resolve_card_owner  # 延迟导入：避免 video_modes 包初始化期对 app.portraits 产生不必要的模块级耦合面
    from app.portraits.current_ref import current_portrait_ref

    identity_id = str(entry.get("identity_id") or "")
    if not identity_id or entry.get("visibility") == "voice_only" or identity_id == "旁白":
        return None
    if str(entry.get("wardrobe_matches_default") or "") == "yes":
        return None
    name = str(entry.get("display_name") or identity_id_core(identity_id) or identity_id)
    card_label = name if identity_id.startswith("entity:") else identity_id_core(identity_id)
    owner_kind, owner = resolve_card_owner(bible, card_label)
    if owner_kind != "owner" or entry.get("subject_kind") in {"extra", "crowd"}:
        return None
    current = current_portrait_ref(
        project_id, str(owner), episode_no, visual_entity_id=identity_id, conn=conn,
    )
    if not current:
        return None
    wardrobe = segment_character_wardrobe(segment, identity_id)
    if not wardrobe:
        return None
    key = look_key_for_wardrobe(wardrobe)
    state = look_view_status(conn, current["portrait_id"], key)
    appearance, portrait_prompt = character_portrait_anchor(bible, str(owner))
    return {
        "shot_id": shot_id, "shot_no": shot_no, "identity_id": identity_id, "character_name": name,
        "portrait_id": current["portrait_id"], "front_full_image_path": current["image_path"],
        "appearance": appearance, "portrait_prompt": portrait_prompt,
        "wardrobe_text": wardrobe, "look_key": key,
        "status": state["status"], "image_path": state["image_path"], "error": state["error"],
    }


def scan_episode_character_look_needs(
    *, conn: Any, bible: Any, project_id: str, episode_no: int, shot_rows: list[Any],
) -> list[dict[str, Any]]:
    """遍历目标段的 resources.characters，解析出每一处「需要造型照」的需求与当前
    状态。只读，不触发生成——生成由 ``character_looks_ensure.ensure_character_
    looks`` 调用本函数拿到 ``status=="missing"`` 的条目后编排；GET 状态接口直接
    复用本函数，保证两处判据是同一份代码，不会漂移。
    """
    items: list[dict[str, Any]] = []
    for row in shot_rows:
        segment = segment_from_shot_row(row)
        if not segment:
            continue
        resources = segment.get("resources") or {}
        for entry in resources.get("characters") or []:
            item = _resolve_one_character_look_need(
                conn=conn, bible=bible, project_id=project_id, episode_no=episode_no,
                segment=segment, entry=entry, shot_id=row["id"], shot_no=row["shot_no"],
            )
            if item is not None:
                items.append(item)
    return items
