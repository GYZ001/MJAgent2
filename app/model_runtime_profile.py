"""从 ``provider_calls`` 的历史观测推导单个模型的运行画像。

「这个模型思考多少 token」「它多久才吐出第一个字符」描述的是**模型自己**跑
起来什么样，不是业务阶段的属性，也不是一个能被全局常量说清的东西。真实事
故里同一套全局参数同时对两个模型判错，而且是往相反方向错的：

* 火山 seed（``d71l5c8nfdb167kligqg``）：2787 次 chat 调用 ``reasoning_tokens``
  **全部为 0**——它根本不思考，给它预留 16384 思考预算是纯浪费。它真正的
  软肋是首字慢，观测首字延迟中位 4.1s、p99 30.7s、最大 241s，长尾一旦越过
  全局 300s 读超时，整集分镜在第一阶段就 ``ReadTimeout`` 且 ``recv=0``。
* 智谱 ``glm-5.3-flash``：思考量横跨三个数量级（中位 336、p95 11012、最大
  30839）。普通调用远在 16384 预留之内，可分镜台阶段二这种重任务稳定落在
  最右尾，思考吃掉 30417 token 后只剩 367 token 写答案，``finish_reason=
  length`` 整集失败。

一个常量不可能同时喂饱「不思考但很慢」和「很快但思考到撑」这两类模型。参数
必须回到模型自己身上，而依据就在库里：每次调用的 ``reasoning_tokens``、
``first_chunk_at`` 都已经落盘，不需要新增采集。

判据只从观测推导，不含任何模型名特判——换一个没见过的模型，它照样在跑够样本
后得到属于自己的画像；样本不足时返回 ``None``，由调用方回落到全局默认，
**绝不把「没有观测」当成「观测到 0」**。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.db import get_conn

#: 少于这么多次观测就不敢下结论，交回调用方用全局默认。冷启动、刚换模型、
#: 刚清库都会落在这里，此时沉默地回落比拿三五次样本外推安全。
MIN_OBSERVATIONS = 30

#: 只看最近这段时间的调用。供应商会换后端权重、会调思考策略，半年前的观测
#: 不代表今天的它。
OBSERVATION_WINDOW_S = 30 * 24 * 3600

#: 画像缓存有效期。热路径每次调用都去聚合一遍库没必要，模型行为也不会在分钟
#: 级别里变。
_CACHE_TTL_S = 300.0

_cache: dict[str, tuple[float, "ModelRuntimeProfile"]] = {}
_cache_lock = threading.Lock()


class ModelRuntimeProfile:
    """一个模型的观测画像。字段为 ``None`` 表示「观测不足，别用我」。"""

    __slots__ = ("model", "sample_count", "reasoning_ceiling", "first_token_ceiling_s")

    def __init__(
        self,
        *,
        model: str,
        sample_count: int,
        reasoning_ceiling: int | None,
        first_token_ceiling_s: float | None,
    ) -> None:
        self.model = model
        self.sample_count = sample_count
        self.reasoning_ceiling = reasoning_ceiling
        self.first_token_ceiling_s = first_token_ceiling_s

    def as_meta(self) -> dict[str, Any]:
        """写进 call_meta 供观测台复盘：这次用的是画像还是全局默认。"""
        return {
            "profile_sample_count": self.sample_count,
            "profile_reasoning_ceiling": self.reasoning_ceiling,
            "profile_first_token_ceiling_s": self.first_token_ceiling_s,
        }


def _empty_profile(model: str) -> ModelRuntimeProfile:
    return ModelRuntimeProfile(
        model=model, sample_count=0, reasoning_ceiling=None, first_token_ceiling_s=None,
    )


def _quantile(sorted_values: list[float], fraction: float) -> float:
    """已排序序列的分位值，按索引取，不插值。空序列由调用方先行排除。"""
    index = int(len(sorted_values) * fraction)
    return sorted_values[min(len(sorted_values) - 1, max(0, index))]


def _measure(model: str) -> ModelRuntimeProfile:
    """聚合一个模型近期的 chat 调用观测。

    ``provider_calls`` 没有 provider 列，模型 ID 是这张表能提供的最细粒度；
    同名模型挂在两个 provider 下会被合并统计，这在本仓库的模型 ID（``glm-5.3
    -flash``、``d71l5c8nfdb167kligqg`` 这类）下不会发生，真发生了也只是让画像
    更保守，不会漏掉任何一边的上界。
    """
    cutoff = time.time() - OBSERVATION_WINDOW_S
    try:
        rows = get_conn().execute(
            # reasoning_tokens 用 SQLite 自己的 json_extract 取，避免把整份
            # response_json（含完整正文）拉进 Python 再解析。
            """SELECT
                   json_extract(response_json,
                       '$.usage.completion_tokens_details.reasoning_tokens') AS reasoning_tokens,
                   length(coalesce(json_extract(response_json, '$.choices[0].message.content'), '')) AS content_len,
                   ts,
                   first_chunk_at
               FROM provider_calls
               WHERE model=? AND kind='chat' AND ts > ?""",
            (model, cutoff),
        ).fetchall()
    except Exception:  # noqa: BLE001 观测是优化手段，读不到就回落全局默认
        return _empty_profile(model)

    reasoning: list[float] = []
    first_token: list[float] = []
    for row in rows:
        raw = row["reasoning_tokens"]
        # 思考完却没交出正文（content 为空）的调用不是「答案要付的思考成本」，是一次失控：
        # 2026-09-03 seed2.0mini 思考 131078 token 后 content 为空，若计入 max，预留会超过模型输出
        # 上限 32768，分镜台预算闸门从此对该模型一律拒绝（留给答案 -98310 tokens），直到 30 天窗口滑过。
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and (row["content_len"] or 0) > 0:
            reasoning.append(float(raw))
        ts, chunk_at = row["ts"], row["first_chunk_at"]
        if ts and chunk_at:
            try:
                delta = float(chunk_at) - float(ts)
            except (TypeError, ValueError):
                continue
            if delta >= 0:
                first_token.append(delta)

    sample_count = max(len(reasoning), len(first_token))

    reasoning_ceiling: int | None = None
    if len(reasoning) >= MIN_OBSERVATIONS:
        # 取观测最大值而不是某个分位：思考预留只抬高请求的 max_tokens 上限，
        # 按真实用量计费（见 config.TEXT_REASONING_TOKEN_RESERVE 的说明），
        # 预留高了不花钱，预留低了直接截断整集。撞满上限那几次也要计入——它
        # 们的 reasoning_tokens 是被 max_tokens 夹住的**下界**，恰恰证明这个
        # 模型在这类任务上至少要思考这么多。
        reasoning_ceiling = int(max(reasoning))

    first_token_ceiling_s: float | None = None
    if len(first_token) >= MIN_OBSERVATIONS:
        # 首字延迟相反，必须防长尾绑架：这里的值会变成等待超时，取最大值会
        # 让一次 241s 的异常把所有调用的容忍度顶上去，真卡死的调用也得干等。
        # p99 覆盖正常波动，剩下的交给读超时本身兜底。
        first_token_ceiling_s = float(_quantile(sorted(first_token), 0.99))

    return ModelRuntimeProfile(
        model=model,
        sample_count=sample_count,
        reasoning_ceiling=reasoning_ceiling,
        first_token_ceiling_s=first_token_ceiling_s,
    )


def model_runtime_profile(model: str | None) -> ModelRuntimeProfile:
    """取一个模型的观测画像，带 TTL 缓存。模型名为空时返回空画像。"""
    key = (model or "").strip()
    if not key:
        return _empty_profile("")
    now = time.time()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return cached[1]
    profile = _measure(key)
    with _cache_lock:
        _cache[key] = (now, profile)
    return profile


def reset_cache() -> None:
    """清空画像缓存。供测试与「刚换了模型配置想立刻生效」的运维路径使用。"""
    with _cache_lock:
        _cache.clear()
