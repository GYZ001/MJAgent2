"""按段决定要不要省略场景参考图：场景此刻的物理状态与场景卡默认状态是否一致。

2026-10-01（《顾念长安》第 1 集 ep_a3c61162b4ce 第四轮逐帧复查，出租屋「温念的
出租屋」被淹场景）：场景卡与参考图是干燥默认状态（scene_1ca544fa6618），但
第 1 集第 15-19 段剧情里房间已经被淹（满地积水、鞋柜歪倒、绿萝横倒、纸箱塌陷、
帆布鞋泡水）。装配参考图时此前无条件把场景卡图发给视频模型，画面因此把卡片里
未被正文点名的干燥陈设（直立木柜、健康盆栽、多双鞋）原样画出来，与正文的积水
状态互相矛盾（第 18/19 段实测两个失败候选）。

与人物 ``wardrobe_matches_default``（见 ``app.video_modes.character_look_
selection``）同一思路：分镜模型逐段自报本段场景状态是否还是卡片默认状态
（取值定义见 ``app.production.storyboard_segment_resources._AiResourceScene.
scene_state_matches_card``）。模型明确报告 ``no``/``unsure`` 时都不发这张可能
已经过期的参考图，场景全凭正文——CLAUDE.md「不得兜底填充」：场景没有人物侧
「头像照」那种退一步仍然诚实的降级素材可用，模型明确说拿不准时唯一不会误导的
做法是不发图，而不是赌一把发送可能对、可能错的旧状态图（这也是与人物侧
「unsure 时仍发头像照」保守方向相反的原因：发错的后果是把灾后场景画成日常
场景，比缺一张参考图更糟）。

空字符串（字段在这个存量分镜段里根本不存在——此字段上线前生成、从未被模型
评估过）单独处理，**不**并入 ``unsure`` 分支：那是"模型从没被问过"，不是"问了
答不出来"，没有任何新证据支持"这段场景状态变了"，贸然按不一致处理会让全部
存量分镜（数量远超本次要修的 15-19 段）一次性全部停发场景参考图，把一次针对
性修复变成一次大范围回归（CLAUDE.md 稳定性优先于视觉效果，但这里连视觉效果
也会普遍变差）——保持改动前"有卡就发"的行为，不升级新的限制。

2026-10-02（代码评审 #0）：首版只在 ``log.info`` 里留了可见信号，装配期发生
在分镜生成之后，没有任何地方把省略决定写回已持久化的段落记录——用户做
「每轮分镜台后必须人工核查」时看不到任何提示，只能去 B 机后端日志里 grep。
补了 ``resource_scene_state_advisories``，由
``app.production.storyboard_pack._segment_content_advisories`` 在生成时调用，
结论随生成结果自然落进该段 ``degraded_capabilities``（前端会渲染），不需要
装配期再做任何写库动作。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def scene_reference_omission_reason(scene_name: str, scene_state_matches_card: str) -> str | None:
    """返回本段省略这张场景参考图的理由；``None`` 表示应当照常发送。

    省略发生时在这里统一打一行可见信号（``[STORYBOARD_SCENE_REF_OMITTED_STATE_
    CHANGED][未拦截]``），不是新增一处阻断，只是让「这段为什么没有场景图」在日志
    里可查——调用方（确认闸门预检、单镜/整集生成、补齐到全片扫描）各自独立调用
    ``resolve_shot_asset_dependencies``，统一在这一处记录比每个调用点各补一遍
    更不容易漏掉。
    """
    if scene_state_matches_card in ("", "yes"):
        return None
    if scene_state_matches_card == "no":
        reason = "本段场景状态与场景卡不一致（scene_state_matches_card=no）"
    else:
        reason = f"本段场景状态是否与场景卡一致未确认（scene_state_matches_card={scene_state_matches_card!r}），保守按不一致处理"
    log.info("[STORYBOARD_SCENE_REF_OMITTED_STATE_CHANGED][未拦截] 场景「%s」：%s", scene_name, reason)
    return reason


def resolve_scene_reference_entry(
    *, scene_name: str, has_card: bool, scene_reference_id: str | None, image_path: str,
    scene_state_matches_card: str, purposes: list[str],
) -> dict[str, Any]:
    """装配 ``_storyboard_pack_asset_dependencies`` 的一条场景条目（不含反打
    视角——反打图的查询需要 ``conn``，由调用方在拿到这份 entry 之后另行叠加，
    见 ``app.scene_reverse.segment_views.augment_scene_entry_with_reverse_
    angle``）。从 ``app.multiview._resolve_scene_entry`` 搬出：该函数把"选哪张
    图"和"要不要发这张图"两件事都放在 multiview.py 里会让这个已经逼近
    ``app/FILE_CONVENTIONS.toml`` 行数棘轮基线的文件继续涨，搬成纯函数后
    multiview.py 那端只剩一次函数调用，与 ``pick_character_reference_view``
    搬出的理由同源（见该函数所在模块 docstring）。
    """
    omitted_reason = scene_reference_omission_reason(scene_name, scene_state_matches_card)
    usable = bool(image_path) and Path(image_path).is_file() and not omitted_reason
    selected_view = {
        "id": scene_reference_id, "view_role": "establishing", "image_path": image_path,
        "input_fingerprint": scene_reference_id, "purposes": list(purposes),
    } if usable else None
    return {
        "name": scene_name,
        "asset_required": has_card,
        "scene_revision_id": scene_reference_id,
        "pack_status": "ready" if usable else None,
        "asset_usable": usable,
        "pack_usable": usable,
        "primary_usable": usable,
        "selected_view_ids": [scene_reference_id] if selected_view else [],
        "selected_views": [selected_view] if selected_view else [],
        "available_view_roles": ["establishing"] if selected_view else [],
        "missing_required": [] if (selected_view or not has_card) else ["establishing"],
        "scene_state_omitted_reason": omitted_reason,
    }


def resource_scene_state_advisories(scenes: list[Any]) -> list[str]:
    """供 ``app.production.storyboard_pack._segment_content_advisories`` 调用：

    生成时就把"这张场景参考图装配时会被省略"写进本段 ``degraded_
    capabilities``（人工核查看得到，不必等到装配期才去 B 机后端日志里 grep
    同一条判断——2026-10-02 代码评审 #0：仅靠 ``scene_reference_omission_
    reason`` 内部的 ``log.info`` 不满足"拦人要给可见信号"的纪律，装配期也
    没有任何地方把这个决定写回已持久化的段落记录）。判据不重复发明，仍是
    ``scene_reference_omission_reason``；这里只是多一个消费方，与装配期各
    自独立调用，互不影响，也不需要装配期才知道的 ``image_path``/行信息。

    ``scenes`` 是刚解析出的 pydantic 对象（未落库），``model_fields_set``
    能看出这次模型回包里到底有没有这个 key——pydantic 的字面量默认值
    ``"unsure"`` 在没被 ``model_fields_set`` 认领时不能当证据用：红灯测试
    实测过，不做这层判断会让任何一次模型漏填都被误判成"场景状态变了"而报
    advisory，而这正是本字段文档里"字段缺失不等于 unsure"那条规则本该挡住
    的情况，只是换了一个消费点重新踩了一次。
    """
    advisories: list[str] = []
    for scene in scenes:
        fields_set = getattr(scene, "model_fields_set", None)
        if fields_set is not None and "scene_state_matches_card" not in fields_set:
            continue
        scene_id = str(getattr(scene, "scene_id", "") or "")
        name = scene_id.split(":", 1)[-1] if scene_id else ""
        reason = scene_reference_omission_reason(
            name, str(getattr(scene, "scene_state_matches_card", "") or ""),
        )
        if reason:
            advisories.append(f"[STORYBOARD_SCENE_REF_OMITTED_STATE_CHANGED][未拦截] 场景「{name}」：{reason}")
    return advisories
