"""蒙太奇镜头形态（WS7，2026-09-02）：叙述者总结/回忆列举/跨年排比段落的分拍契约。

背景：分镜台 2.0.0（app.production.storyboard_pack）把原文按 15 秒切成
``shots`` 表一行；当这一段原文本身就是叙述者对多年经历的总结或排比（例如
「我八岁的时候被诊断出长不高……我三十五岁，在卡塔尔的夜里，把它抱在怀里」），
一镜一地一动作的假设不成立——这类段落天然跨越多个时间点/地点，被迫塞进
单一 scene_name 会产出一个与叙事内容无关的场景（实测：跑不快的孩子 第 2 集
一段荣誉列举被配上「校园食堂」）。``Shot.form == "montage"`` 是这类段落的
显式标记；``Shot.beats`` 承载段内最多 3 个独立拍点，每拍各自的时间锚点/
场景/画面，供 app.production.storyboard_dialects 的方言指令按拍生成
「镜头1（约0-X秒）……镜头2……」文本（视频侧 keyframe_sequence.beats 已支持
一镜内多段画面，这里补的是分镜台自己的段落判定/持久化契约）。

``form`` 默认 "scene"：绝大多数镜头（对白、动作明确的段落）保持现状，
不受这次改动影响——校验器与消费方必须在 ``form != "montage"`` 时立即放行，
不得因为新字段存在就改判旧数据行（app/validators/storyboard_montage.py 的
每个函数都以这条早退作为第一行）。
"""
from __future__ import annotations

from pydantic import BaseModel


class MontageBeat(BaseModel):
    """montage 镜头内部一个独立拍点，对齐视频侧 keyframe_sequence.beats。"""

    # 原文逐字给出的年份/年龄/时代锚点（例如「我八岁的时候」「三十五岁」）；
    # 原文没有明确锚点时留空，不得为了填满这个字段编造时间信息。
    time_anchor: str = ""
    # 必须是本集映射包已有场景的规范名；这一拍确实没有对应的具体场景
    # （抽象意象、无场景的纯人物特写等）时留空，不得自造场景名。
    scene_name: str = ""
    # 这一拍的画面描述：写这一拍具体要看见什么，不是原文的转述。
    visual: str = ""
    # 支撑这一拍画面判断的原文逐字片段（越短越好，只取判断所需的那一句）。
    source_span: str = ""


__all__ = ["MontageBeat"]
