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

from .discovery_fragments import _discovery_skip_key, _non_character_skip_key
from .presence_evidence import (
    functional_card_worthy,
    has_onscreen_evidence,
    presence_evidence_citation,
)


def reconsider_verdict_with_presence_evidence(name: str, verdict: dict, evidence: dict) -> dict:
    """非角色（``subject_kind != person``）的模型判定必须与画面存在证据对照
    （WS3：人物发现按叙事分量与画面存在判定，非角色判定不再与画面事实相反）。

    证据里有 ≥1 处在场出现（对白/动作描写邻接候选称谓，或已有分镜把它标成
    在场角色）时，不能直接采信模型"这不是一个人"的结论——按证据把
    ``subject_kind`` 改判为 ``"person"``。生产事故：马拉多纳（"赛后马拉多纳
    哭了"）、姆巴佩（"姆巴佩刚刚跑过他身边"）先后被判 subject_kind 非人，
    写进 ``char_not_character`` 永久负缓存，proj_ce9fcf749b23——哭、跑都是
    只有人才做得出的动作，模型的"这不是人"判定本身就与画面事实矛盾。

    刻意只纠正 subject_kind，不代管"重要与否"这个独立的叙事分量判断：结构
    信号能可靠证明"这能哭/能跑/能开口说话，所以是人"，但不能可靠证明"这段
    戏份值不值得建卡"——一句话路人同样会命中动作邻接（"路人甲走过"里"走"
    与候选称谓紧邻，结构上与"马拉多纳哭了"同形），见
    ``tests/test_character_discovery.py::test_minor_character_is_skipped_and_negatively_cached``
    的既有预期：那类候选就应该保持"戏份不足"，不能被本函数误伤。important
    的取舍原样交还给 ``model_important``（subject_kind 硬闸门生效前模型的
    真实判断，被 ``cards._build_verdict`` 强制压低前的值——``require_identity_
    card=True`` 时按合同应为 True，见 ``assess_new_character`` 的
    identity_contract）与下游 ``unimportant_verdict_result``。

    已经判为 ``subject_kind=="person"`` 的 verdict 原样返回，不做任何改写——
    本函数只能把 subject_kind 从非人改成人，不会让任何现有角色被降级。
    """
    subject_kind = str(verdict.get("subject_kind") or "").strip()
    if subject_kind == "person" or not has_onscreen_evidence(evidence):
        return verdict
    citation = presence_evidence_citation(evidence)
    overridden = dict(verdict)
    overridden["subject_kind"] = "person"
    overridden["is_person"] = True
    overridden["important"] = bool(verdict.get("model_important"))
    overridden["reason"] = (
        f"模型原判「{verdict.get('reason') or subject_kind or '非人'}」，"
        f"但原文有画面在场证据（{citation}），按画面存在证据判定为人物"
    )
    return overridden


def non_character_or_unimportant_result(
    name: str, verdict: dict, *, require_identity_card: bool, card_complete: bool,
    project_id: str, cache_signature: str,
) -> dict | None:
    """``ensure_character_card`` 的"非人 / 不重要"终态判定：subject_kind 硬闸门
    + ``unimportant_verdict_result`` 收拢到一处（从 ``cards.py`` 内联搬出——
    该函数已顶格 function_lines 基线，新判据装不下就得先搬家，不能靠加基线）。

    ``cache_signature`` 取代裸的 ``fragment_signature`` 作为负缓存键的值：必须
    把画面存在证据折进去（见 ``presence_evidence.presence_evidence_fingerprint``
    docstring），否则同一段原文永远不会因为"分镜后来标出了在场证据"而重判。
    """
    subject_kind = str(verdict.get("subject_kind") or "").strip()
    if subject_kind != "person":
        # 人格是独立的硬闸门，不能被 require_identity_card 绕过：身份消歧确认的
        # 是"这是一个稳定的专名"，不是"这是一个人"。宗门、器物、地点即使专名
        # 稳定、戏份很重，也只能留在场景库/reference 身份里。
        set_setting(_discovery_skip_key(project_id, name), cache_signature)
        set_setting(_non_character_skip_key(project_id, name), "1")
        return {
            "status": "skipped_not_person",
            "name": name,
            "subject_kind": subject_kind,
            "reason": verdict["reason"],
        }
    return unimportant_verdict_result(
        name, verdict, require_identity_card=require_identity_card,
        card_complete=card_complete, project_id=project_id,
        fragment_signature=cache_signature,
    )


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


def portrait_generation_decision(*, require_identity_card: bool, presence: dict) -> tuple[bool, str]:
    """身份已确认的真名是否值得【自动】出定妆照（WS10-A：一句话真名过度收录）。

    生产事故（跑不快的孩子）：德科、埃托奥、莱曼、蒙铁尔、马丁内斯各只在一句话
    里被提到——「队里有罗纳尔迪尼奥，有德科，有埃托奥」「莱曼扑出点球」「蒙铁尔
    罚进」——却因 ``require_identity_card=True`` 时 ``assess_new_character`` 的
    合同固定 ``important=true``，全部被当成「重要配角」建卡并自动出图。根因是
    "身份消歧确认了这是稳定真名"（决定该不该建卡登记进人物谱）与"这段戏份值不
    值得花一次定妆照的生成开销"（决定该不该自动出图）被当成了同一件事。

    人物谱条目该不该建——不受本函数影响，`important=true` 的合同原样保留，
    真名一律登记（未来章节可能还会用到这个名字，需要一个可解析的稳定身份）。
    这里只决定新建的卡是否【自动】触发定妆照生成：判据复用
    ``app.portraits.presence_evidence.functional_card_worthy`` 同一套画面存在
    证据（在场 ≥2 段，或单段但对白+动作齐备）——与
    ``cards_ensure._ensure_qualifying_functional_cards`` 给无名功能身份建卡的
    门槛完全同一份判据，不写死名单，也不因为是"已确认真名"就放宽。够格的
    立即出图；不够格的人物卡仍然落库（role/appearance_canonical 都有），只是
    不自动生成定妆照——下一次该角色真的出现在某一集生成的剧本里时，
    ``app.portraits.portrait_drift.ensure_cards_for_screenplay`` 的自愈补图会
    按同一份 ``bible_auto_changes_json`` 状态重试（见 ``cards.ensure_character_
    card`` 对 ``auto_applied_asset_pending`` 的复用），也可以在人物谱页手动
    单独生成——两条路径都不需要这里的判定介入。

    ``require_identity_card=False``（模型自主判断戏份、非身份确认路径）时原样
    放行：那条路径的 ``important`` 本来就是模型自己给出的戏份判断，不是被合同
    强制的常量，不需要再加一层画面存在证据闸门。
    """
    if not require_identity_card or functional_card_worthy(presence):
        return True, ""
    return False, (
        "戏份不足（原文仅一句话提及/单次在场，未达到在场 ≥2 段或对白+动作齐备的"
        "门槛），人物卡已登记但未自动出图；角色后续如在剧本里实际出场会自动补图，"
        "也可在人物谱页手动生成定妆照"
    )
