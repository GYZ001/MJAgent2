"""人物谱页「未出图」角标缺理由的投影补丁（WS13）。

WS10 起，一句话真名（画面在场证据不足，如三国白话的桓帝、张角/张宝/张梁；
跑不快的德科/埃托奥/莱曼/蒙铁尔/马丁内斯）入谱但不自动出定妆照。落库时
``app.portraits.cards.ensure_character_card`` 把结果写进
``projects.bible_auto_changes_json``——``status`` 复用
``auto_applied_asset_pending``/``auto_applied_asset_failed``/``auto_apply_failed``，
``decision_reason`` 是给用户看的具体原因。用 B 生产库实测核对发现这个原因有
两种真实成因（``character_portrait_projection`` docstring有完整说明，不在这里
重复）：``app.portraits.card_verdict.portrait_generation_decision`` 判定的
"戏份不足……"长句，或身份消歧确认真名后 ``generate_portrait=False`` 结构性
延后出图时的通用兜底句"人物卡已加入；定妆包等待独立资产环节确认"——proj_
ecabd38b7261（三国白话）/proj_ce9fcf749b23（跑不快的孩子）里桓帝/张角/德科等
角色当时落库的实际是后一种。人物谱页 ``GET /projects/{id}?view=bible`` 的
``bible.characters[]`` 投影从未读过这份队列，前端只能按"有没有
``ref_image_url``"判出一个笼统的「未出图」角标，用户看不到真正原因，会误以为
出图失败去反复重试。

本模块只做只读投影：从已经挂好 ``portraits[]``/``ref_image_url``（见
``app.domain.projects.bible_attachments._attach_character_portraits``）的角色
字典 + ``bible_auto_changes_json`` 原始列表，算出 ``portrait_status``/
``portrait_reason`` 两个展示字段，不引入新状态机、不回写数据库。真正的状态
转移仍然只在 ``app.portraits.cards.ensure_character_card`` /
``app.portraits.portrait_drift.ensure_cards_for_screenplay`` 里发生（本次改动
不许碰 ``app/portraits/*``）——这里只是把已经存在的 ``status``/
``decision_reason`` 显示出来。
"""
from __future__ import annotations

import json

# 必须与 app.portraits.cards.ensure_character_card 写入 bible_auto_changes_json
# 时使用的 kind 取值集合保持一致（该函数归 app/portraits/*，本次改动不许碰，
# 见 app/portraits/portrait_drift.py::ensure_cards_for_screenplay 同一份集合）。
_DISCOVERY_KINDS = {"new_character", "character_discovery", "new_bible_character"}

_DEFERRED_STATUSES = {"auto_applied_asset_pending"}
_FAILED_STATUSES = {"auto_applied_asset_failed", "auto_apply_failed"}
_GENERATING_STATUSES = {"processing"}

PortraitStatus = str  # "ready" | "generating" | "deferred" | "failed" | "missing"


def _character_has_portrait(character: dict) -> bool:
    """判据须与挂载 ref_image_url/portraits 的
    ``bible_attachments._attach_character_portraits`` 同源：``ref_image_url``
    挂载时已优先取最新 ready 分段图，这里不需要重新遍历落盘文件。"""
    if character.get("ref_image_url"):
        return True
    return any(
        portrait.get("image_url") and portrait.get("pack_status") in (None, "ready")
        for portrait in character.get("portraits") or []
    )


def _latest_change_for_character(changes: list[dict], name: str) -> dict | None:
    matches = [
        item for item in changes
        if isinstance(item, dict)
        and item.get("kind") in _DISCOVERY_KINDS
        and item.get("character") == name
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: item.get("decided_at") or item.get("created_at") or 0)


def character_portrait_projection(character: dict, changes: list[dict]) -> tuple[str, str]:
    """返回 ``(portrait_status, portrait_reason)``。

    ``portrait_status`` 取值：

    - ``ready``：已出图。
    - ``generating``：定妆包正在生成（``bible_auto_changes_json`` 队列里该角色
      的最新记录处于 ``processing``）。
    - ``deferred``：未出图·尚未出图（``auto_applied_asset_pending``）。用 B
      生产库实测核对发现这一态有两种成因，``portrait_reason`` 会如实区分：
      ``portrait_generation_decision`` 判定"戏份不足"给出的具体长句，或身份
      消歧确认真名后以 ``generate_portrait=False`` 结构性延后出图时的通用
      兜底句"人物卡已加入；定妆包等待独立资产环节确认"（``app/identity_
      adjudication.py``/``app/production/prep_pack/persistent_appellation.py``，
      与戏份多少无关）——两种都归 ``deferred``，不在这里拆成两个状态值（没有
      结构化字段能可靠区分，拆了也只是从 reason 文本猜测，不比展示原文可靠）。
    - ``failed``：未出图·失败——定妆包生成本身报错，或人物卡写入失败。
    - ``missing``：未出图，但没有可归因的队列记录（多半是初始批次角色、尚未
      跑过 refs 生成，或曾出图后被画风切换清空）——没有数据就不编造原因，
      ``portrait_reason`` 保持空字符串，遵守"不得兜底填充"。
    """
    if _character_has_portrait(character):
        return "ready", ""
    name = character.get("name") or ""
    change = _latest_change_for_character(changes, name)
    if change is None:
        return "missing", ""
    status = str(change.get("status") or "")
    reason = str(change.get("decision_reason") or "")
    if status in _GENERATING_STATUSES:
        return "generating", reason
    if status in _DEFERRED_STATUSES:
        return "deferred", reason
    if status in _FAILED_STATUSES:
        return "failed", reason
    # auto_applied 但仍判定为无图（例如图后来被画风切换清空）：如实标"未出图"，
    # 不借用一条"生成成功"的旧 decision_reason 冒充失败/戏份不足原因。
    return "missing", ""


def attach_portrait_projection(bible: dict, changes_raw: str | None) -> None:
    """给 ``bible.characters[]`` 逐个挂 ``portrait_status``/``portrait_reason``。

    ``changes_raw`` 是 ``projects.bible_auto_changes_json`` 原始列，可能是
    None/空字符串/非法 JSON/非列表——均按"没有队列数据"处理，不抛异常中断
    整份人物谱投影：这只是一个展示补充字段，不能因为解析失败拖垮
    ``GET /projects/{id}``。
    """
    try:
        changes = json.loads(changes_raw or "[]")
        if not isinstance(changes, list):
            changes = []
    except (TypeError, ValueError):
        changes = []
    for character in bible.get("characters", []):
        status, reason = character_portrait_projection(character, changes)
        character["portrait_status"] = status
        character["portrait_reason"] = reason
