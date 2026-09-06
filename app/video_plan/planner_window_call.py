"""视频规划窗口调用：输出不合法就带着证据有界重问（2026-09-06 第 14 轮第 28 集）。

第 28 集第 1 窗口模型回了 ``"shot_id":"shot_360a45648b8d,"relations"``——少一个引号，整段 JSON
解析失败，AI_PLAN_SCHEMA_INVALID 直接把整集生成台收口。结构化输出是单点脆弱性（与
cards.py 人物卡重试同源）：这里用与缓存复用同一条契约校验（``cached_window_is_valid``：
JSON 可解析 + 逐镜 PlannerShotAnalysis）判合法，不合法就把上一轮的片段作为证据追加一条
修正消息再问一次，最多 ``PLANNER_WINDOW_ATTEMPTS`` 次；仍不合法把最后一次响应原样交回，
由调用方照旧判 AI_PLAN_SCHEMA_INVALID（不放松校验、不兜底编计划）。重问用新的 operation_id，
否则幂等复用会把那条坏响应原样再拿回来。
"""
from __future__ import annotations

from typing import Any

from app.harness import model_gateway

from .planner_contract import cached_window_is_valid
from .primitives import _hash

PLANNER_WINDOW_ATTEMPTS = 3


def _correction_message(previous: str) -> dict[str, str]:
    snippet = previous.strip()[:400].replace("\n", " ")
    return {"role": "user", "content": (
        "上一轮输出不是合法的规划 JSON（片段：" + snippet + "）。请重新只输出一个 JSON 对象："
        "顶层键 shots 是数组，每一项的 shot_id 逐字取自上文给出的镜头 id 并用双引号完整包住，"
        "字段与上文契约一致，不要输出任何解释文字。"
    )}


async def planner_window_response(
    messages: list[dict[str, Any]], *, temperature: float, max_tokens: int, call_meta: dict[str, Any],
) -> str:
    """返回窗口的模型响应文本；不合法时最多重问到 ``PLANNER_WINDOW_ATTEMPTS`` 次。"""
    current = list(messages)
    meta = dict(call_meta)
    response = ""
    for attempt in range(1, PLANNER_WINDOW_ATTEMPTS + 1):
        response = await model_gateway.chat(current, temperature=temperature, max_tokens=max_tokens, call_meta=meta)
        if cached_window_is_valid(response):
            return response
        if attempt == PLANNER_WINDOW_ATTEMPTS:
            break
        current = [*current, {"role": "assistant", "content": response}, _correction_message(response)]
        meta = {**call_meta, "planner_retry_no": attempt,
                "operation_id": "op_video_plan_" + _hash({"model": meta.get("model"), "messages": current})[:24]}
    return response
