"""Seedance 参考图编号引用与中文用途说明（从 app.video_modes.seedance_pack
拆出，2026-09-03）。

拆出原因：seedance_pack.py 已在 app/FILE_CONVENTIONS.toml 的 line_count 棘轮
baseline 里（523 行，零余量），本次改造（正文 @角色名 确定性替换成 @图片N、
参考图用途说明从英文改中文并放在正文之后）要新增逻辑，这块逻辑本身只依赖
packed_refs 与 prompt_text，是纯函数，不依赖 seedance_pack.py 的任何私有
状态，天然可独立成模块。

对照 Seedance 2.0 官方指南（dexhunter/seedance2-skill 与
ambienceai.com/tutorials/seedance-prompting-guide）：参考图要用编号
`@图片N` 引用、并对每张图的用途给出明确说明，不要用人名做 @ 前缀再让模型
自己去猜哪张图对应谁；附加说明的语言也要跟正文一致（中文正文配中文说明），
不要在纯中文段落后面拼一段英文注释。

与 app.minimax_h3._tagged_prompt 的耦合（2026-09-03 修复背景里未列出、
实测发现的隐藏依赖）：这份说明文本是 Seedance 与 MiniMax H3 两个方言共用的
一份——app.media_exec.input_reference 在两种方言都适用的公共阶段统一调用
本模块生成，H3 适配器随后按“图片N”这个锚点把它替换成 `<Picture N>`。把
锚点从英文 "Reference image N" 改成中文 "图片N" 后，app/minimax_h3.py 里
匹配这个锚点的正则、以及判断“<Picture N> 是否已经在正文里出现过”的去重
正则（原来只认半角冒号）必须同步改，否则 H3 路径的 `<Picture N>` 替换会
静默失效、或去重失灵导致重复插入——这正是 CLAUDE.md「配套参数必须一起
传递」要挡的那类故障，已同步修改 app/minimax_h3.py 两处正则并补测试。
"""
from __future__ import annotations

import re
from typing import Any

REFERENCE_PROMPT_NOTE_MARKER = "参考图说明："
REFERENCE_SINGLE_INSTANCE_NOTE = (
    "参考图只用来锁定身份与环境外观；每个具名角色在画面里只出现一次。"
)

_TYPE_PURPOSE_ZH: dict[str, str] = {
    "character": "角色{who}的人物参考，只用来锁定长相与服装",
    "character_no_name": "人物参考，只用来锁定长相与服装",
    "scene": "场景参考，只用来锁定环境外观",
    "prop": "道具{who}参考，只用来锁定外观与材质",
    "prop_no_name": "道具参考，只用来锁定外观与材质",
    "style": "风格参考，只用来锁定画面风格",
    # 与 app.video_plan.prev_frame_reference.PREVIOUS_FRAME_PURPOSE_ZH 同一句（那边有测试锁住），
    # 这里不 import：video_modes 包不能反向依赖 video_plan.generate 所在的包初始化链。
    "previous_shot_frame": "上一段{who}画面参考，只用来锁定场景布局、家具与关键道具的位置和形态；人物的姿势与动作按本段文字描述，不沿用这张图",
}


def _related_names(ref: dict[str, Any]) -> list[str]:
    """与 seedance_pack._reference_identity_names 同一套取名逻辑：优先取
    relatedCharacterIds/related_character_ids，角色类型再补entity_name。"""
    related = [
        str(name).strip()
        for name in (ref.get("relatedCharacterIds") or ref.get("related_character_ids") or [])
        if str(name).strip()
    ]
    entity_name = str(ref.get("entity_name") or "").strip()
    if ref.get("type") == "character" and entity_name and entity_name not in related:
        related.append(entity_name)
    return related


def _plot_key_frame_purpose_zh(ref: dict[str, Any], who: str) -> str:
    who_part = f"{who}的" if who else ""
    beat_index = ref.get("keyframe_index") or "?"
    beat_total = ref.get("keyframe_total") or "?"
    time_ratio = ref.get("keyframe_time_ratio")
    purpose = f"{who_part}关键帧参考，只用来锁定该拍点画面"
    try:
        pct = round(float(time_ratio) * 100)
        purpose += f"（进度约{pct}%，第{beat_index}/{beat_total}拍）"
    except (TypeError, ValueError):
        purpose += f"（第{beat_index}/{beat_total}拍）"
    target = str(ref.get("keyframe_target_desc") or "").strip()
    if target:
        purpose += f"，目标画面：{target}"
    return purpose


