"""分镜台 P0-E：体貌锚点与着装/表情分离（2026-09-30，真实生产回归：
proj_ca86b15ab7d7 EP1，逐帧核对生成视频发现）。

背景：世界书人物外观锚点（``Character.appearance_canonical``/``character_portraits.
appearance``）把体貌特征、默认服装、默认表情焊在同一段文字里，例如「二十四岁的年轻
女性，……，乌黑顺直的长发垂到胸前，……，穿米白色宽松针织开衫，内搭浅蓝色细碎花棉质
长裙，……，神情温柔，嘴角带着浅浅的笑」。``app.production.storyboard_dialects`` 的
「锚点第一次出现时必须逐字沿用」规则（角色长相跨段一致性的唯一保证手段）因此把默认
服装/默认表情也一起焊进了每一集第一次出场的那一镜——而全集服装表
（``app.production.storyboard_wardrobe_plan``）/跨段连贯性备忘
（``app.production.storyboard_continuity_memo``）才是这一刻真正该穿什么的权威。两边
打架时，模型的应对是先抄一遍锚点默认服装，再用否定句「纠正」（「此刻她没有穿开衫，
身上是……」／「颈间没有围巾」）——但 Seedance 这类视频模型不理解否定句，看到「开衫」
「围巾」这些词就会画出来。实测三处真实缺陷：温念在围裙外面又穿了一件按剧情已经不
存在的开衫、顾屿凭空多出一条已经摘下的围巾（画面里出现两条围巾）、温念在听到坏消息/
触电时仍带着锚点默认的「温柔浅笑」。

修法（对应任务 Required Outcome A 的首选机制）：阶段一（``storyboard_beat_sheet``）
本来就要一次性看到每个人物的完整外观锚点（供 ``storyboard_wardrobe_plan`` 推导首次
着装），在同一次模型调用里让它顺手申报一份「只保留体貌特征」的锚点子集——
``_AiBeatSheetDraft.physical_anchors``（见 ``storyboard_beat_sheet_schemas``）。模型
申报、代码核验（核验是原锚点的字符子序列，即只做了删减、没有改写或新增，同构于
``app/schemas/character.py`` 的 ``AppearanceEvidence``「模型申报+代码核验」证据锚点
模式——那边核验连续子串，这里核验允许跳字的子序列，因为体貌专用锚点需要跳过锚点
中间的服装/表情小句，不是连续摘录），核验通过的体貌专用锚点在阶段一结束后原地覆盖
回 ``payload["asset_manifest"]["characters"][].appearance``——阶段二
``storyboard_pack._segment_relevant_assets`` 切片到的、``storyboard_dialects``「锚点
必须逐字沿用」规则读到的都是这份体貌专用文本，服装不再随体貌锚点重复出现，也就不再
需要否定句「纠正」；服装改由全集服装表/连贯性备忘正面给出（见这两个模块 2026-09-30
的同批修法），表情则完全交给每一镜的表演内容（``storyboard_dialects`` 已有的「情绪
一律写成面部肌肉动作和肢体动作」规则）。核验失败或没有申报的人物原样保留完整锚点
（含服装/表情），行为与改造前完全一致——不兜底改写（CLAUDE.md「不得兜底填充」）。

隐藏随身物（贴身佩戴、被外层衣物挡住看不见的配饰，例如星盘挂坠隔着卫衣贴胸挂着）
另有专门的正面陈述——见 ``physical_anchor_beat_sheet_rules`` 第二条：这类东西原样
抄进体貌锚点或着装描述时，「隔着卫衣贴胸挂着」这类写法会被视频模型当成外衣上能看见
的花纹/图案画出来，应该写成「看不见」或「衣服下若隐若现的轻微凸起」。

覆盖点选在 ``storyboard_pack.generate_storyboard_pack`` 的唯一编排接线点
``storyboard_wardrobe_recheck.generate_beat_sheet_with_wardrobe_recheck`` 返回前
（2026-10-02 起取代直接调用 ``storyboard_short_drama_review.generate_beat_sheet_
with_drop_review``——后者仍保留、仍在返回前做同一次覆盖，只是生产路径不再经它，
见该模块与 ``storyboard_wardrobe_recheck`` 模块 docstring），不在 ``storyboard_pack.
generate_storyboard_pack`` 本体里（后者已在 ``app/FILE_CONVENTIONS.toml`` 的
line_count 棘轮基线上零余量，见其模块 docstring）——覆盖必须晚于短剧档可能存在的
「第二遍」生成、也必须晚于本集可能新增的服装表同场变化复核，否则读到的锚点/服装表
会是覆盖或复核之前的旧状态。
"""
from __future__ import annotations

import logging
from typing import Any

from app.production.storyboard_wardrobe_plan import known_identity_ids

log = logging.getLogger(__name__)


