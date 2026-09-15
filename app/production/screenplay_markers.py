"""剧本体原文的结构标记：段头「【段 N｜地点｜时段】」、转场「【转场：X】」、必拍括号镜（格局镜/钩子）。

2026-09-15《龙猫出爪》第 1 集：原文是人间/爪间平行蒙太奇的快切剧本，作者给了衔接手段——换世界处写
「【转场：爪印光圈】」，段尾写「（格局镜：…）」「（钩子：切爪间，…灰线粗了一圈）」——分镜全部丢掉：
17 段一律「硬切」，钩子没拍，换场段还按「色温不同要写两秒渐变」把上一段色调残留进新场景，用户看到
13→14、16→17 衔接生硬。这里只做纯文本解析，判据全部来自原文自己的标记，不含任何剧情词表：

* 段头变化（地点或时段不同）= 换场；同一段头 = 同场；
* 显式「【转场：X】」优先，按 X 里的关键词映射到成片台已支持的转场名（app.final_edit.transition_spec）；
* 「（格局镜：…）」「（钩子：…）」是作者点名必须拍的镜头，原样交给分镜作 required_beats。
"""
from __future__ import annotations

import re

_SCENE_HEADER_RE = re.compile(r"【\s*段\s*[0-9０-９一二三四五六七八九十]+\s*[｜|]\s*([^｜|】]+?)\s*(?:[｜|]\s*([^】]+?))?\s*】")
_TRANSITION_RE = re.compile(r"【\s*转场\s*[：:]\s*([^】]+?)\s*】")
_REQUIRED_BEAT_RE = re.compile(r"[（(]\s*(格局镜|钩子|定场镜|收尾镜)\s*[：:]\s*([^）)]+?)\s*[）)]")

# 显式转场文字 → 成片台支持的转场名（见 app.final_edit.transition_spec）。关键词是转场手法的固定术语，
# 不是剧情词表；没命中的显式转场按「遮挡转场」处理——作者明确要了一个非硬切的手法。
_TRANSITION_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("叠化", "叠化"), ("溶", "叠化"),
    ("黑场", "淡出淡入"), ("淡出", "淡出淡入"), ("淡入", "淡出淡入"), ("闪黑", "淡出淡入"),
    ("闪白", "闪白"), ("白闪", "闪白"),
    ("甩", "甩镜"),
    ("匹配", "匹配剪辑"),
    ("光圈", "遮挡转场"), ("圈", "遮挡转场"), ("遮挡", "遮挡转场"), ("擦", "遮挡转场"), ("划", "遮挡转场"),
)
SCENE_CHANGE_TRANSITION = "叠化"
SAME_SCENE_TRANSITION = "硬切"


def parse_scene_header(text: str) -> tuple[str, str] | None:
    """返回 (地点, 时段)；没有段头返回 None。取文本里第一个段头。"""
    match = _SCENE_HEADER_RE.search(text or "")
    if not match:
        return None
    return match.group(1).strip(), (match.group(2) or "").strip()


def explicit_transition_marker(text: str) -> str | None:
    """文本里最后一个「【转场：X】」的 X；没有返回 None。"""
    found = _TRANSITION_RE.findall(text or "")
    return found[-1].strip() if found else None


def map_transition(marker: str) -> str:
    for keyword, name in _TRANSITION_KEYWORDS:
        if keyword in marker:
            return name
    return "遮挡转场"


def required_beats(text: str) -> list[str]:
    """作者点名必拍的括号镜：「（格局镜：…）」「（钩子：…）」，按出现顺序，保留标签便于分镜引用。"""
    return [f"{label}：{body.strip()}" for label, body in _REQUIRED_BEAT_RE.findall(text or "")]


def _same_place(left: str, right: str) -> bool:
    """段头地点按「·」分层，作者常省略中间层（「人间·医院·诊室」与「人间·诊室」、「爪间·掌心接线台」与
    「爪间·接线台」是同一处）：最末一层互为后缀即同一地点。"""
    a = (left or "").split("·")[-1].strip()
    b = (right or "").split("·")[-1].strip()
    return bool(a and b) and (a.endswith(b) or b.endswith(a))


def scene_changed(previous_text: str, current_text: str) -> bool:
    """本段与上一段是否换场：两边都有段头且地点或时段不同；本段有段头而上一段没有也算换场；
    两段覆盖同一原文（容量拆分）永远是同场。"""
    if previous_text and previous_text == current_text:
        return False
    current = parse_scene_header(current_text)
    if current is None:
        return False
    previous = parse_scene_header(previous_text)
    if previous is None:
        return True
    return not (_same_place(previous[0], current[0]) and previous[1] == current[1])


