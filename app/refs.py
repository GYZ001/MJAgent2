"""角色定妆照工作流（人物跨集一致性的核心，PRD §5.4 第 2 层）。

圣经定稿后为每个角色生成 Seedream 全身立绘，存入 projects/<id>/refs/；
生成镜头时，出场角色的定妆照以 base64 data URL 注入 reference_image。
实测结论（2026-06-12）：HiAgent /up/* 文件接口受 CSRF 保护不可程序化调用，
但网关接受 data URL 参考图，故不需要外部托管。
"""
from __future__ import annotations

import asyncio
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


def portrait_prompt(visual_style: str, anchor: str) -> str:
    spectral_tokens = ("透明", "半透明", "虚影", "魂体", "灵魂", "幽灵", "悬浮", "漂浮", "人影")
    style = normalize_prompt_text(visual_style or "")
    body = normalize_prompt_text(anchor or "")
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
              "仅保留锚点明确要求的特效，禁止额外火焰、斗气光环、文字、水印和 logo"
        )
    return normalize_prompt_text(
        f"{style}。全身角色立绘定妆照：{body}。"
        "正面站立，中性表情，双臂自然下垂，纯浅米色背景，全身完整可见。"
        "头顶、肩臂和鞋底均不得贴边或出画，主体四周保留至少 8% 安全边距。"
        "仅保留锚点明确要求的特效，禁止额外火焰、斗气光环、文字、水印和 logo"
    )


def _merge_generated_portraits(conn, project_id: str, characters) -> None:
    """Merge accepted portrait paths into the latest concurrent Bible snapshot."""
    accepted = {item.name: item.ref_image_path for item in characters if item.ref_image_path}
    if not accepted:
        return
    row = conn.execute("SELECT bible_json FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row or not row["bible_json"]:
        return
    latest = json.loads(row["bible_json"])
    for item in latest.get("characters", []):
        if item.get("name") in accepted:
            item["ref_image_path"] = accepted[item["name"]]
    conn.execute(
        "UPDATE projects SET bible_json=? WHERE id=?",
        (json.dumps(latest, ensure_ascii=False), project_id),
    )


async def generate_refs(
    project_id: str,
    only_character: str | None = None,
    *,
    only_characters: list[str] | None = None,
    resume: bool = False,
    fresh_after: float | None = None,
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

    # 初始定妆照登记到 character_portraits（适用集 1~ 至今），供按集分段刷新与评审墙按集选图。
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
            base_prompt = ((c.portrait_prompt_override or "").strip()
                           or portrait_prompt(style, c.appearance_canonical))
            last_error: Exception | None = None
            # Score-only：只生成一次；QA 低分不带 critique 重生（PRD QA-SO #14）。
            for attempt in range(1, 2):
                portrait_id: str | None = None
                path = str(Path(ref_path(project_id, c.name)).with_name(
                    f"{_safe_name(c.name)}__{new_id('candidate')}.jpg"
                ))
                prompt = base_prompt
                try:
                    item = await hiagent.generate_image(
                        prompt,
                        size=config.REF_IMAGE_SIZE,
                        call_meta={
                            "asset_kind": "portrait",
                            "character_name": c.name,
                            "episode_no": 1,
                            "portrait_mode": "initial",
                            "attempt": attempt,
                        })
                    if item.get("url"):
                        await hiagent.download(item["url"], path)
                    elif item.get("b64_json"):
                        import base64
                        atomic_write_bytes(path, base64.b64decode(item["b64_json"]))
                    else:
                        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")
                    qa = await review_portrait_image(
                        hiagent.encode_image_file(path), c.appearance_canonical,
                    )
                    artifact = record_reference_asset(
                        asset_type="character_portrait",
                        scope_id=f"{project_id}:{c.name}:1",
                        file_path=path,
                        content={
                            "character_name": c.name,
                            "appearance": c.appearance_canonical,
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
                        conn, project_id, c.name, path, c.appearance_canonical, prompt,
                        bible_version, artifact_id=artifact["id"])
                    # 初始多视角资产包：任一侧视角/整包失败则禁止半包生效
                    from app.multiview import (
                        ensure_character_multiview_pack, character_multiview_enabled, pack_result_ok,
                    )
                    if character_multiview_enabled():
                        pack = await ensure_character_multiview_pack(
                            project_id=project_id,
                            portrait_id=portrait_id,
                            character_name=c.name,
                            appearance=c.appearance_canonical,
                            visual_style=style,
                            ep_start=1,
                            primary_qa=qa,
                        )
                        if not pack_result_ok(pack):
                            raise hiagent.ProviderError(
                                f"多视角资产包未通过，禁止生效：{c.name}"
                                f"（status={pack.get('status')}）"
                            )
                    _portraits.promote_staged_initial_portrait(
                        conn, project_id, c.name, portrait_id,
                    )
                    # The Bible path becomes visible only after the required
                    # multiview pack has passed as a whole.
                    c.ref_image_path = path
                    break
                except asyncio.CancelledError:
                    if portrait_id:
                        conn.execute("DELETE FROM character_portraits WHERE id=?", (portrait_id,))
                        conn.commit()
                    c.ref_image_path = None
                    raise
                except hiagent.ProviderError as exc:
                    if portrait_id:
                        conn.execute("DELETE FROM character_portraits WHERE id=?", (portrait_id,))
                        conn.commit()
                        portrait_id = None
                    c.ref_image_path = None
                    if "多视角资产包未通过" in str(exc):
                        raise
                    last_error = exc
                except Exception as exc:  # noqa: BLE001 候选失败后在有界循环内修复
                    if portrait_id:
                        conn.execute("DELETE FROM character_portraits WHERE id=?", (portrait_id,))
                        conn.commit()
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
        detail = "；".join(f"{name}：{exc}" for name, exc in failures)[:600]
        if all(isinstance(exc, ContentGenerationError) for _, exc in failures):
            raise ContentGenerationError("部分定妆照未通过质量校验：" + detail)
        raise hiagent.ProviderError("部分定妆照失败：" + detail)


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
