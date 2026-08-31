"""``ensure_character_card`` 的"戏份不足 / 卡片不完整"终态判定。

从 ``cards.py`` 的 ``ensure_character_card`` 函数体内搬出：该函数已经顶格
app/FILE_CONVENTIONS.toml 的 function_lines 基线（238 行），新增判据装不下就得
先搬家，不能靠加基线（棘轮只降不升）。

事故背景：模型判定某角色"值得建卡"（``important=true``），只因
``appearance_canonical`` 长度不在 ``APPEARANCE_MIN``~``APPEARANCE_MAX`` 之间（或
``role`` 不在合法枚举里）就被 ``cards.py._build_verdict`` 强制降级为
``important=false``——但下游沿用的还是模型那句"值得建卡"的理由，``status`` 却变成
``skipped_minor``，日志和界面上看起来是"戏份不足"，实际是格式问题。这里把两种
情形分开：``card_incomplete`` 是需要人看见的失败（reason 写实际的长度/角色枚举
越界），``skipped_minor`` 才是模型本身就判定不重要的正常终态。

``card_incomplete`` 不写负缓存（``_discovery_skip_key``）：它是一次性的格式失败，
不是"这个角色戏份不够"的稳定判断，下次调用应该让模型重新有机会写对，不能被
静默缓存 20 集。它也刻意不落进 ``app/portraits/cards_ensure.py`` 现有的
``{"skipped_minor", "exists", "skipped_not_person"}`` 正常终态集合——那个文件归
另一个代理管，这里不碰它；``card_incomplete`` 不在那个集合里，会自然落进它已有
的 ``else: errors.append(...)`` 分支，不需要它配合。
"""

from __future__ import annotations

from app.db import set_setting

from .discovery_fragments import _discovery_skip_key


def unimportant_verdict_result(
    name: str,
    verdict: dict,
    *,
    require_identity_card: bool,
    card_complete: bool,
    project_id: str,
    fragment_signature: str,
) -> dict | None:
    """``verdict["important"]`` 为假、且不在"身份已确认+卡片完整"豁免路径时的
    终态结果；两种豁免成立时返回 ``None``（调用方继续走建卡流程，实际不会走到
    这个分支，只是与原调用点的条件写法对齐，避免额外分支判断）。
    """
    if verdict["important"] or (require_identity_card and card_complete):
        return None
    if require_identity_card:
        return {
            "status": "error", "name": name,
            "reason": (
                "身份模型已确认真名，但人物卡模型未返回完整稳定卡片："
                + (verdict.get("incomplete_reason") or "未知原因")
            ),
        }
    if verdict.get("model_important") and not card_complete:
        # 模型自己判了 important=true，是代码因格式不达标才强制降级——不是
        # "戏份不足"，reason 必须写实际越界的字段与数值，不能沿用 verdict["reason"]
        # （那是模型给"值得建卡"下的结论，会把格式问题误报成戏份判断）。
        return {
            "status": "card_incomplete", "name": name,
            "reason": verdict.get("incomplete_reason")
            or "appearance_canonical/role 未通过完整度校验",
        }
    set_setting(_discovery_skip_key(project_id, name), fragment_signature)
    return {"status": "skipped_minor", "name": name, "reason": verdict["reason"]}
