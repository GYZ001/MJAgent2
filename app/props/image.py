"""道具参考图生成：纯色背景单件道具定物图（与 app.scenes 的定场图机制同构，
不内联复用其私有实现——两者只在同一层同构，不共享调用边界）。
"""
from __future__ import annotations

import base64

from app import config, hiagent
from app.atomic_io import atomic_write_bytes
from app.refs import visual_style_lock

from .store import prop_ref_path


def prop_ref_prompt(style: str, appearance_canonical: str, *, name: str = "") -> str:
    """道具定物图生成词：纯色浅灰背景、单件道具、无人物、无文字。"""
    style_constraint = visual_style_lock(style) if style.strip() else ""
    identity = f"道具名称：{name.strip()}。" if name.strip() else ""
    parts = [
        style_constraint,
        f"道具定物图（纯色浅灰背景，仅这一件道具居中展示，画面中不出现任何人物）："
        f"{identity}{appearance_canonical.strip()}",
        "1:1 或竖幅构图，产品级实物摄影/渲染质感，光影均匀，无阴影夸张，无场景陈设",
        "不得生成任何文字、字幕、标签、角标、水印或 logo，不得出现其它道具",
    ]
    return "。".join(p.strip("。") for p in parts if p.strip()) + "。"


async def _save_prop_image_item(item: dict, dest: str) -> None:
    if item.get("url"):
        await hiagent.download(item["url"], dest)
    elif item.get("b64_json"):
        atomic_write_bytes(dest, base64.b64decode(item["b64_json"]))
    else:
        raise hiagent.ProviderError(f"图像响应缺少 url/b64_json：{list(item.keys())}")


async def generate_prop_reference_image(project_id: str, name: str, prompt: str) -> str | None:
    """出一张道具定物图并落盘；失败返回 None（不抛出，由调用方决定是否记 failed 状态）。"""
    dest = prop_ref_path(project_id, name)
    try:
        item = await hiagent.generate_image(
            prompt, size=config.REF_IMAGE_SIZE,
            call_meta={"asset_kind": "prop_reference", "prop_name": name},
        )
        await _save_prop_image_item(item, dest)
    except Exception:  # noqa: BLE001 技术失败交由调用方记 failed 状态，不在这里吞
        return None
    return dest
