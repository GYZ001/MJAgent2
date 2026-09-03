"""分镜里的群演描述性措辞与映射包登记的群演标签归并（WS12，持久化侧）。

背景：分镜台段落提示词生成时，映射台已登记的群演（``asset_manifest.
functional_extras``）连同 label/visual_entity_id/anchor_phrase 一起进了
``relevant_assets``，``_segment_shared_rules`` 也已要求模型 identity_id
只能逐字复制 ``relevant_assets.characters[].identity_id`` 或
``relevant_assets.functional_extras[].visual_entity_id`` 二选一（见
``app.production.storyboard_narrative_arc._segment_shared_rules``）——这条
正面陈述规则已在真实数据上验证有效（跨 4 个项目、18 个已生成分集实测：
凡是映射包为本段登记过的群演，``shots.characters`` 无一例外直接写的是
``entity:<hash>``，从未出现过带描述性措辞的重复造型）。

本模块处理的是这条规则失效时的兜底：EP7 的真实回归（见
``storyboard_pack.py`` 2.0.2 changelog）证明模型确实会偶尔无视规则、把
identity_id 写成自造前缀或原文描述——那一次是命名角色，同一失败模式对
群演同样成立、只是当时的候选判别/发现覆盖了本集全部群演所以没有本该
观察到的案例。``resolve_persisted_character_ids`` 只查
``asset_manifest.characters`` 与 ``appellation_map``，从不查
``functional_extras``，模型一旦写出描述性文字而不是登记的
visual_entity_id，这条文字会原样落进 ``shots.characters``、且此后每一镜
各自现编一次——三国白话 ep1 shot1 的「中年留三绺长须的藏青色官袍官员」
就是这类文字的真实样子（尽管核实后那三个官员本身在映射包里从未被登记过
——映射台发现缺口是另一个更大的问题，不在本模块职责内，本模块只处理
"已登记但模型没照抄"这一种情况）。

归并判据（两个条件缺一不可，都是确定性字符串/集合运算，不猜、不用模型）：
  1. 段落重叠：这条 characters 文字所在这一镜的 ``source_segment_indexes``
     与候选群演 L 的 ``segment_indexes`` 交集非空——L 是映射台在原文的哪个
     范围里发现的这个人，本镜画面对应的原文范围必须落在同一处，否则同一个
     描述性短语在原文不同地方完全可能指向不同的人。
  2. 文本包含：L.label 逐字是这段描述性文字的子串（"杂役"⊂"穿杂役衫的魁梧
     大汉"），或反过来这段文字逐字是 L.label 的子串——群演的 label 本身
     经常就是一句浓缩描述（"半百老道士""绿袍男子"），不是纯功能称谓，两个
     方向都要试。

同时满足两个候选群演的，是真正的歧义（同一段落范围内注册了两个字面上都能
命中的群演），不归并、原样保留描述性文字，另记一条可见 note——宁可让用户
看见"这里没法自动确定"，也不猜一个可能错的绑定（CLAUDE.md「不得兜底填充」）。
只命中 0 个候选的，本模块保持沉默：这种情况就是映射台压根没登记过这个人，
是发现覆盖率问题，已经由 ``_segment_content_advisories`` 的
``[STORYBOARD_PACK_RESOURCE_CHARACTER_UNKNOWN]`` 标记诚实报告过一次，本模块
重复报告没有信息增量。

层号：随 ``app.production`` 包前缀归 L4（app/LAYERS.toml），只依赖标准库。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 单字标签（"人""他"之类）逐字包含判据几乎必然假阳性——不是这条规则本身
#: 的判据变弱，是给"子串"这个操作符划一个最小可信长度，短于这个长度的
#: label 不参与归并（既不会被判定命中、也不会挡住其它候选），但仍然原样
#: 保留在 functional_extras 里、仍然享受 Step 1 的 payload/prompt 待遇。
_MIN_MERGE_LABEL_CHARS = 2

_AMBIGUOUS_NOTE_TAG = "STORYBOARD_PACK_EXTRA_RECONCILE_AMBIGUOUS"


@dataclass(frozen=True)
class ExtraReconcileResult:
    """单条 ``characters`` 文字的归并结果。"""

    #: 归并成功时是候选群演的 visual_entity_id；未归并（0 命中或歧义）时
    #: 原样等于传入的 raw_text——诚实回退，不猜一个值出来。
    resolved_id: str
    merged: bool
    #: 只有歧义（>=2 候选）时非空；0 候选时留空，见模块 docstring 最后一段。
    note: str | None = None


def _matches(label: str, raw_text: str) -> bool:
    # 空 raw_text 是空字符串对任何非空 label 都成立（``"" in label`` 恒真），
    # 会把"模型这条 identity_id 干脆没填"误判成"匹配了全部候选群演"——不是
    # 归并判据变弱，是先排除这个根本不构成"文本包含"的退化输入。
    if not raw_text or len(label) < _MIN_MERGE_LABEL_CHARS:
        return False
    return label in raw_text or raw_text in label


def reconcile_descriptive_extra(
    raw_text: str,
    segment_source_indexes: list[int],
    functional_extras: list[dict[str, Any]],
) -> ExtraReconcileResult:
    """对一条 ``characters`` 描述性文字尝试归并到已登记群演。

    ``raw_text`` 预期是已经过其它路径（``resolve_persisted_character_ids``
    的 manifest/appellation_map 两道）判定"查不到"之后才会走到这里的剩余
    情形；本函数自身不重复那两道判断，纯粹只做"能否归并到某个 functional
    extra"这一件事，调用方决定什么时候调用它。
    """
    wanted = set(segment_source_indexes)
    candidates: list[dict[str, Any]] = []
    for extra in functional_extras:
        label = str(extra.get("label") or "").strip()
        if not label:
            continue
        extra_segments = {int(i) for i in (extra.get("segment_indexes") or [])}
        if not (wanted & extra_segments):
            continue
        if _matches(label, raw_text):
            candidates.append(extra)

    if len(candidates) == 1:
        visual_entity_id = str(candidates[0].get("visual_entity_id") or "").strip()
        if visual_entity_id:
            return ExtraReconcileResult(resolved_id=visual_entity_id, merged=True)
        return ExtraReconcileResult(resolved_id=raw_text, merged=False)

    if len(candidates) > 1:
        labels = sorted({str(c.get("label") or "") for c in candidates})
        note = (
            f"[{_AMBIGUOUS_NOTE_TAG}][未拦截] characters 描述性措辞"
            f"「{raw_text}」在本镜原文范围内同时匹配 {len(candidates)} 个"
            f"映射台已登记群演（{'/'.join(labels)}），无法确定唯一归并对象，"
            f"已保留原措辞、未做归并"
        )
        return ExtraReconcileResult(resolved_id=raw_text, merged=False, note=note)

    return ExtraReconcileResult(resolved_id=raw_text, merged=False)


def reconcile_persisted_extra_ids(
    identity_ids: list[str],
    segment_source_indexes: list[int],
    functional_extras: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """批量版本，供持久化调用点一次性处理一镜的全部 identity_id。

    返回 ``(resolved_ids, notes)``——``resolved_ids`` 与入参 ``identity_ids``
    一一对应、等长；``notes`` 只收集歧义提示，顺序与触发顺序一致。
    """
    resolved: list[str] = []
    notes: list[str] = []
    for raw_id in identity_ids:
        result = reconcile_descriptive_extra(raw_id, segment_source_indexes, functional_extras)
        resolved.append(result.resolved_id)
        if result.note:
            notes.append(result.note)
    return resolved, notes


__all__ = [
    "ExtraReconcileResult",
    "reconcile_descriptive_extra",
    "reconcile_persisted_extra_ids",
]