def transition_between(previous_text: str, current_text: str) -> str:
    """上一段 → 本段的转场：显式标记（写在上一段末尾或本段开头）优先；换场用叠化；同场硬切。"""
    if previous_text and previous_text == current_text:
        return SAME_SCENE_TRANSITION  # 同一原文段拆成两段：段尾的【转场】属于这场戏的结束，不在两半之间
    marker = explicit_transition_marker(_tail(previous_text)) or explicit_transition_marker(_head(current_text))
    if marker:
        return map_transition(marker)
    return SCENE_CHANGE_TRANSITION if scene_changed(previous_text, current_text) else SAME_SCENE_TRANSITION


def _tail(text: str, chars: int = 120) -> str:
    return (text or "")[-chars:]


def _head(text: str, chars: int = 120) -> str:
    return (text or "")[:chars]


__all__ = [
    "SAME_SCENE_TRANSITION", "SCENE_CHANGE_TRANSITION", "explicit_transition_marker", "map_transition",
    "parse_scene_header", "required_beats", "scene_changed", "transition_between",
]


def segment_structure(previous_text: str, current_text: str) -> dict:
    """一段的结构事实（喂给分镜模型、也落库）：是否换场、上一段到本段的转场、作者点名必拍的镜头。"""
    return {
        "scene_change": scene_changed(previous_text, current_text),
        "transition_from_previous": transition_between(previous_text, current_text) if previous_text else SAME_SCENE_TRANSITION,
        "required_beats": required_beats(current_text),
    }


def structure_rules(structure: dict) -> list[str]:
    """阶段二 rules[] 的正面陈述：换场段的起幅怎么写、同场段怎么接、必拍镜头怎么放。"""
    rules: list[str] = []
    if structure.get("scene_change"):
        rules.append(
            f"本段换了场景（task_payload.scene_change=true，与上一段之间的转场是「{structure.get('transition_from_previous')}」，"
            "由成片阶段渲染，你不用写转场本身）：镜头1 是新场景自己的定场镜，直接用本段的光线与色温开画，"
            "不写「残留上一段色调」「两秒内过渡」这类从上一段延续过来的画面；人物以他们在本段原文里的第一个"
            "动作入画，不沿用上一段的站位。"
        )
    else:
        rules.append(
            "本段与上一段是同一场戏（task_payload.scene_change=false）：镜头1 的人物位置、朝向与手里的东西从上一段"
            "末镜的状态起（见 previous_continuity_memo.layout），观众感觉是同一个镜头往下拍，不重开机位。"
        )
    beats = structure.get("required_beats") or []
    if beats:
        listed = "；".join(f"「{b}」" for b in beats)
        rules.append(
            f"本段原文里作者点名的必拍镜头（task_payload.required_beats）：{listed}。每一条各自成一镜、按原文顺序"
            "排在本段动作之后（钩子镜排最后），画面内容照原文写；「切爪间/切某地」这类钩子就是本镜切到那个地点。"
        )
    return rules


def required_beats_errors(draft, beats: list[str]) -> list[str]:
    """作者点名的必拍镜头必须出现在 prompt_text 里：按二元组覆盖率核对（同 textmatch 关键台词判据）。"""
    from app import textmatch

    prompt = str(getattr(draft, "prompt_text", "") or "")
    missing = [
        beat for beat in beats
        if textmatch.bigram_coverage(beat.split("：", 1)[-1], prompt) < textmatch.KEY_LINE_BIGRAM_COVERAGE
    ]
    if not missing:
        return []
    return [
        "原文作者点名的必拍镜头没有出现在本段镜头里：" + "；".join(f"「{m}」" for m in missing)
        + "。请各自补成一镜（按原文顺序、钩子镜排最后），画面内容按原文写，不改台词。"
    ]


__all__ = [*__all__, "required_beats_errors", "segment_structure", "structure_rules"]


def joined_source_text(segments, indexes) -> str:
    """按原文段号（1 起）拼出这一段分镜覆盖的原文全文；越界段号忽略。"""
    return "\n".join(segments[i - 1].text for i in (indexes or []) if 0 < i <= len(segments))


__all__ = [*__all__, "joined_source_text"]
