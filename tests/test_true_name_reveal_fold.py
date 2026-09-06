"""决议新签发的真名用的是既有卡的称谓 → 并入该卡，不再「同一称谓对应多个 authority」（第 14 轮第 29 集）。"""
from __future__ import annotations

from app.identity_authority import NAMED_FAMILY_RESOLUTIONS, identity_authority_registry
from app.schemas import Bible, Character, CharacterAlias, World

_APPEARANCE = "二十许岁女性，身着银袍，身形挺拔，腰间配储物袋"
_NAMED = sorted(NAMED_FAMILY_RESOLUTIONS)[0]


def _bible() -> Bible:
    return Bible(world=World(visual_style_canonical="国漫"), characters=[
        Character(name="许师姐", role="重要配角", appearance_canonical=_APPEARANCE, aliases=[
            CharacterAlias(text="许姓女子", name_kind="referential", evidence_chapter_index=5, evidence_quote="许姓女子迟疑了一下", is_exclusive=True),
        ]),
        Character(name="曹阳", role="反派", appearance_canonical=_APPEARANCE),
    ])


def _entry(registry, authority_id):
    return next(e for e in registry if e["authority_id"] == authority_id)


def test_revealed_true_name_folds_into_the_card_that_owns_the_label() -> None:
    registry = identity_authority_registry(_bible(), [
        {"source_label": "许师姐", "canonical_name": "许清", "resolution": _NAMED, "identity_group": "current-1:F4"},
    ])
    assert [e["authority_id"] for e in registry] == ["bible:许师姐", "bible:曹阳"]
    entry = _entry(registry, "bible:许师姐")
    assert entry["source_labels"] == ["许师姐", "许姓女子", "许清"]
    assert entry["true_name_candidates"] == ["许清"]


def test_alias_form_also_folds_and_unrelated_names_do_not() -> None:
    registry = identity_authority_registry(_bible(), [
        {"source_label": "许姓女子", "canonical_name": "许清", "resolution": _NAMED, "identity_group": "current-1:F1"},
        {"source_label": "王师兄", "canonical_name": "王腾飞", "resolution": _NAMED, "identity_group": "current-1:F2"},
    ])
    ids = [e["authority_id"] for e in registry]
    assert "bible:许清" not in ids and "bible:王腾飞" in ids  # 王腾飞的称谓不属于任何既有卡，照常独立
    assert "许清" in _entry(registry, "bible:许师姐")["source_labels"]


def test_existing_card_name_as_true_name_is_not_folded() -> None:
    registry = identity_authority_registry(_bible(), [
        {"source_label": "曹阳", "canonical_name": "曹阳", "resolution": _NAMED, "identity_group": "current-1:F1"},
    ])
    assert _entry(registry, "bible:曹阳")["source_labels"] == ["曹阳"] and "true_name_candidates" not in _entry(registry, "bible:曹阳")
