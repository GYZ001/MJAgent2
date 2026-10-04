"""定妆照写实画风生成后的肤色局部色块核验：生成→判定→（判否）加强提示词重画，
有限次数；判定本身失败（未判定）立即停止，不继续重画，标记待人工确认。

判定不合格后不能走图生图修改（供应商对图生图人物图一律按隐私拒收，见
``app.portraits.headshot_crop`` 模块文档），只能换一版措辞更强的提示词重新纯
文生图；本模块因此不接收"已生成图片"作为输入，而是自己驱动"生成→判定"整个
循环，调用方只需注入单个 ``generate_and_write`` 协作者（prompt、call_meta ->
None；落盘到调用方给定的 ``path``，调用方的供应商下载/复用失效细节由它自己的
闭包处理，本模块不关心），与 ``app.scene_reverse.produce`` 同一"调用方传入
裸回调"的注入手法（拆包测试打桩才能透传到这里，见 CLAUDE.md「拆包静默废掉
monkeypatch」）。

判定调用走 ``app.harness.model_gateway``（``app/portraits`` 下唯一合法模型
入口，``scripts/check_contract_surface.py`` 的 FORBIDDEN 不许直接调用 hiagent
的聊天接口，与 ``app.portraits.headshot_crop``/
``app.video_modes.prop_composite_face_check`` 同一约束）。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app import hiagent
from app.harness import model_gateway
from app.portraits import portrait_skin_audit_store
from app.portraits.portrait_skin_blush import PORTRAIT_SKIN_BLUSH_RULE_VERSION
from app.visual_styles import is_photographic_style_prompt

_LOGGER = logging.getLogger(__name__)

#: 最多生成次数：一次初始生成 + 一次加强措辞重画。来源与
#: ``app.portraits.headshot_crop`` 裁后复核「最多两轮」同一上限口径——多一次
#: 重画成本等同再生成一张定妆照，继续加码边际收益低；同一提示词加强到第三遍
#: 仍判不过的情况留给人工确认，不无限重试。
MAX_SKIN_BLUSH_ATTEMPTS = 2

STATUS_SKIPPED = "skipped"
STATUS_CLEAN = "clean"
STATUS_UNVERIFIED = "unverified"
STATUS_FLAGGED = "flagged"

GenerateAndWrite = Callable[[str, dict[str, Any]], Awaitable[None]]

_JUDGE_PROMPT = (
    "这是一张人物定妆照（写实摄影画风）。请只判断脸颊、眼皮、鼻头、额头、下巴这几处"
    "皮肤上是否存在与周围肤色明显不同、边界清晰的局部颜色区域——例如腮红、红晕、"
    "彩色眼影，或某种彩色光斑/光晕只落在脸颊、眼皮或半边脸的某一处。嘴唇、眉毛、"
    "头发、瞳孔本身带有与周围皮肤不同的固有颜色，不算局部色块，不在判断范围内；"
    "均匀一致的自然肤色、正常的明暗阴影、自然光源造成的渐变式暖冷色调（没有清晰"
    "硬边界的那种）也不算。\n"
    '只返回一个 JSON 对象：{"has_local_color": true 或 false, "reason": "一句话说明依据"}。'
)


def _build_judge_messages(image_url: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "Return exactly one valid JSON object. No Markdown, no prose."},
        {"role": "user", "content": [
            {"type": "text", "text": _JUDGE_PROMPT},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]


def _parse_judge(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    body = text[start:end + 1] if start >= 0 and end > start else text
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    has_local_color = data.get("has_local_color")
    if not isinstance(has_local_color, bool):
        raise ValueError(f"has_local_color 不是布尔值：{has_local_color!r}")
    return {"has_local_color": has_local_color, "reason": str(data.get("reason") or "")}


async def judge_face_local_color(path: str, *, call_meta: dict[str, Any]) -> dict[str, Any]:
    """一次 VLM 判定；格式/网络失败重问一次；两次都失败返回 ``checked=False``
    （未判定，不阻断），与 ``app.portraits.headshot_crop`` 的 ``_chat_json_twice``
    同一取舍。"""
    image_url = hiagent.data_url_from_file(path)
    messages = _build_judge_messages(image_url)
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = await model_gateway.chat(
                messages, temperature=0, max_tokens=300,
                provider=hiagent.active_provider("vlm"),
                call_meta={"kind": "vlm_portrait_skin_blush_check", "judge_attempt": attempt, **call_meta},
                response_format={"type": "json_object"},
            )
            return {"checked": True, "rule_version": PORTRAIT_SKIN_BLUSH_RULE_VERSION, **_parse_judge(raw)}
        except Exception as exc:  # noqa: BLE001 格式/网络失败都按未判定处理，见模块文档
            last_error = exc
            _LOGGER.warning("[PORTRAIT_SKIN_BLUSH_CHECK][第 %d 次核验未通过] %s", attempt, exc)
    return {
        "checked": False, "has_local_color": None, "reason": "",
        "rule_version": PORTRAIT_SKIN_BLUSH_RULE_VERSION, "error": str(last_error),
    }


def _cache_verdict(path: str, *, project_id: str, character_name: str, verdict: dict[str, Any]) -> None:
    """只缓存``checked``为真的结论——``unverified``（判定失败）与``skipped``
    （未核验）两种调用方根本不会走到这里。"""
    portrait_skin_audit_store.set_cached_audit(
        content_hash=portrait_skin_audit_store.content_sha256(path), rule_version=verdict["rule_version"],
        project_id=project_id, character_name=character_name, portrait_id="",
        image_path=path, has_local_color=bool(verdict["has_local_color"]), reason=verdict.get("reason") or "",
    )


def _strengthen_prompt(prompt: str) -> str:
    """判否后用于重新生成的加强措辞：追加一句指出上一张的问题并重申要求，不改动
    提示词其余部分（保持角色外观/服装/画风锚点不变，只加强肤色这一条）。"""
    return (
        f"{prompt}。重要修正：本次人物整张脸是干净的素颜，脸颊、眼皮、鼻头、额头、"
        "下巴是同一种均匀自然的肤色，只有光线造成的柔和明暗。"
    )


async def generate_front_full_with_skin_check(
    *, prompt: str, path: str, visual_style: str, call_meta: dict[str, Any],
    generate_and_write: GenerateAndWrite, project_id: str, character_name: str,
) -> dict[str, Any]:
    """生成 front_full 候选图（委托 ``generate_and_write`` 落盘到 ``path``）；
    写实画风（``visual_style`` 命中 ``is_photographic_style_prompt``，口径与
    ``app.refs.character_visual_style_lock`` 同源）下在判否时换一版加强措辞
    重新生成，最多 ``MAX_SKIN_BLUSH_ATTEMPTS`` 次。返回
    ``{"final_prompt", "attempts", "status"}``：``status`` 为 ``skipped``
    （非写实画风，只生成一次不核验）/``clean``（核验通过）/``unverified``
    （判定本身连续失败，立即停止，待人工确认）/``flagged``（重画次数用尽仍
    判有色块，保留最后一张）。``project_id``/``character_name`` 只用于把
    ``clean``/``flagged`` 这两种"确实判过"的结论回填进
    ``app.portraits.portrait_skin_audit_store`` 的缓存——否则存量核验端点对
    刚生成的这张图第一次查询必然缓存未命中，被迫重新发一次独立的真实模型
    调用，还可能与本次结论不一致而静默漏报（2026-10-04 复查发现）。

    调用方（``app.refs._generate_one_character_portrait``）只在进入本函数前调用
    一次 ``rollback_before_long_wait``；本函数内部每轮重画都会再做一次长等待
    （图像生成 + 两次 VLM 判定），不重复回滚——这依赖一个未被本函数强制的假设：
    调用方传入的 ``generate_and_write`` 闭包在这些长等待之前不会把未提交事务留在
    调用方连接上（当前实现的 ``download_or_invalidate_reuse`` 只在失败路径写入且
    立即自提交，满足假设）。若未来有人往该闭包里加一条跨越长等待的写入，必须在
    那个闭包内部自己调用 ``rollback_before_long_wait``，不能指望本函数兜底。
    """
    photographic = is_photographic_style_prompt((visual_style or "").strip())
    attempts: list[dict[str, Any]] = []
    current_prompt = prompt
    for attempt in range(1, MAX_SKIN_BLUSH_ATTEMPTS + 1):
        gen_meta = dict(call_meta)
        if attempt > 1:
            # 换一版加强提示词重画不能复用上一次的 operation_id：dedup 复用会
            # 原样吐回刚被判否的那张图，白跑一轮核验（真实踩坑，特此记录）。
            gen_meta["skin_blush_attempt"] = attempt
            if gen_meta.get("operation_id"):
                gen_meta["operation_id"] = f"{gen_meta['operation_id']}_blush{attempt}"
        await generate_and_write(current_prompt, gen_meta)
        if not photographic:
            return {"final_prompt": current_prompt, "attempts": attempts, "status": STATUS_SKIPPED}
        verdict = await judge_face_local_color(path, call_meta={"skin_blush_attempt": attempt, **call_meta})
        attempts.append({"attempt": attempt, **verdict})
        if verdict["checked"] is False:
            return {"final_prompt": current_prompt, "attempts": attempts, "status": STATUS_UNVERIFIED}
        if verdict["has_local_color"] is False:
            _cache_verdict(path, project_id=project_id, character_name=character_name, verdict=verdict)
            return {"final_prompt": current_prompt, "attempts": attempts, "status": STATUS_CLEAN}
        if attempt == MAX_SKIN_BLUSH_ATTEMPTS:
            _LOGGER.warning(
                "[PORTRAIT_SKIN_BLUSH_CHECK][重画 %d 次仍判有局部色块] 保留最后一张，"
                "标记供人工确认：%s", MAX_SKIN_BLUSH_ATTEMPTS, call_meta,
            )
            _cache_verdict(path, project_id=project_id, character_name=character_name, verdict=verdict)
            return {"final_prompt": current_prompt, "attempts": attempts, "status": STATUS_FLAGGED}
        current_prompt = _strengthen_prompt(current_prompt)
    raise AssertionError("unreachable: MAX_SKIN_BLUSH_ATTEMPTS 循环必在返回前终止")


__all__ = [
    "MAX_SKIN_BLUSH_ATTEMPTS",
    "STATUS_SKIPPED", "STATUS_CLEAN", "STATUS_UNVERIFIED", "STATUS_FLAGGED",
    "judge_face_local_color",
    "generate_front_full_with_skin_check",
]
