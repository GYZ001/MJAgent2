"""分镜台阶段二：项目级「统一配乐」开关的方言追加规则 + 确定性回填（2026-09-28）。

背景：视频模型每段各自生成一条 15 秒配乐，段与段之间硬切断裂；成片合成阶段将统一
铺一条贯穿全集、随情节切换的配乐（另一个任务落地），因此分镜台这一层反过来要让
每段视频**不带自己的配乐**，只留人物对白与环境音。2026-09-29 真实实测：把分镜
「全片贯穿」段的声音描述从「配乐为……」换成「无任何背景音乐……，声音只有人物
对白与环境音」后，Seedance 生成片段的频谱里旋律完全消失——模型确实遵守这条指令。

开关是项目级的（``app.project_settings.enhance_music_bed_enabled``），关闭时本模块
两个函数都是无操作（``music_bed_dialect_addendum`` 返回空串、
``ensure_no_music_bed_in_prompt`` 直接返回空列表且不碰 ``prompt_text``），因此接线
处（``app.production.storyboard_pack._generate_all_segment_prompts``）在关闭时的
输出与本次改动之前逐字相同。

两条判据都不是黑名单式的语义分类：「配乐」是我们自己写进
``storyboard_dialects.SEEDANCE_DIALECT_INSTRUCTIONS`` 的固定模板关键字（要求模型
结尾必须写「配乐……」），「non_diegetic_music:」是 H3 方言的固定字段名字面量，
两者都是本项目自定提示词契约的一部分，不是对模型自由文本做开放式语义判断，与
``storyboard_dialogue_attribution._TAIL_MARKER``（同样匹配「全片贯穿」字面量）
同一先例。

接线方式与调用点约束见
``app.production.storyboard_shot_mandates``（两个模型方言各自的文案在阶段二对
每一段都无条件拼进 ``dialect_instructions``，不依赖任何提名）；
``storyboard_pack.py`` 已在行数与 ``_generate_all_segment_prompts`` 函数行数两条
棘轮基线上零余量，因此本模块的一切新逻辑都封装成独立可调用的函数，接线处只需
在既有物理行内追加一次函数调用，不新增物理行。
"""
from __future__ import annotations

import re
from typing import Any

#: 「全片贯穿」段落只汇总环境音/配乐/风格/约束四类信息，标记与
#: storyboard_dialogue_attribution._TAIL_MARKER 同一字面量（各自模块独立定义，
#: 都是本项目提示词模板的固定关键字，不互相 import 是因为二者关注点不同：那边
#: 剥台词，这里改配乐描述）。
_TAIL_MARKER = "全片贯穿"

#: Seedance 侧统一配乐开启时，「全片贯穿」段落配乐描述要写成的固定表述——与
#: 2026-09-29 真实实测通过的措辞一致。
NO_MUSIC_SEEDANCE_PHRASE = "无任何背景音乐（成片统一配乐），声音只有人物对白与环境音"

#: 配乐分句：从「配乐」起（可跟「：」「:」「为」）到下一个分隔符（；;。换行）或
#: 全文结尾为止；只匹配我们自己要求模型写的固定关键字，不是开放式语义判断。
_MUSIC_CLAUSE_RE = re.compile(r"配乐[：:为]?[^；;。\n]*")

SEEDANCE_MUSIC_BED_RULE = (
    "本项目已开启统一配乐：成片合成阶段会为全集统一铺一条贯穿全片、随情节切换的"
    "配乐，因此这一段视频本身不需要也不应该带配乐。结尾「全片贯穿」段的声音描述"
    "不写任何配乐：把配乐部分整体写成"
    f"「{NO_MUSIC_SEEDANCE_PHRASE}」，"
    "环境音、风格、约束三项仍按原有规则照常写、不能省略。"
)

