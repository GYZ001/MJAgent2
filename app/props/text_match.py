"""文本里逐字出现的道具卡匹配：按最长不重叠匹配、原文出现顺序去重，返回命中
的道具卡对象列表。

与 ``app.props.card_match``（判断"这次模型提及是不是某张既有卡的另一种写法"，
服务映射台道具登记）是不同问题：这里回答的是"一段已经写定的文本（场景规范
描述/场景状态描述）里，逐字点到了哪些已建卡的道具"，供生图提示词追加"外观按
道具卡画"的正面陈述，不涉及登记新卡或判断提及归属。

两个真实调用点共用同一份判据，不允许各写一套扫描逻辑而慢慢漂移（CLAUDE.md
「模型契约两侧必须对齐」同一精神）：
  ① ``app.video_modes.scene_state_views.prop_appearance_notes_for_description``
     ——场景状态图（2026-10-05，《顾念长安》EP1：场景状态图把道具卡「绿萝」
     画成酒红陶盆、「鞋柜」画成高木柜，压过了同时发出的道具卡参考图）。
  ② 本模块 ``prop_notes_for_text``，供 ``app.scenes.scene_ref_prompt`` 的
     ``prop_notes`` 必传参数使用——主场景定场图（2026-10-06，同一项目出租屋
     定场图把已有道具卡「鞋柜」画成带抽屉的高木柜，与道具卡本身登记的
     「原木色低矮开放搁板矮柜」互相矛盾，视频模型在两者之间摇摆）。

判据从文本本身推导：道具卡名/别名逐字出现在文本里才命中，不维护任何名单
（CLAUDE.md「禁止黑白名单与枚举穷举」）。
"""
from __future__ import annotations

from app.schemas import Prop


def _prop_name_occurrences(text: str, props: list[Prop]) -> list[tuple[int, int, Prop]]:
    """文本里逐字出现的道具卡名/别名候选区间：``(起点, 终点, 卡)``。同一张卡
    的正名与别名都命中时会产生多个候选，交给调用方按最长匹配取舍。"""
    spans: list[tuple[int, int, Prop]] = []
    for prop in props or []:
        names = {str(prop.name or "").strip()}
        names.update(str(a).strip() for a in (prop.aliases or []))
        for name in names:
            if not name:
                continue
            start = 0
            while True:
                idx = text.find(name, start)
                if idx < 0:
                    break
                spans.append((idx, idx + len(name), prop))
                start = idx + 1
    return spans


def _longest_nonoverlapping_props(spans: list[tuple[int, int, Prop]]) -> list[Prop]:
    """重叠匹配取最长：按命中长度降序贪心选择不重叠的区间（描述里写「顾屿
    外套」时不再把「外套」卡也套上），再按原文出现顺序去重同一张卡的多次
    命中（同一张卡只讲一次外观）。"""
    chosen: list[tuple[int, int]] = []
    picked: list[tuple[int, Prop]] = []
    for start, end, prop in sorted(spans, key=lambda s: -(s[1] - s[0])):
        if any(start < o_end and end > o_start for o_start, o_end in chosen):
            continue
        chosen.append((start, end))
        picked.append((start, prop))
    seen: set[int] = set()
    ordered: list[Prop] = []
    for _start, prop in sorted(picked, key=lambda p: p[0]):
        if id(prop) in seen:
            continue
        seen.add(id(prop))
        ordered.append(prop)
    return ordered


def matched_props_in_text(text: str, props: list[Prop]) -> list[Prop]:
    """文本里逐字出现的道具卡，按最长不重叠匹配、原文出现顺序去重后返回卡
    对象列表；没有命中返回空列表。"""
    text = text or ""
    if not text or not props:
        return []
    return _longest_nonoverlapping_props(_prop_name_occurrences(text, props))


def prop_notes_for_text(text: str, props: list[Prop]) -> str:
    """文本里逐字出现的道具卡，各追加一句正面陈述，告诉图像模型外观按卡画；
    不附带"此刻位置/状态"框架——供场景定场图这类跨集复用、不描述瞬间状态的
    锚点图使用。场景状态图自己的"位置与状态仍按描述走"版本见
    ``app.video_modes.scene_state_views.prop_appearance_notes_for_description``。
    文本里没提到的道具不提，判据从这段文本本身推导。"""
    matched = matched_props_in_text(text, props)
    sentences = [
        f"画面里的「{prop.name}」外观（颜色、材质、款式）按道具卡画：{prop.appearance_canonical}。"
        for prop in matched
    ]
    return " ".join(sentences)
