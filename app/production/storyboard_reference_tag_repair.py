"""分镜段 @ 引用与紧随镜头描述连写时的确定性修补（模型提名、代码核验）。

2026-09-24 真实故障（B 机沙箱、生产库副本、真实文本模型，《我欲封天》EP3 分镜
第二步）：``StructuredSemanticError: ... 业务校验失败：图片引用 @孟浩肩后看向对面的
没有对应的可见角色或场景``——语义重试耗尽，整集失败。根因是
``storyboard_identity_validation.final_identity_prompt_errors`` 用
``re.findall(r"@([\\w:-]+)", prompt)`` 取引用名，Python 的 ``\\w`` 匹配汉字，
中文又没有空格分隔，模型把 ``@孟浩`` 与后面的镜头描述连写时整串被当成一个不存在
的名字。模型重试也可能再犯同一种连写，不是稳定可用重试兜底的错误类别。

只做机械的一件事：@X 的 X 不是本段合法名（与校验同一份名单，见
``storyboard_identity_validation.visible_reference_names``），但以某个合法名开头时，
按最长匹配把 @X 拆成「@N + 一个空格 + 剩余原文」，不删一个字；多个合法名互为前缀
（如「孟浩」「孟浩然」）取最长的那个。X 不以任何合法名开头的未知引用原样保留，
交给 ``final_identity_prompt_errors`` 如实报错——不猜测缺失数据。

只在生成路径（``storyboard_identity_generation.generated_identity_errors``）接线；
人工提交路径（``storyboard_identity_submission``）不复用——人工文本是用户显式提交
的内容，默认不自动改写。
"""
from __future__ import annotations

import logging
import re

from app.production.storyboard_identity_validation import visible_reference_names

log = logging.getLogger(__name__)

#: 与 final_identity_prompt_errors 完全相同的引用正则，取名的判据必须两边一致。
REFERENCE_TAG = re.compile(r"@([\w:-]+)")


def repair_reference_tags(prompt: str, names: set[str]) -> tuple[str, list[str]]:
    """纯函数：按最长合法名前缀拆开连写的 @ 引用，返回新文本与被拆开的原始引用。"""
    if not prompt or "@" not in prompt or not names:
        return prompt, []
    ordered = sorted((name for name in names if name), key=len, reverse=True)
    split_from: list[str] = []

    def _split(match: re.Match[str]) -> str:
        raw = match.group(1)
        if raw in names:
            return match.group(0)
        prefix = next((name for name in ordered if raw.startswith(name)), "")
        if not prefix:
            return match.group(0)
        split_from.append(raw)
        return f"@{prefix} {raw[len(prefix):]}"

    repaired = REFERENCE_TAG.sub(_split, prompt)
    return repaired, split_from


def repair_segment_reference_tags(segment: dict) -> list[str]:
    """对 prompt_text 与（若存在）speech_template 做完全相同的修补，保持两者一致。

    speech_template 是渲染前的模板源头；render_segment_speech 只替换
    ``{{speech:Uxx}}`` 占位符，@ 引用原样透传到 prompt_text，所以两个字段各自
    独立修补即可保持一致，不需要重新渲染。生成路径首次校验时 speech_template
    恒为空（模型只产出 prompt_text），这里仍处理它是为了这份修补对「已带
    speech_template 的段」同样正确，不依赖调用顺序的偶然性。
    """
    names = visible_reference_names(segment)
    fixed: list[str] = []
    for field in ("prompt_text", "speech_template"):
        text = str(segment.get(field) or "")
        if not text:
            continue
        repaired, changed = repair_reference_tags(text, names)
        if changed:
            segment[field] = repaired
            fixed.extend(changed)
    return sorted(set(fixed))


__all__ = ["repair_reference_tags", "repair_segment_reference_tags"]
