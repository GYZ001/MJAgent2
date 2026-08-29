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

from app import config, generation_concurrency, hiagent
from app.atomic_io import atomic_write_bytes
from app.db import get_conn, new_id
from app.evidence.media import record_reference_asset
from app.errors import ContentGenerationError
from app.schemas import Bible, character_is_portrait_eligible


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


_PORTRAIT_CLOTHING_CONTRACT = (
    "常规角色定妆照着装，服装面料不透明并完整覆盖身体，"
    "重点呈现面部、发型、外层服装和可见配饰"
)
_PORTRAIT_PRIVACY_SAFE_STYLE = (
    "人物面部与皮肤必须采用明显动画化比例和非照片级卡通渲染材质，"
    "保持虚构角色辨识度但不得生成可误认成真人照片的写实人脸"
)
_PORTRAIT_PHOTOGRAPHIC_STYLE_NOTE = (
    "人物面部与皮肤必须采用摄影级写实质感和自然人体比例，保留清晰可见的肌理、"
    "毛孔与光影层次；角色身份仍是虚构数字角色、并非真实世界中存在的真人照片，"
    "但渲染手法必须是照片级摄影写实，不得改画成卡通或插画质感"
)
_PORTRAIT_OVERRIDE_APPEARANCE_ONLY_NOTE = (
    "最近一次用户编辑文案作为完整外观补充；全局画风合同仍独立生效"
)
PRODUCTION_APPEARANCE_MIN_CHARS = 20
PRODUCTION_APPEARANCE_MAX_CHARS = 80
# 场景锚点的长度闸，与人物外观锚点同构：闸的数字和写进提示词的数字必须是同一个。
# 真实故障 ERR-20260828-4f4f19（《罗刹海市》场景库）：提示词写「30~60 字」，闸卡
# 在 80 字，模型照着 60 盲打，12 个场景里 3 个写到 81。preview 端点把这份清单原样
# 返回，用户点确认时被自己的提交端点以 422 拒收——预览承诺的东西提交不进去。
SCENE_CANONICAL_MIN_CHARS = 30
SCENE_CANONICAL_MAX_CHARS = 80


def production_appearance_anchor(anchor: str) -> str:
    """Preserve the approved appearance contract without lexical filtering."""
    return normalize_prompt_text(anchor or "").strip()


def ensure_portrait_clothing_contract(prompt: str) -> str:
    safe = production_appearance_anchor(prompt)
    if _PORTRAIT_CLOTHING_CONTRACT not in safe:
        safe = f"{safe}。{_PORTRAIT_CLOTHING_CONTRACT}" if safe else _PORTRAIT_CLOTHING_CONTRACT
    return normalize_prompt_text(safe)


def visual_style_lock(visual_style: str) -> str:
    style = normalize_prompt_text(visual_style or "").strip()
    from app.visual_styles import is_photographic_style_prompt
    prefix = f"画风最高优先级：必须严格保持「{style}」，" if style else "画风最高优先级："
    if is_photographic_style_prompt(style):
        return normalize_prompt_text(
            prefix
            + "整体必须保持统一的照片级人像摄影渲染，要求真实的人体比例、自然肌理、"
              "摄影质感的光影和景深；不得擅自切换成卡通、二次元、插画、CG 渲染或其他"
              "与该画风冲突的非写实风格"
        )
    return normalize_prompt_text(
        prefix
        + "不得擅自切换成与该画风冲突的真人摄影、照片写实、live-action、"
          "实拍质感或其他渲染风格。整体必须保持统一的 CG/动画/漫画/插画类非真人渲染"
    )