def physical_anchor_beat_sheet_rules() -> list[str]:
    """阶段一 rules[]，两档都无条件追加（不按 adaptation_mode 分支）。"""
    return [
        "为本集每个出场人物申报一份体貌专用锚点（physical_anchors）：identity_id 逐字取自 "
        "known_assets.characters 里的某个 identity_id；只有这个人物的 appearance（外观锚点）"
        "是一段具体描述时才需要申报，appearance 本身是「没有标准外观……」这类说明文字的人物"
        "不用报。physical_description 只保留脸型/五官轮廓、发型发色、体型身高、年龄区间这几类"
        "不随场次改变的体貌特征，必须从 appearance 原文里逐字摘取、按原有先后顺序删减而成"
        "（只删不改、不新增一个字，可以跳过中间不要的部分），服装/配饰的款式颜色、默认表情"
        "（例如「神情温柔」「嘴角带笑」）都不属于体貌特征，不要保留——这两类信息分别由全集"
        "服装表和每一镜的表演内容负责，不在这里重复。",
        "appearance 锚点或着装描述里如果写到某件随身物/配饰贴身佩戴、被外层衣物盖住看不见"
        "（例如「隔着卫衣贴胸挂着」「藏在衣领下面」），体貌专用锚点与后续着装描述都不要把它"
        "写成外层衣物上能看见的花纹或图案——这类看不见的随身物，画面描述要么整句不提，要么"
        "写成「衣服下若隐若现的轻微凸起」这类看不见实体、只见轮廓的说法。",
    ]


def _is_char_subsequence(candidate: str, source: str) -> bool:
    """``candidate`` 的每个字符能否按原有先后顺序在 ``source`` 里逐个找到（允许中间
    跳过任意字符）——只允许「删减」，不允许「插入新内容」或「打乱顺序」，是「模型申报、
    代码核验」这条纪律里「核验」的具体算法。"""
    pos = 0
    for ch in candidate:
        pos = source.find(ch, pos)
        if pos == -1:
            return False
        pos += 1
    return True


def physical_anchor_verified(physical_description: str, appearance_anchor: str) -> bool:
    """体貌专用锚点是否是原锚点的合法删减：去空白后非空，且是原锚点的字符子序列。"""
    candidate = "".join(physical_description.split())
    source = "".join(appearance_anchor.split())
    return bool(candidate) and _is_char_subsequence(candidate, source)


def _character_appearance_by_identity(payload: dict[str, Any]) -> dict[str, str]:
    """本集 asset_manifest.characters 的 identity_id -> 完整外观锚点（覆盖前的原始值，
    含服装/表情），供核验体貌专用锚点是不是它的子序列。"""
    manifest = payload.get("asset_manifest") or {}
    return {
        str(c.get("identity_id") or ""): str(c.get("appearance") or "")
        for c in manifest.get("characters") or []
        if c.get("identity_id")
    }


def build_physical_anchor_overrides(draft: Any, payload: dict[str, Any]) -> dict[str, str]:
    """核验 ``draft.physical_anchors`` 每条申报，只保留 identity_id 命中本集已知人物、
    且 ``physical_description`` 确实是对应完整锚点字符子序列的条目；返回
    identity_id -> 体貌专用锚点 的覆盖表。不合法的条目记一条告警（不阻断、不改写），
    不参与覆盖——对应人物沿用完整外观锚点，行为与改造前完全一致。
    """
    anchors_by_identity = _character_appearance_by_identity(payload)
    known_ids = known_identity_ids(payload)
    overrides: dict[str, str] = {}
    for nomination in getattr(draft, "physical_anchors", None) or []:
        identity_id = nomination.identity_id
        anchor = anchors_by_identity.get(identity_id, "")
        if identity_id not in known_ids or not anchor:
            log.warning(
                "[STORYBOARD_PHYSICAL_ANCHOR_UNKNOWN][未拦截] physical_anchors 申报了未知"
                "人物「%s」或该人物没有可核验的完整外观锚点，已丢弃，本人物沿用完整外观锚点",
                identity_id,
            )
            continue
        if not physical_anchor_verified(nomination.physical_description, anchor):
            log.warning(
                "[STORYBOARD_PHYSICAL_ANCHOR_UNVERIFIED][未拦截] 「%s」的 physical_description"
                "「%s」不是完整外观锚点的有效删减（可能改写或新增了内容），已丢弃，本人物沿用"
                "完整外观锚点", identity_id, nomination.physical_description[:60],
            )
            continue
        overrides[identity_id] = nomination.physical_description
    return overrides


def apply_physical_anchor_overrides(payload: dict[str, Any], overrides: dict[str, str]) -> None:
    """把已核验的体貌专用锚点原地写回 asset_manifest.characters[].appearance——覆盖之后
    阶段二 ``storyboard_pack._segment_relevant_assets`` 切片到的、``storyboard_dialects``
    「锚点必须逐字沿用」规则读到的都是这份体貌专用文本，不再连带默认服装/默认表情。没有
    命中 overrides 的人物（没有申报，或申报未通过核验）原样保留完整锚点。

    覆盖前把即将被替换掉的完整锚点（含服装）快照进 ``appearance_at_beat_sheet``——这是
    阶段一 ``storyboard_wardrobe_plan`` 模型提名 wardrobe_plan 时实际看到的那份原文，
    本函数覆盖之后 ``appearance`` 本身就不再含服装信息了。``storyboard_wardrobe_plan.
    _character_appearance_by_identity`` 判断「全集服装表第一条记录是否真的从锚点逐字
    抄来」时必须读这份快照、不能读覆盖后的 ``appearance``，否则任何触发了本函数覆盖的
    人物都会被误判成「锚点里查不到服装」（见该模块 docstring 的时序说明）。
    """
    manifest = payload.get("asset_manifest") or {}
    for character in manifest.get("characters") or []:
        identity_id = str(character.get("identity_id") or "")
        if identity_id in overrides:
            character["appearance_at_beat_sheet"] = character.get("appearance")
            character["appearance"] = overrides[identity_id]
