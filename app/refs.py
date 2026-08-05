"""角色定妆照工作流（人物跨集一致性的核心，PRD §5.4 第 2 层）。

圣经定稿后为每个角色生成 Seedream 全身立绘，存入 projects/<id>/refs/；
生成镜头时，出场角色的定妆照以 base64 data URL 注入 reference_image。
实测结论（2026-06-12）：HiAgent /up/* 文件接口受 CSRF 保护不可程序化调用，
但网关接受 data URL 参考图，故不需要外部托管。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import get_conn, new_id
from app.evidence.media import record_reference_asset
from app.errors import ContentGenerationError
from app.schemas import Bible


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w一-鿿]", "_", name)


def ref_path(project_id: str, character_name: str) -> str:
    d = config.PROJECTS_DIR / project_id / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{_safe_name(character_name)}.jpg")


def normalize_prompt_text(text: str) -> str:
    """生成前规范化：重复标点压缩、完全重复片段去重；保留原始语义强调。"""
    if not text:
        return text
    out = text
    # 重复中英文标点 → 单个
    out = re.sub(r"([。．\.．？！!?,，、；;：:])\1+", r"\1", out)
    out = re.sub(r"([。．\.]){2,}", "。", out)
    # 连续空白
    out = re.sub(r"[ \t]{2,}", " ", out)
    # 完全重复的短句片段（以句号/分号切）去重保序
    parts = re.split(r"(?<=[。；;])", out)
    seen: set[str] = set()
    kept: list[str] = []
    for part in parts:
        key = part.strip()
        if not key:
            kept.append(part)
            continue
        if key in seen and len(key) >= 4:
            continue
        seen.add(key)
        kept.append(part)
    return "".join(kept).strip()


_NON_PRODUCTION_APPEARANCE_RE = re.compile(
    r"(?:乳头|乳晕|乳房|阴部|阴唇|阴蒂|阴毛|生殖器|下体|私处|"
    r"隐私部位|裸体|裸露|赤裸|一丝不挂|内裤|胸罩|文胸)",
    re.IGNORECASE,
)
_CLOTHING_HIDDEN_SKIN_MARK_RE = re.compile(
    r"(?:腰侧|腰部|胸部|腹部|背部|肩部|手臂|上臂|(?:左|右)?臂|大腿|腿部|臀部|髋部|胯部)"
    r"[^，,；;。]*(?:痣|胎记|纹身|疤痕|疤)",
)
_NON_STATIC_APPEARANCE_RE = re.compile(
    r"(?:性格|气度|气场|气质|风情|女人味|书卷气|一举一动|"
    r"看(?:向)?(?:女性|女人|他人)|看人|视线(?:落|停|扫|盯)|"
    r"自带[^，,；;。]*(?:气场|气质|风情|女人味|书卷气)|"
    r"眼神(?:躲闪|游移|贪婪|淫邪|迷离)|"
    r"色欲|算计感|侵略感|迂腐|猥琐|含春|撩人|志在必得)",
)
_NON_NEUTRAL_CLOTHING_RE = re.compile(
    r"(?:露肤|露腰|暴露|低领|深V|透视|镂空|吊带|超短|高开衩|丝袜|网袜|吊袜)",
    re.IGNORECASE,
)
_SEXUALIZED_BODY_EMPHASIS_RE = re.compile(
    r"(?:身材|体型|身体|曲线)[^，,；;。]*(?:丰满|丰腴|性感|凹凸|曲线)",
    re.IGNORECASE,
)
_APPEARANCE_LIST_PREFIX_RE = re.compile(
    r"^(?P<prefix>.*?标志性特征(?:是|为)?)(?P<items>.*)$",
)
_PORTRAIT_CLOTHING_CONTRACT = (
    "常规角色设定图着装，服装面料不透明并完整覆盖身体，"
    "重点呈现面部、发型、外层服装和可见配饰"
)
_PORTRAIT_PRIVACY_SAFE_STYLE = (
    "人物面部与皮肤必须采用明显动画化比例和非照片级卡通渲染材质，"
    "保持虚构角色辨识度但不得生成可误认成真人照片的写实人脸"
)
PRODUCTION_APPEARANCE_MIN_CHARS = 20
PRODUCTION_APPEARANCE_MAX_CHARS = 80


def contains_non_production_appearance(anchor: str) -> bool:
    return bool(
        _NON_PRODUCTION_APPEARANCE_RE.search(anchor or "")
        or _CLOTHING_HIDDEN_SKIN_MARK_RE.search(anchor or "")
        or _NON_STATIC_APPEARANCE_RE.search(anchor or "")
        or _NON_NEUTRAL_CLOTHING_RE.search(anchor or "")
        or _SEXUALIZED_BODY_EMPHASIS_RE.search(anchor or "")
    )


def production_appearance_anchor(anchor: str) -> str:
    """Keep only identity traits that a normally clothed model sheet can prove."""
    clauses = re.split(r"[，,；;。]+", normalize_prompt_text(anchor or ""))
    kept: list[str] = []
    for raw_clause in clauses:
        clause = raw_clause.strip()
        if not clause:
            continue
        if not contains_non_production_appearance(clause):
            kept.append(clause)
            continue
        match = _APPEARANCE_LIST_PREFIX_RE.match(clause)
        prefix = match.group("prefix") if match else ""
        items_text = match.group("items") if match else clause
        items = [
            item.strip()
            for item in re.split(r"(?:[、与和]|及(?!膝))+", items_text)
            if item.strip() and not contains_non_production_appearance(item)
        ]
        if items:
            kept.append(f"{prefix}{'、'.join(items)}")
    return "，".join(kept).strip("， ")


def missing_production_appearance_dimensions(anchor: str) -> list[str]:
    safe = production_appearance_anchor(anchor)
    missing = []
    if not re.search(
        r"(?:岁|成年|男性|女性|男子|女子|男人|女人|青年|中年|老年|少年|少女|老人|人影)",
        safe,
    ):
        missing.append("年龄性别")
    if not re.search(r"(?:发|须|脸|面|眼|眉|肤|身形|身材|体型|高|矮|胖|瘦|形态|人影)", safe):
        missing.append("外形")
    spectral = any(token in safe for token in ("透明", "半透明", "虚影", "魂体", "灵魂", "幽灵", "人影"))
    if not spectral and not re.search(r"(?:穿|衣|衫|裙|裤|鞋|装|袍|服|戴|配饰|手表|眼镜)", safe):
        missing.append("服装配饰")
    return missing


def ensure_portrait_clothing_contract(prompt: str) -> str:
    safe = production_appearance_anchor(prompt)
    if _PORTRAIT_CLOTHING_CONTRACT not in safe:
        safe = f"{safe}。{_PORTRAIT_CLOTHING_CONTRACT}" if safe else _PORTRAIT_CLOTHING_CONTRACT
    return normalize_prompt_text(safe)


def portrait_prompt(visual_style: str, anchor: str) -> str:
    spectral_tokens = ("透明", "半透明", "虚影", "魂体", "灵魂", "幽灵", "悬浮", "漂浮", "人影")
    style = normalize_prompt_text(visual_style or "")
    body = production_appearance_anchor(anchor)
    is_spectral = any(token in body for token in spectral_tokens)
    if is_spectral:
        refinements: list[str] = [
            "这是超自然角色概念设定图，不要套用普通人的站立证件照姿态",
            "锚点指定的非实体形态、悬浮关系和神态优先级最高",
        ]
        if any(token in body for token in ("透明", "半透明", "虚影", "魂体", "灵魂", "幽灵", "人影")):
            refinements.append("身体必须明显半透明，背景能透过身体看见，禁止实体皮肤质感")
        if any(token in body for token in ("悬浮", "漂浮")):
            refinements.append("双脚离地，明确表现悬浮，禁止站在地面")
        if "戒指" in body:
            refinements.append("戒指完整清晰地置于画面底部中央，角色垂直悬浮在戒指正上方")
        if "戏谑" in body:
            refinements.append("嘴角微扬、眼神狡黠，明确表现戏谑，禁止严肃皱眉或中性表情")
        return normalize_prompt_text(
            f"{style}。单角色全身概念定妆设定图：{body}。"
            + "；".join(refinements)
            + "。纯浅米色背景，全身与关联道具完整可见，主体四周保留安全边距。"
              f"{_PORTRAIT_CLOTHING_CONTRACT}。"
              "仅保留锚点明确要求的特效，禁止额外火焰、斗气光环、文字、水印和 logo"
        )
    return normalize_prompt_text(
        f"{style}。全身角色立绘定妆照：{body}。"
        "正面站立，中性表情，双臂自然下垂，纯浅米色背景，全身完整可见。"
        "头顶、肩臂和鞋底均不得贴边或出画，主体四周保留至少 8% 安全边距。"
        f"{_PORTRAIT_CLOTHING_CONTRACT}。{_PORTRAIT_PRIVACY_SAFE_STYLE}。"
        "仅保留锚点明确要求的特效，禁止额外火焰、斗气光环、文字、水印和 logo"
    )


def _merge_generated_portraits(conn, project_id: str, characters) -> None:
    """Merge accepted portrait truth into the latest concurrent Bible snapshot.

    ``portrait_prompt_override`` is allowed to change clothes/hair/body details.
    Once the newly generated pack is accepted, its derived appearance anchor and
    image path must be published together; publishing only the path leaves video
    and keyframe compilation on the previous outfit.
    """
    accepted = {
        item.name: {
            "ref_image_path": item.ref_image_path,
            "appearance_canonical": item.appearance_canonical,
        }
        for item in characters
        if item.ref_image_path
    }
    if not accepted:
        return
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return
    latest = json.loads(row["bible_json"])
    for item in latest.get("characters", []):
        if item.get("name") in accepted:
            published = accepted[item["name"]]
            item["ref_image_path"] = published["ref_image_path"]
            item["appearance_canonical"] = published["appearance_canonical"]
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(latest, ensure_ascii=False), project_id),
    )


_PORTRAIT_APPEARANCE_START_MARKERS = (
    "全身角色立绘定妆照：",
    "全身角色立绘定妆照:",
)
_PORTRAIT_APPEARANCE_STOP_MARKERS = (
    "正面站立",
    "正面全身立绘",
    "中性姿态",
    "中性表情",
    "纯浅米色背景",
    "全身完整可见",
    "头顶、肩臂",
    "主体四周保留",
    "仅保留锚点",
    "禁止额外",
    "生成同一角色多视角",
)


def portrait_appearance_anchor(prompt: str | None, fallback: str = "") -> str:
    """Extract the production appearance anchor from an accepted portrait prompt.

    Portrait prompts often append character-sheet pose/background instructions.
    Those instructions are useful while drawing the model sheet but conflict with
    narrative shots, so downstream video/keyframe prompts receive only the visual
    identity/outfit portion. Free-form prompts without the standard marker remain
    authoritative after the same composition suffixes are stripped.
    """
    fallback_text = production_appearance_anchor(fallback)
    text = normalize_prompt_text(prompt or "").strip()
    if not text:
        return fallback_text
    for marker in _PORTRAIT_APPEARANCE_START_MARKERS:
        if marker in text:
            text = text.split(marker, 1)[1].strip()
            break
    stop_positions = [text.find(marker) for marker in _PORTRAIT_APPEARANCE_STOP_MARKERS if marker in text]
    if stop_positions:
        text = text[:min(stop_positions)].strip()
    text = text.strip(" 。，,;；：:")
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 8 and len(re.sub(r"\s+", "", fallback_text)) > len(compact):
        return fallback_text
    return production_appearance_anchor(text) or fallback_text


async def generate_refs(
    project_id: str,
    only_character: str | None = None,
    *,
    only_characters: list[str] | None = None,
    resume: bool = False,
    fresh_after: float | None = None,
    operation_started_at: float | None = None,
) -> None:
    """为项目全部（或指定）角色生成定妆照，写回 bible_json 的 ref_image_path。"""
    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        raise ValueError("项目不存在或还没有角色圣经")
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    style = bible.world.visual_style_canonical

    selected = {str(name).strip() for name in (only_characters or []) if str(name).strip()}
    targets = [
        c for c in bible.characters
        if ((not selected or c.name in selected) and (only_character is None or c.name == only_character))
    ]
    if not targets:
        raise ValueError(f"角色不存在：{only_character or sorted(selected)}")

    # 初始定妆照登记到 character_portraits（适用集 1~ 至今），供按集分段刷新与生成台按集选图。
    from app import portraits as _portraits
    bible_version = project["bible_version"] or 0

    if resume:
        # A candidate is committed per character before the batch-level Bible merge.
        # Rehydrate only complete packs. A process may stop after the front view
        # was committed but before the side views finished; resume that pack
        # instead of mistaking the partial row for a completed character.
        from app.multiview import (
            character_multiview_enabled,
            complete_legacy_character_pack,
            pack_result_ok,
        )

        committed: dict[str, str] = {}
        for character in targets:
            if fresh_after is None:
                row = conn.execute(
                    "SELECT image_path FROM character_portraits "
                    "WHERE project_id=? AND character_name=? AND ep_start=1 "
                    "ORDER BY created_at DESC LIMIT 1",
                    (project_id, character.name),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT image_path FROM character_portraits "
                    "WHERE project_id=? AND character_name=? AND ep_start=1 AND created_at>=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (project_id, character.name, fresh_after),
                ).fetchone()
            if row and row["image_path"] and Path(row["image_path"]).is_file():
                if character_multiview_enabled():
                    try:
                        pack = await complete_legacy_character_pack(
                            project_id, character.name, 1, style,
                        )
                    except Exception:  # noqa: BLE001 - regular generation retries below
                        pack = None
                    if not pack_result_ok(pack):
                        continue
                    row = conn.execute(
                        "SELECT image_path FROM character_portraits "
                        "WHERE project_id=? AND character_name=? AND ep_start=1 "
                        "ORDER BY created_at DESC LIMIT 1",
                        (project_id, character.name),
                    ).fetchone()
                    if not row or not row["image_path"] or not Path(row["image_path"]).is_file():
                        continue
                character.ref_image_path = row["image_path"]
                committed[character.name] = row["image_path"]
        if committed:
            _merge_generated_portraits(
                conn, project_id, [c for c in targets if c.name in committed]
            )
            conn.commit()
        targets = [c for c in targets if c.name not in committed]
        if not targets:
            return

    failures: list[tuple[str, Exception]] = []
    for c in targets:
        try:
            c.ref_image_path = None
            from app.stages import review_portrait_image
            override = (c.portrait_prompt_override or "").strip()
            base_prompt = (
                ensure_portrait_clothing_contract(override)
                if override
                else portrait_prompt(style, c.appearance_canonical)
            )
            effective_appearance = portrait_appearance_anchor(
                base_prompt, production_appearance_anchor(c.appearance_canonical),
            )
            last_error: Exception | None = None
            # Score-only：只生成一次；QA 低分不带 critique 重生（PRD QA-SO #14）。
            for attempt in range(1, 2):
                portrait_id: str | None = None
                path = str(Path(ref_path(project_id, c.name)).with_name(
                    f"{_safe_name(c.name)}__{new_id('candidate')}.jpg"
                ))
                prompt = base_prompt
                try:
                    call_meta = {
                        "asset_kind": "portrait",
                        "character_name": c.name,
                        "episode_no": 1,
                        "portrait_mode": "initial",
                        "attempt": attempt,
                    }
                    if operation_started_at is not None:
                        operation_material = (
                            f"{project_id}:{operation_started_at}:{c.name}:initial_portrait"
                        )
                        call_meta.update({
                            "operation_id": "op_portrait_" + hashlib.sha256(
                                operation_material.encode("utf-8")
                            ).hexdigest()[:32],
                            "reuse_successful_operation": True,
                        })
                    item = await hiagent.generate_image(
                        prompt,
                        size=config.REF_IMAGE_SIZE,
                        call_meta=call_meta,
                    )
                    if item.get("url"):
                        await hiagent.download(item["url"], path)
                    elif item.get("b64_json"):
                        import base64
                        atomic_write_bytes(path, base64.b64decode(item["b64_json"]))
                    else:
                        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")
                    qa = await review_portrait_image(
                        hiagent.encode_image_file(path), base_prompt,
                    )
                    artifact = record_reference_asset(
                        asset_type="character_portrait",
                        scope_id=f"{project_id}:{c.name}:1",
                        file_path=path,
                        content={
                            "character_name": c.name,
                            "appearance": effective_appearance,
                            "prompt": prompt,
                            "attempt": attempt,
                        },
                        parent_artifact_ids=(
                            [project["bible_artifact_id"]] if project["bible_artifact_id"] else []
                        ),
                        qa=qa,
                    )
                    if artifact["status"] not in {"approved", "validated"}:
                        last_error = ContentGenerationError(
                            f"定妆照技术校验未通过：{c.name}"
                        )
                        continue
                    portrait_id = _portraits.stage_initial_portrait(
                        conn, project_id, c.name, path, effective_appearance, prompt,
                        bible_version, artifact_id=artifact["id"])
                    # 初始多视角资产包会有界补齐；耗尽后仍发布已落盘的正面主图。
                    from app.multiview import (
                        ensure_character_multiview_pack, character_multiview_enabled, pack_result_ok,
                    )
                    if character_multiview_enabled():
                        pack = await ensure_character_multiview_pack(
                            project_id=project_id,
                            portrait_id=portrait_id,
                            character_name=c.name,
                            appearance=effective_appearance,
                            visual_style=style,
                            portrait_prompt=base_prompt,
                            ep_start=1,
                            primary_qa=qa,
                        )
                        if not pack_result_ok(pack):
                            raise ContentGenerationError(
                                f"定妆多视角包结构不完整：{c.name}"
                            )
                    _portraits.promote_staged_initial_portrait(
                        conn, project_id, c.name, portrait_id,
                    )
                    c.appearance_canonical = effective_appearance
                    c.ref_image_path = path
                    break
                except asyncio.CancelledError:
                    if portrait_id:
                        from app.rejected_media import purge_character_portrait
                        purge_character_portrait(conn, portrait_id)
                    c.ref_image_path = None
                    raise
                except hiagent.ProviderError as exc:
                    if portrait_id:
                        from app.rejected_media import purge_character_portrait
                        purge_character_portrait(conn, portrait_id)
                        portrait_id = None
                    c.ref_image_path = None
                    last_error = exc
                except Exception as exc:  # noqa: BLE001 候选失败后在有界循环内修复
                    if portrait_id:
                        from app.rejected_media import purge_character_portrait
                        purge_character_portrait(conn, portrait_id)
                        portrait_id = None
                    c.ref_image_path = None
                    last_error = exc
            if not c.ref_image_path:
                raise last_error or hiagent.ProviderError(f"定妆照生成失败：{c.name}")
            # Publish each accepted character as its own durable checkpoint.
            # The remaining characters may take many minutes or be interrupted
            # by a process restart; already-ready packs must be visible and
            # resumable without waiting for the entire batch to finish.
            _merge_generated_portraits(conn, project_id, [c])
            conn.commit()
        except Exception as exc:  # noqa: BLE001 失败要响：逐角色记录，最后汇总抛出
            failures.append((c.name, exc))

    # Scene Bible generation runs in parallel with portraits.  Merge only the
    # fields owned by this task so its old snapshot cannot erase newly added scenes.
    _merge_generated_portraits(conn, project_id, targets)
    conn.commit()
    if failures:
        details = "；".join(f"{name}：{exc}" for name, exc in failures)
        raise ContentGenerationError(f"定妆资产生成未完整通过：{details}")
    return {
        "generated": [c.name for c in targets if c.ref_image_path],
        "gate_retry_exhausted": False,
        "warnings": [],
    }


def refs_as_image_inputs(bible: Bible, character_names: list[str], limit: int,
                         *, project_id: str | None = None,
                         episode_no: int | None = None) -> list[tuple[str, str]]:
    """出场角色定妆照 →(data_url, role) 列表，按出场顺序最多 limit 张。

    传入 project_id+episode_no 时，按集号选用 character_portraits 中覆盖该集的定妆照（分镜阶段按集
    反应式重绘形成的分段，时间维一致性）；未命中或未传时回退到 bible 里的初始 ref_image_path。
    """
    out: list[tuple[str, str]] = []
    by_name = {c.name: c for c in bible.characters}
    for name in character_names[:max(limit, 0)]:
        c = by_name.get(name)
        if not c:
            continue
        path = None
        if project_id is not None:
            from app.portraits import portrait_for_episode
            path = portrait_for_episode(project_id, name, episode_no)
        path = path or c.ref_image_path
        if path:
            try:
                out.append((hiagent.data_url_from_file(path), "reference_image"))
            except OSError:
                continue  # 文件被手动删除时跳过该参考图（prompt 锚点串仍在兜底一致性）
    return out
