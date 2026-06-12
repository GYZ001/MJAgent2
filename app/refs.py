"""角色定妆照工作流（人物跨集一致性的核心，PRD §5.4 第 2 层）。

圣经定稿后为每个角色生成 Seedream 全身立绘，存入 projects/<id>/refs/；
生成镜头时，出场角色的定妆照以 base64 data URL 注入 reference_image。
实测结论（2026-06-12）：HiAgent /up/* 文件接口受 CSRF 保护不可程序化调用，
但网关接受 data URL 参考图，故不需要外部托管。
"""
from __future__ import annotations

import json
import re

from app import config, hiagent
from app.db import get_conn
from app.schemas import Bible


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w一-鿿]", "_", name)


def ref_path(project_id: str, character_name: str) -> str:
    d = config.PROJECTS_DIR / project_id / "refs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"{_safe_name(character_name)}.jpg")


def portrait_prompt(visual_style: str, anchor: str) -> str:
    return (
        f"{visual_style}。全身角色立绘定妆照：{anchor}。"
        "正面站立，中性表情，双臂自然下垂，纯浅米色背景，全身完整可见，无文字无水印"
    )


async def generate_refs(project_id: str, only_character: str | None = None) -> None:
    """为项目全部（或指定）角色生成定妆照，写回 bible_json 的 ref_image_path。"""
    conn = get_conn()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or not project["bible_json"]:
        raise ValueError("项目不存在或还没有角色圣经")
    bible = Bible.model_validate(json.loads(project["bible_json"]))
    style = bible.world.visual_style_canonical

    targets = [c for c in bible.characters if (only_character is None or c.name == only_character)]
    if not targets:
        raise ValueError(f"角色不存在：{only_character}")

    errors: list[str] = []
    for c in targets:
        try:
            prompt = (c.portrait_prompt_override or "").strip() or portrait_prompt(style, c.appearance_canonical)
            item = await hiagent.generate_image(prompt, size=config.REF_IMAGE_SIZE)
            path = ref_path(project_id, c.name)
            if item.get("url"):
                await hiagent.download(item["url"], path)
            elif item.get("b64_json"):
                import base64
                with open(path, "wb") as f:
                    f.write(base64.b64decode(item["b64_json"]))
            else:
                raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")
            c.ref_image_path = path
        except Exception as exc:  # noqa: BLE001 失败要响：逐角色记录，最后汇总抛出
            errors.append(f"{c.name}：{exc}")

    conn.execute("UPDATE projects SET bible_json=? WHERE id=?", (bible.model_dump_json(), project_id))
    conn.commit()
    if errors:
        raise RuntimeError("部分定妆照失败：" + "；".join(errors)[:600])


def refs_as_image_inputs(bible: Bible, character_names: list[str], limit: int) -> list[tuple[str, str]]:
    """出场角色定妆照 →(data_url, role) 列表，按出场顺序最多 limit 张。"""
    out: list[tuple[str, str]] = []
    by_name = {c.name: c for c in bible.characters}
    for name in character_names[:max(limit, 0)]:
        c = by_name.get(name)
        if c and c.ref_image_path:
            try:
                out.append((hiagent.data_url_from_file(c.ref_image_path), "reference_image"))
            except OSError:
                continue  # 文件被手动删除时跳过该参考图（prompt 锚点串仍在兜底一致性）
    return out
