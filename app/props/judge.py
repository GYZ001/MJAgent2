"""关键道具判据 + 外观锚点生成（模型申报 + 代码核验，不用道具名/关键词黑白名单）。

只登记"关键道具"，不是每一次提及都建库：一次性出现的背景物件（用户举例：路过
桌上的一只杯子）建库只会浪费出图成本、稀释真正需要跨集稳定的道具。判据从数据
推导，四选一（结构信号，零语义，不针对任何具体道具名做特判）：
  a) mention.segment_indexes 去重后覆盖 ≥2 个原文段——跨段落反复出现，说明
     不是一次性入镜；
  b) description 按中文顿号/逗号/分号/空白切分后，非空子句数 ≥3——结构上
     等价于"对材质/颜色/结构/尺寸/标志物做了多维度描述"（契约要求
     appearance_canonical 本身就是三项以上可视觉验证特征），不检查具体是
     哪些词、只数分句密度；一次性路过的背景物件描述往往只有一个笼统短语，
     分句数量天然达不到这个密度。
  c) 道具在本集原文里被反复提到：label 本身、或 label 的「中心词」（≥2 字的最长后缀，
     中文名词短语是修饰语 + 中心词的结构，「旧猫包」的中心词是「猫包」）在原文里出现
     ≥2 次——真实投诉的「旧猫包」在 EP1 原文里只占一个原文段、描述只有一句，但「猫包」
     出现 5 次，正是跨段被反复拍到、最容易漂移的那类道具。
  d) 模型提名 + 代码核验（2026-09-28 新增，真实缺陷：《顾念长安》第1集贴身佩戴的
     黄铜旧星盘、只出现一次的童年合影都漏建卡——它们是剧情伏笔/交接物，不是背景
     陈设，但只在原文里出现一两次、描述也往往只有一句，前三条结构信号天然覆盖
     不到）：mention.plot_significant=true 且 plot_significant_quote 逐字命中
     source_text——这不是"模型说重要就信"，是模型必须交出可核验的原文证据（这件
     物品在剧情里被拿起/交接/特写/作为伏笔反复强调），代码只核验证据是否真实存在，
     不判断"重要"这件事本身该不该成立。证据编造（quote 在原文里查无实据）一律
     不采信，不走这一条。
四条都不满足时不发起模型调用（省成本），标签维持"只有 label+description 文字
描述"的原状，与此前完全一致——不是退化，是本来就不该入库。
"""
from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field

from app.harness import model_gateway

_CLAUSE_SPLIT_RE = re.compile(r"[、，,;；\s]+")
MIN_SEGMENT_COUNT = 2
MIN_DESCRIPTION_CLAUSES = 3
MIN_APPEARANCE_FEATURES = 3
MIN_SOURCE_OCCURRENCES = 2
MIN_HEAD_NOUN_CHARS = 2


def _description_clause_count(description: str) -> int:
    return len([part for part in _CLAUSE_SPLIT_RE.split(description.strip()) if part.strip()])


def source_occurrences(label: str, source_text: str) -> int:
    """label 或其中心词（≥2 字的最长后缀）在原文里的出现次数，取最大者。"""
    label = (label or "").strip()
    if not label or not source_text:
        return 0
    best = 0
    for start in range(0, len(label) - MIN_HEAD_NOUN_CHARS + 1):
        best = max(best, source_text.count(label[start:]))
    return best


def is_plot_significant_prop_mention(mention: dict, *, source_text: str) -> bool:
    """判据 d) 的独立核验：模型提名 + 代码核验，见模块 docstring。``source_text``
    为空时结构上不可能核验出任何一条 quote，直接返回 False（不是"宽松放过"，是
    "没有原文可核对，就不能采信"）。"""
    if not mention.get("plot_significant") or not source_text:
        return False
    quote = str(mention.get("plot_significant_quote") or "").strip()
    return bool(quote) and quote in source_text


def is_key_prop_mention(mention: dict, *, source_text: str = "") -> bool:
    """纯数据结构判据，不发模型调用；``source_text`` 为空时只看前两条。"""
    segment_indexes = {int(i) for i in mention.get("segment_indexes") or []}
    if len(segment_indexes) >= MIN_SEGMENT_COUNT:
        return True
    description = str(mention.get("description") or "")
    if _description_clause_count(description) >= MIN_DESCRIPTION_CLAUSES:
        return True
    if source_occurrences(str(mention.get("label") or ""), source_text) >= MIN_SOURCE_OCCURRENCES:
        return True
    return is_plot_significant_prop_mention(mention, source_text=source_text)


class _PropAppearanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appearance_canonical: str
    aliases: list[str] = Field(default_factory=list)


async def assess_prop_appearance(
    label: str, description: str, *, style: str, ep_label: str,
) -> dict:
    """从 label+description 写出跨集稳定的道具锚点串（三项以上可视觉验证特征）
    与该道具在本集原文里可能出现的别称。返回 ``{"appearance_canonical", "aliases"}``；
    appearance_canonical 不满足最少特征数时兜底裁剪为 description 本身（不空转）。
    """
    prompt = f"""任务：为漫剧道具库写一条【规范外观锚点】，供后续跨集出图保持形态一致
（用户投诉根因：同一件道具在不同集画得不一样，因为此前没有素材库锚定）。

道具标签：{label}
本集描述：{description}
画风：{style}
所属集数：{ep_label}

要求：
- appearance_canonical 必须是至少 3 项可视觉验证特征的拼接（材质/颜色/结构/尺寸/
  标志物等，任选其中的具体项，不要求逐一覆盖这五类），30~120 字，只写可画出来的
  静态外观，不写动作/剧情。每一项特征都必须来自上面给出的「本集描述」，不得编造
  描述里没有提到的新特征。
- 「本集描述」里若出现多条互相不一致的描述（同一道具在不同镜头被写出不同细节），
  选取其中最连贯自洽的一版作为最终外观，不要把矛盾的细节硬拼在一起。
- appearance_canonical 只写「这件道具单独摆出来、周围没有别的东西时，它自己身上
  看得到」的材质、颜色、结构、版型、以及它表面本来就有的图案花纹。描述里提到的
  其它物件——藏在它里面的、挂在它上面的、放在它旁边的、被它包住或遮住的东西，
  以及人物动作在它上面留下的印痕（按压出的凹痕、蹭上的痕迹等）——都属于那件
  别的物件或当时的剧情本身，它们各自有自己的道具卡或分镜画面去表现，不写进
  这件道具的 appearance_canonical。
- aliases 列出这件道具在描述中可能出现的其它称呼（无则给空数组），不得虚构。
输出 JSON：{{"appearance_canonical": str, "aliases": [str]}}"""
    response = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_PropAppearanceResponse,
        validate=None,
        operation_id="assess_prop_appearance:" + hashlib.sha256(
            f"{label}:{description}:{ep_label}".encode("utf-8")
        ).hexdigest(),
        temperature=0.2,
        max_tokens=500,
        call_meta={"stage": "assess_prop_appearance", "prop_label": label},
    )
    appearance = response.appearance_canonical.strip()
    if _description_clause_count(appearance) < MIN_APPEARANCE_FEATURES:
        appearance = f"{appearance}。{description.strip()}" if appearance else description.strip()
    aliases = [a.strip() for a in response.aliases if a.strip() and a.strip() != label]
    return {"appearance_canonical": appearance, "aliases": aliases}