def character_visual_style_lock(visual_style: str) -> str:
    from app.visual_styles import is_photographic_style_prompt
    style = normalize_prompt_text(visual_style or "").strip()
    if is_photographic_style_prompt(style):
        return normalize_prompt_text(
            f"{visual_style_lock(visual_style)}。"
            "人物面部与皮肤必须采用照片级摄影写实质感和自然人体比例，保留清晰可见的"
            "肌理、毛孔与光影层次；角色身份仍是虚构数字角色，但渲染手法不得画成卡通、"
            "二次元或 CG 材质"
        )
    return normalize_prompt_text(
        f"{visual_style_lock(visual_style)}。"
        "人物面部与皮肤必须采用明显动画化比例和非照片级卡通/CG 渲染材质，"
        "保持虚构数字角色质感，不得生成可误认成真人照片或真人实拍的脸和皮肤"
    )


def scene_visual_style_lock(visual_style: str) -> str:
    from app.visual_styles import is_photographic_style_prompt
    style = normalize_prompt_text(visual_style or "").strip()
    if is_photographic_style_prompt(style):
        return normalize_prompt_text(
            f"{visual_style_lock(visual_style)}。"
            "环境必须保持统一的实景摄影质感渲染，要求真实材质、自然光影和摄影级细节，"
            "不得切换成卡通、插画或 CG 渲染背景"
        )
    return normalize_prompt_text(
        f"{visual_style_lock(visual_style)}。"
        "环境必须保持统一的动画/插画/CG 场景渲染，不得切换成真人实景、"
        "摄影棚实拍、实景照片或照片写实背景"
    )


def portrait_override_appearance_anchor(anchor: str, portrait_prompt_override: str | None = None) -> str:
    fallback = production_appearance_anchor(anchor)
    override = normalize_prompt_text(portrait_prompt_override or "").strip()
    return override or fallback


def effective_portrait_prompt(
    visual_style: str,
    anchor: str,
    portrait_prompt_override: str | None = None,
    period_costume_canonical: str = "",
) -> str:
    merged_anchor = portrait_override_appearance_anchor(anchor, portrait_prompt_override)
    prompt = portrait_prompt(visual_style, merged_anchor, period_costume_canonical)
    if not normalize_prompt_text(portrait_prompt_override or "").strip():
        return prompt
    return normalize_prompt_text(
        f"{prompt}。{_PORTRAIT_OVERRIDE_APPEARANCE_ONLY_NOTE}。"
        f"最新外观补充已吸收：{ensure_portrait_clothing_contract(merged_anchor)}"
    )


