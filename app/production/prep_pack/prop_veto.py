"""道具否决权的共享结构判据（2.0.18，从 ``prop_recheck.py`` 拆出）。

``prop_recheck.py`` 的否决机制现在有两条独立通路——对抽取已申报条目的
逐件裁决（``prop_recheck.py`` 自己）、对复核自己新发现候选的独立自查
（``prop_recheck_addition_confirm.py``）——两条通路共用同一套"模型提名、
代码核验"结构判据，单独抽出来放这里，避免互相 import 成环（见两个调用方
模块各自的 docstring）。

本文件只装结构性、可复用的部分：否决响应的 schema 片段（``_PropDeclaration
Veto``）、把已申报道具渲染成带序号清单供提示词嵌入（``_render_declared_
props``）、钉证二次核验（``_prop_veto_is_grounded``）、判重签名
（``_prop_mention_signature``）。语义判断（提示词怎么问、否决代表什么）
不在这里，留在各自的调用方模块——这里只管结构，不管措辞。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.source_excerpt import SourceSegment

from .schemas import _ModelPropMention

_VETO_LOG_PREFIX = "[PREP_PACK_PROP_DECLARATION_VETOED][未拦截]"
_VETO_UNGROUNDED_LOG_PREFIX = "[PREP_PACK_PROP_VETO_UNGROUNDED][未拦截]"


class _PropDeclarationVeto(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # 1-based，对应喂给模型那份"本块已申报道具清单"里的序号——用序号而不是
    # 让模型复述 label 字符串，避免转述走样导致代码侧匹配不上（同候选判别
    # 钉证"段号而非原文"的同一思路，见 functional_candidate_verdict.py）。
    declared_index: int
    criterion: str
    evidence_quote: str


def _render_declared_props(declared_props: list[_ModelPropMention]) -> str:
    """把一份道具清单（已申报条目或复核自己的新发现候选，两个调用方共用
    同一个序号约定）渲染成带序号的清单文本，供各自的提示词嵌入；序号即
    ``_PropDeclarationVeto.declared_index`` 要引用的值。空清单返回空字符串
    （调用方据此整段跳过否决环节，不问一个空名单）。"""
    lines = [
        f"{index}. label=「{item.label}」 外观=「{item.description}」 "
        f"出场编号={item.segment_indexes} "
        f"摘录=「{item.plot_significant_quote or item.source_wording}」"
        for index, item in enumerate(declared_props, start=1)
    ]
    return "\n".join(lines)


def _prop_veto_is_grounded(
    item: _ModelPropMention, evidence_quote: str, chunk_by_index: dict[int, SourceSegment],
) -> bool:
    """否决二次核验（2.0.18，见 ``prop_recheck.py`` 模块顶部大注释完整
    案情）：证据句必须同时满足①逐字出自这条申报自己 ``segment_indexes``
    某一编号的原文——不是卷宗/复核渲染过的文本，是代码直接检索的真实
    原文，天然杜绝转述失真；②包含这条申报自己的 label 或 source_wording
    ——防止模型拿一句跟这件道具毫无关系、但恰好也是原文逐字的句子来否决
    （同候选判别钉证"标签+候选都要在同一段证据里"的同一纪律，见
    functional_candidate_verdict._prep_pack_candidate_pin_is_grounded）。
    两者有一个不满足就不算否决成立，原申报原样保留。"""
    if not evidence_quote:
        return False
    label = (item.label or "").strip()
    source_wording = (item.source_wording or "").strip()
    if not (
        (label and label in evidence_quote)
        or (source_wording and source_wording in evidence_quote)
    ):
        return False
    return any(
        (segment := chunk_by_index.get(seg_index)) and evidence_quote in segment.text
        for seg_index in item.segment_indexes
    )


def _prop_mention_signature(mention: Any) -> set[str]:
    """判重用的候选字符串集合：label/known_prop_name/source_wording 三者
    的非空值（结构判据，不枚举具体道具名）。``mention`` 既可能是 dict
    （recheck 候选），也可能是 ``_ModelPropMention``（已有抽取结果）。"""
    get = mention.get if isinstance(mention, dict) else lambda k: getattr(mention, k, None)
    return {
        str(get(key) or "").strip()
        for key in ("label", "known_prop_name", "source_wording")
        if str(get(key) or "").strip()
    }