MINIMAX_H3_MUSIC_BED_RULE = (
    "This project has a unified score enabled: the final-edit stage lays down one "
    "continuous score across the whole episode that shifts with the story, so this "
    "individual clip must not carry its own music. Write non_diegetic_music exactly "
    "as \"N/A\" -- do not describe any instrument, tempo, rhythm, or mood for "
    "background music. overall_soundscape still follows the existing rule (ambient "
    "sound and dialogue only, never left empty)."
)


def music_bed_dialect_addendum(render_format: str, *, enabled: bool) -> str:
    """项目关闭统一配乐时返回空串——拼进 ``dialect_instructions`` 的 f-string 后
    逐字不变；开启时返回带前导换行的正面陈述规则，按 ``render_format`` 选对应
    方言，接线方式与 ``storyboard_shot_mandates.shot_mandates_dialect_rule`` 同源。
    """
    if not enabled:
        return ""
    rule = MINIMAX_H3_MUSIC_BED_RULE if render_format == "minimax_h3_native_fields" else SEEDANCE_MUSIC_BED_RULE
    return "\n" + rule


def _rewrite_seedance_music_phrase(prompt_text: str) -> str:
    """把「全片贯穿」段里的配乐描述改写成 ``NO_MUSIC_SEEDANCE_PHRASE``；没有配乐
    描述（甚至没有「全片贯穿」段）就原样追加，保证开关开启时这句表述一定存在。
    """
    if NO_MUSIC_SEEDANCE_PHRASE in prompt_text:
        return prompt_text
    idx = prompt_text.rfind(_TAIL_MARKER)
    if idx < 0:
        return prompt_text.rstrip() + "\n" + NO_MUSIC_SEEDANCE_PHRASE + "。"
    head, tail = prompt_text[:idx], prompt_text[idx:]
    if _MUSIC_CLAUSE_RE.search(tail):
        return head + _MUSIC_CLAUSE_RE.sub(NO_MUSIC_SEEDANCE_PHRASE, tail, count=1)
    return prompt_text.rstrip() + "；" + NO_MUSIC_SEEDANCE_PHRASE


_H3_MUSIC_FIELD_RE = re.compile(r"non_diegetic_music:\s*[^\n]*")
_H3_MUSIC_FIELD_LINE = "non_diegetic_music: N/A"


def _rewrite_h3_non_diegetic_music(prompt_text: str) -> str:
    """把 ``non_diegetic_music:`` 字段整行改写成 ``N/A``；字段本身缺失就原样
    追加，保证开关开启时这个字段一定等于 N/A。"""
    match = _H3_MUSIC_FIELD_RE.search(prompt_text)
    if match is None:
        return prompt_text.rstrip() + "\n\n" + _H3_MUSIC_FIELD_LINE
    if match.group(0).strip() == _H3_MUSIC_FIELD_LINE:
        return prompt_text
    return prompt_text[:match.start()] + _H3_MUSIC_FIELD_LINE + prompt_text[match.end():]


def ensure_no_music_bed_in_prompt(draft: Any, *, render_format: str, enabled: bool) -> list[str]:
    """确定性回填：项目关闭统一配乐时直接返回空列表、不碰 ``draft.prompt_text``
    （逐字不变）；开启时按方言把配乐描述改写为「无配乐」表述。返回值恒为空
    列表——这是确定性回填，不是校验，不参与语义重试/失败判定，与
    ``storyboard_cast_lock.ensure_cast_lock_in_prompt`` 同一先例。
    """
    if not enabled:
        return []
    prompt = str(getattr(draft, "prompt_text", "") or "")
    if not prompt.strip():
        return []
    draft.prompt_text = (
        _rewrite_h3_non_diegetic_music(prompt)
        if render_format == "minimax_h3_native_fields"
        else _rewrite_seedance_music_phrase(prompt)
    )
    return []


__all__ = [
    "NO_MUSIC_SEEDANCE_PHRASE",
    "SEEDANCE_MUSIC_BED_RULE",
    "MINIMAX_H3_MUSIC_BED_RULE",
    "music_bed_dialect_addendum",
    "ensure_no_music_bed_in_prompt",
]