def portrait_prompt(visual_style: str, anchor: str, period_costume_canonical: str = "") -> str:
    from app.visual_styles import is_photographic_style_prompt
    style = character_visual_style_lock(visual_style)
    body = production_appearance_anchor(anchor)
    period_costume = normalize_prompt_text(period_costume_canonical or "").strip()
    period_contract = (
        f"年代服饰硬约束：{period_costume}；服装形制、面料、鞋履、束发和配饰必须符合该年代、地域与身份，禁止现代、跨时代或跨文化误植。"
        if period_costume else
        "服装形制、面料、鞋履、束发和配饰必须服从角色外观锚点与世界年代，不得擅自加入现代或跨时代元素。"
    )
    privacy_note = (
        _PORTRAIT_PHOTOGRAPHIC_STYLE_NOTE
        if is_photographic_style_prompt(normalize_prompt_text(visual_style or "").strip())
        else _PORTRAIT_PRIVACY_SAFE_STYLE
    )
    return normalize_prompt_text(
        f"{style}。单角色全身定妆照：{body}。"
        "完整遵循锚点声明的实体形态、空间关系、姿态和关联道具；"
        "若锚点未声明特殊姿态，则采用正面中性展示姿态。纯浅米色背景，全身完整可见。"
        "头顶、肩臂和鞋底均不得贴边或出画，主体四周保留至少 8% 安全边距。"
        f"{_PORTRAIT_CLOTHING_CONTRACT}。{period_contract}。{privacy_note}。"
        "不得添加外观合同未声明的主体或视觉元素"
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


def portrait_appearance_anchor(prompt: str | None, fallback: str = "") -> str:
    """Return the separately persisted appearance authority when available."""
    fallback_text = production_appearance_anchor(fallback)
    return fallback_text or production_appearance_anchor(prompt or "")


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
    if only_characters is not None and not selected and only_character is None:
        return {"generated": [], "gate_retry_exhausted": False, "warnings": ["暂无具备定妆资格的角色"]}
    targets = [
        c for c in bible.characters
        if character_is_portrait_eligible(c)
        and ((not selected or c.name in selected) and (only_character is None or c.name == only_character))
    ]
    if not targets:
        requested = only_character or sorted(selected)
        raise ValueError(f"角色不存在或暂不具备定妆资格：{requested}")

    # 初始定妆照登记到 character_portraits（适用集 1~ 至今），供按集分段刷新与生成台按集选图。
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

    # 各角色互相独立（互不引用彼此的定妆照作为参考图），故按角色分批并发；
    # 并发池上限见 generation_concurrency.character_portrait_batch_semaphore
    # ——与场景定场图各自独立，互不挤占彼此的槽位。角色内部（正面→侧面视角）
    # 的先后依赖完全保留在 ensure_character_multiview_pack 内部，未受影响。
    bible_merge_lock = asyncio.Lock()
    semaphore = generation_concurrency.character_portrait_batch_semaphore()

    async def _bounded(c) -> None:
        async with semaphore:
            await _generate_one_character_portrait(
                project_id, c, style, project, bible_version,
                operation_started_at, bible_merge_lock,
            )

    results = await asyncio.gather(
        *(_bounded(c) for c in targets), return_exceptions=True,
    )
    failures: list[tuple[str, Exception]] = []
    for c, result in zip(targets, results):
        if result is None:
            continue
        if isinstance(result, Exception):
            failures.append((c.name, result))
        elif isinstance(result, BaseException):
            # CancelledError (or any other non-Exception BaseException): the
            # whole batch is being torn down, not "this character failed" --
            # propagate instead of quietly recording it as a content failure.
            raise result

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


async def _generate_one_character_portrait(
    project_id: str,
    c,
    style: str,
    project,
    bible_version: int,
    operation_started_at: float | None,
    bible_merge_lock: asyncio.Lock,
) -> None:
    """Run one character's full definitive-portrait pipeline; raises on failure.

    Spawned as its own ``asyncio.Task`` by ``asyncio.gather`` in
    ``generate_refs``.  ``get_conn()`` keys connections by
    ``asyncio.current_task()`` (see ``app.db``), so calling it fresh here --
    never inheriting the caller's ``conn`` via closure -- gives this
    character its own isolated SQLite connection.  Concurrent siblings
    therefore never share a connection or its implicit transaction state,
    which matters because most writes below commit immediately but a few
    (staged portrait -> multiview pack -> promote) span several statements.
    """
    conn = get_conn()
    from app import portraits as _portraits

    c.ref_image_path = None
    override = (c.portrait_prompt_override or "").strip()
    base_prompt = effective_portrait_prompt(
        style, c.appearance_canonical, override, c.period_costume_canonical,
    )
    effective_appearance = portrait_override_appearance_anchor(
        c.appearance_canonical, override,
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
            # VLM 图片质检已下线：定妆照是否可用只看文件是否存在（技术校验），
            # 由 record_reference_asset 内部的 validate_image_file 判定；不再产生分数。
            qa: dict = {}
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
    # Publish each accepted character as its own durable checkpoint.  The
    # remaining characters may take many minutes or be interrupted by a
    # process restart; already-ready packs must be visible and resumable
    # without waiting for the entire batch to finish.  ``bible_merge_lock``
    # serializes this read-modify-write of the whole bible_json blob against
    # concurrent siblings in the same batch -- two unlocked writers here
    # would silently lose one another's merge (last write wins on the whole
    # blob, not just this character's fields).
    async with bible_merge_lock:
        _merge_generated_portraits(conn, project_id, [c])
        conn.commit()


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
