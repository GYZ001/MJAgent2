"""建卡即登记别名：``ensure_character_card`` 落卡时把本次身份决议里同一
``identity_group`` 的其它 ``source_label`` 登记为 ``Character.aliases``。

背景：``app/portraits/card_owner.py``（另一个代理所有，只读）把"这个称呼是否已经
属于人物谱里的某个角色"收敛成别名感知匹配（name + aliases[].text），但落卡这一刻
从不写 aliases——新建的卡永远是"裸名"。下次同一个人换个称呼出现（比如人物谱记
"李富贵"，剧本这次写"小胖子"），``card_owner`` 的别名匹配没有东西可匹配，仍会
漏判、建出第二张卡。``card_owner.py`` 修的是"查得准"，这个模块修的是"查得到"。

证据口径与 ``app.stages.roster_recurring._attach_roster_source_appellations`` 的
"共现闸"同构（该函数已明确"这条免检通道只做共现检查，没有为排他性做过任何核
验"）：候选别名文本必须与角色规范名在同一章节原文里、彼此相距不超过一个短窗口
内共现，才登记；找不到共现证据的候选一律丢弃——不确定不登记，是 CharacterAlias
的既有合同（见 app/schemas.py:113 docstring），不是本模块新定的规则。``is_exclusive``
显式写 False：这里同样没有做过任何排他性核验，不该假装做过（同一口径见
app/stages/roster_recurring.py:196 附近注释）。

``app/portraits/cards_ensure.py`` 的 ``unknown_by_name`` 分组、
``app/identity_adjudication.py`` 的 ``identity.source_names`` 持有这份"同一
identity_group 的全部 source_label"数据，均已接线传入 ``ensure_character_card``。
参数默认 ``None``，未传（或传空列表）时 ``new_card_aliases`` 返回空列表，与历史
行为完全一致。
"""

from __future__ import annotations

import re

from app.portraits.constants import IDENTITY_NAME_FORM_REFERENTIAL
from app.schemas import CharacterAlias

# 与 roster_recurring._cooccurrence_quote 同一量级：短窗口，逐字命中，不做模糊匹配。
_COOCCURRENCE_WINDOW = 80


def _cooccurrence_evidence(
    chapters_by_idx: dict[int, str], anchor: str, label: str,
) -> tuple[int, str] | None:
    """在 ``chapters_by_idx``（``_forward_fragments`` 的第三个返回值，未截断的整章
    原文）里找一处 ``anchor``（角色规范名）与 ``label``（候选别名文本）彼此相距
    不超过一个短窗口共现的原文片段，返回 ``(章节 idx, 逐字引句)``；找不到返回
    ``None``——这条证据链路只做机械核验，不发起模型调用。
    """
    for idx, content in chapters_by_idx.items():
        if anchor not in content or label not in content:
            continue
        for match in re.finditer(re.escape(label), content):
            start = max(0, match.start() - _COOCCURRENCE_WINDOW)
            quote = content[start:start + _COOCCURRENCE_WINDOW * 2]
            if anchor in quote and label in quote:
                return idx, quote.replace("\n", "")
    return None


def new_card_aliases(
    name: str,
    identity_source_labels: list[str] | None,
    chapters_by_idx: dict[int, str],
) -> list[dict]:
    """``ensure_character_card`` 新建卡片时的 aliases 取值，供直接塞进
    ``Character.model_validate({..., "aliases": new_card_aliases(...)})``。

    每条候选 ``source_label`` 必须通过 ``_cooccurrence_evidence`` 的机械核验才
    登记；核验不通过（原文里找不到该称呼与规范名共现）的候选直接丢弃，不做任何
    猜测式兜底。返回值已经是 ``CharacterAlias.model_dump(mode="json")`` 的字典，
    与 ``verdict["relationships"]`` 等其它字段的形状一致。
    """
    aliases: list[dict] = []
    seen = {name}
    for raw in identity_source_labels or []:
        label = str(raw or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        found = _cooccurrence_evidence(chapters_by_idx, name, label)
        if found is None:
            continue
        chapter_idx, quote = found
        aliases.append(CharacterAlias(
            text=label,
            # 这条别名是代码按共现补回来的，没有模型标注过形态，就不替它下结论
            # （与 roster_recurring._attach_roster_source_appellations 同一口径）。
            name_kind=IDENTITY_NAME_FORM_REFERENTIAL,
            evidence_chapter_index=chapter_idx,
            evidence_quote=quote,
            is_exclusive=False,
        ).model_dump(mode="json"))
    return aliases