def _reference_purpose_zh(ref: dict[str, Any]) -> tuple[str, list[str]]:
    """返回 (这张参考图的中文用途说明, 它绑定的具名人物/场景列表)。"""
    ref_type = str(ref.get("type") or "reference")
    related = _related_names(ref)
    who = "、".join(related)
    if ref_type == "plot_key_frame":
        return _plot_key_frame_purpose_zh(ref, who), related
    if ref_type == "character":
        template = _TYPE_PURPOSE_ZH["character" if who else "character_no_name"]
    elif ref_type == "prop":
        template = _TYPE_PURPOSE_ZH["prop" if who else "prop_no_name"]
    else:
        template = _TYPE_PURPOSE_ZH.get(ref_type, f"{ref_type}参考")
    return template.format(who=who), related


def _replace_at_mentions_with_picture_numbers(
    body: str, named_indices: dict[str, int],
) -> str:
    """把正文里完全匹配的 @名字 确定性替换成 @图片N，名字后面的空格/标点
    原样保留——只替换 "@名字" 这一段本身。按名字长度降序建正则候选：更长
    的名字先参与匹配，避免短名字先命中、把长名字截断成"短名字+残留字符"。
    """
    if not named_indices:
        return body
    ordered = sorted(named_indices, key=len, reverse=True)
    # EP1 重跑实测：模型从第 5 段起把 @李麦麦 写成了 @bible:李麦麦（identity_id 前缀漏进
    # 正文），精确匹配 @名字 全部落空，Seedance 拿到的是一串无绑定的 @bible:xxx。可选的
    # 「字母:」前缀一并吃掉，替换结果仍是 @图片N。
    pattern = re.compile("@(?:[A-Za-z_]+:)?(" + "|".join(re.escape(name) for name in ordered) + ")")
    return pattern.sub(lambda m: f"@图片{named_indices[m.group(1)]}", body)


def _compose_purposes(packed_refs: list[dict[str, Any]]) -> tuple[list[str], dict[str, int]]:
    purposes: list[str] = []
    named_indices: dict[str, int] = {}
    for idx, ref in enumerate(packed_refs, 1):
        purpose, related = _reference_purpose_zh(ref)
        purposes.append(f"图片{idx}：{purpose}")
        for name in related:
            named_indices.setdefault(name, idx)
    return purposes, named_indices


def build_seedance_reference_prompt_notes(
    prompt_text: str,
    packed_refs: list[dict[str, Any]],
    *,
    duration_s: float | int | None = None,
) -> str:
    """给 prompt_text 做两件事：① 正文里完全匹配的 @角色名/@场景名替换成
    @图片N；② 在正文之后追加一段中文参考图用途说明。没有任何参考图时
    （text-only 回退）原样返回，正文里的 @名字 不受影响。marker 幂等：
    已经带过说明的 prompt 不重复加。"""
    from app.compiler import _split_video_args

    if REFERENCE_PROMPT_NOTE_MARKER in prompt_text:
        return prompt_text
    prompt_body, prompt_args = _split_video_args(prompt_text, duration_s)
    purposes, named_indices = _compose_purposes(packed_refs)
    if not purposes:
        return prompt_text
    prompt_body = _replace_at_mentions_with_picture_numbers(prompt_body, named_indices)
    purpose_list = "；".join(purposes) + "；" + REFERENCE_SINGLE_INSTANCE_NOTE
    if prompt_body.startswith("subject_definitions:\n"):
        heading, body = prompt_body.split("\n", 1)
        return heading + "\n" + purpose_list + "\n" + body + prompt_args
    note = REFERENCE_PROMPT_NOTE_MARKER + "\n" + purpose_list
    return prompt_body + "\n" + note + prompt_args
