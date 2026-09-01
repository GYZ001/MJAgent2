"""「这个称谓在 backend-owned 证据里有没有逐字出处」——查找与报错文案。

从 ``identity_response_projection.py`` 抽出（那个文件顶着行数棘轮，零余量）：
逐字查找本身是一句纯计算，而"没找到"要报什么，取决于**没找到的形态**——两种形态
的处置完全不同，混成一句话会让排查的人无从下手：

- **整批 0 命中**：这个名字在本批原文里一次都没出现过，是模型按作品常识/外部知识
  补出来的。真实事故 ERR-20260901-0fbce1 / ERR-20260901-ca388b（《金瓶梅词话》
  第一回，同一提示词连撞两次）：本批 16 条证据里「武大」出现 52 次、「武植」0 次，
  模型仍按民间说法写了本名「武植」，还绑到一条讲汉高祖四皓的证据上。治因在提示词
  （见 constants.IDENTITY_LITERAL_LABEL_RULE），这里只负责把现象说清楚。
- **命中多条**：名字确实在原文里，只是模型选错了 ``evidence_ref``，而逐字命中不止
  一条、无法确定改绑到哪一条（唯一命中时调用方已自动改绑，根本走不到报错）。

两种形态的处置不同，理由是"证据能不能证伪它"：

- **整批 0 命中**：这条记录可以被证据直接证伪——本次唯一允许的名字来源里，这个字符串
  一次都没有出现过。丢弃它是确定性的判断，不是猜测，也不是兜底填充（不补任何值）；
  留着它才是把一个原文不存在的人写进人物体系。丢弃走 WARNING 日志，不静默。
- **命中多条**：证据证明这个名字真的在原文里，只是不知道该绑哪一条——这是真正的
  "不确定"，维持 fail-closed 硬失败。

为什么 0 命中不再整集硬失败：实测（2026-09-01，《金瓶梅词话》第一回）线上两次、
本地三次连撞同一形态，模型每次都换一个名字（武植 / 武大郎 / 項羽——原文分别写
「武大」「武大」「項籍」），提示词三轮加强只把误报压小、消不掉：古典名著的人物本名
在模型先验里太强。硬失败的代价是整集永远过不去（重试必然复现），而这条记录本身
是可证伪的编造——把可确定处理的局部问题升级成整集停摆，判据就挂错了地方。
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def literal_owned_matches(
    source_label: str, evidence_by_ref: dict[str, Any] | None,
) -> list[Any]:
    """本批 owned 证据里 text 逐字包含 ``source_label`` 的那些条目。"""
    if not source_label:
        return []
    return [
        owned
        for owned in (evidence_by_ref or {}).values()
        if source_label in str((owned or {}).get("text") or "")
    ]


def named_literal_miss_verdict(
    source_label: str, evidence_by_ref: dict[str, Any] | None,
) -> str | None:
    """named 记录找不到逐字出处时怎么处置（形态与理由见模块 docstring）。

    返回 ``None`` 表示这条记录已被判定为凭空编造、就地丢弃（本函数已记 WARNING）；
    返回字符串表示歧义，调用方按业务校验错误硬失败。
    """
    matches = literal_owned_matches(source_label, evidence_by_ref)
    if matches:
        return (
            f"current named 缺少逐字 owned evidence：{source_label}"
            f"（所选证据里没有它，本批另有 {len(matches)} 条证据逐字含它，"
            "无法确定该改绑哪一条）"
        )
    log.warning(
        "current named 丢弃无出处名字：%s（本批 %d 条证据里一次都没有逐字出现，"
        "按外部知识补名，见 constants.IDENTITY_LITERAL_LABEL_RULE）",
        source_label, len(evidence_by_ref or {}),
    )
    return None
