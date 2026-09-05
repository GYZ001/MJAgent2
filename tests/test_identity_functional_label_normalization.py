"""functional source_label 里的分隔符/空白确定性剥掉（2026-09-05 我欲封天第 23 集「周、尹二人」）。"""

from __future__ import annotations

import pytest

from app.portraits.identity_schemas import CurrentFunctionalIdentityDecision


def _decision(label: str) -> CurrentFunctionalIdentityDecision:
    return CurrentFunctionalIdentityDecision(
        evidence_ref="E1", source_label=label, functional_identity_key="fk", kind="onscreen",
    )


@pytest.mark.parametrize("raw,expected", [
    ("周、尹二人", "周尹二人"),
    ("王腾飞 身后的男子", "王腾飞身后的男子"),
    ("围杀妖蟒的其他修士", "围杀妖蟒的其他修士"),
    ("周/尹，二人", "周尹二人"),
])
def test_list_separators_are_stripped_instead_of_rejected(raw, expected):
    assert _decision(raw).source_label == expected


def test_label_made_only_of_separators_is_still_rejected():
    with pytest.raises(ValueError):
        _decision("、、")
