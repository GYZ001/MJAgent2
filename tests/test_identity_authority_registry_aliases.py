"""红→绿测试：``app.identity_authority.identity_authority_registry`` 并入
``Character.aliases``（持久别名，docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1）。

覆盖：
1. 已登记别名并入该角色 authority 的 source_labels（断点一闭环）。
2. 别名严格按所属角色隔离——不得让另一个角色的 authority 也收到它
   （防"张冠李戴"，见 app/portraits.py 断点二修复要求的反例二）。
3. 防御：别名字段缺失/空串/与真名或彼此重复时不崩溃、不重复收录。
4. 无别名角色（存量数据）行为逐字不变。
"""
from __future__ import annotations

from app.identity_authority import identity_authority_registry
from app.schemas import Bible, Character, CharacterAlias, World


def _bible(*characters: Character) -> Bible:
    return Bible(world=World(visual_style_canonical="国风"), characters=list(characters))


def _entry(registry: list[dict], authority_id: str) -> dict:
    return next(item for item in registry if item["authority_id"] == authority_id)


class TestAliasesJoinSourceLabels:
    def test_registered_alias_is_added_to_owning_authority(self) -> None:
        bible = _bible(Character(
            name="许清",
            role="重要配角",
            appearance_canonical="青衣长剑，眉目清冷",
            aliases=[CharacterAlias(
                text="许师姐",
                name_kind="honorific",
                evidence_chapter_index=1,
                evidence_quote="许师姐提剑而立",
            )],
        ))
        registry = identity_authority_registry(bible, [])
        entry = _entry(registry, "bible:许清")
        assert entry["canonical_name"] == "许清"
        assert entry["source_labels"] == ["许清", "许师姐"]

    def test_multiple_aliases_all_join_in_declared_order(self) -> None:
        bible = _bible(Character(
            name="李富贵",
            role="重要配角",
            appearance_canonical="圆脸胖少年",
            aliases=[
                CharacterAlias(
                    text="小胖子", name_kind="referential",
                    evidence_chapter_index=1, evidence_quote="小胖子跑了过来",
                ),
                CharacterAlias(
                    text="胖师兄", name_kind="honorific",
                    evidence_chapter_index=3, evidence_quote="胖师兄笑着摆手",
                ),
            ],
        ))
        registry = identity_authority_registry(bible, [])
        entry = _entry(registry, "bible:李富贵")
        assert entry["source_labels"] == ["李富贵", "小胖子", "胖师兄"]

    def test_character_without_aliases_is_unaffected(self) -> None:
        # 存量数据（无别名字段/空列表）行为必须逐字不变。
        bible = _bible(Character(
            name="王平", role="配角", appearance_canonical="布衣短打",
        ))
        registry = identity_authority_registry(bible, [])
        entry = _entry(registry, "bible:王平")
        assert entry["source_labels"] == ["王平"]


class TestAliasOwnershipIsIsolated:
    """反例二守边界：别名只能并入它真正所属的角色，不得波及同名字面的其他角色。"""

    def test_alias_never_leaks_into_a_different_character_entry(self) -> None:
        bible = _bible(
            Character(
                name="许清", role="重要配角", appearance_canonical="青衣长剑",
                aliases=[CharacterAlias(
                    text="师姐", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="师姐来了",
                )],
            ),
            Character(
                name="沈婉", role="重要配角", appearance_canonical="红衣软剑",
            ),
        )
        registry = identity_authority_registry(bible, [])
        xu_qing = _entry(registry, "bible:许清")
        shen_wan = _entry(registry, "bible:沈婉")
        assert "师姐" in xu_qing["source_labels"]
        assert "师姐" not in shen_wan["source_labels"]

    def test_same_literal_alias_text_stays_scoped_per_owner(self) -> None:
        # 两个角色各自拥有字面相同的别名文本（如都被称"师姐"），各自只影响
        # 自己的 authority，不得合并或互相污染。
        bible = _bible(
            Character(
                name="许清", role="重要配角", appearance_canonical="青衣长剑",
                aliases=[CharacterAlias(
                    text="师姐", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="许师姐来了",
                )],
            ),
            Character(
                name="沈婉", role="重要配角", appearance_canonical="红衣软剑",
                aliases=[CharacterAlias(
                    text="师姐", name_kind="honorific",
                    evidence_chapter_index=2, evidence_quote="沈师姐来了",
                )],
            ),
        )
        registry = identity_authority_registry(bible, [])
        xu_qing = _entry(registry, "bible:许清")
        shen_wan = _entry(registry, "bible:沈婉")
        assert xu_qing["source_labels"] == ["许清", "师姐"]
        assert shen_wan["source_labels"] == ["沈婉", "师姐"]


class TestAliasDefensiveHandling:
    def test_blank_and_duplicate_alias_text_is_dropped(self) -> None:
        bible = _bible(Character(
            name="许清", role="重要配角", appearance_canonical="青衣长剑",
            aliases=[
                CharacterAlias(
                    text="  ", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="",
                ),
                CharacterAlias(
                    text="许师姐", name_kind="honorific",
                    evidence_chapter_index=1, evidence_quote="许师姐来了",
                ),
                CharacterAlias(
                    text="许师姐", name_kind="honorific",
                    evidence_chapter_index=5, evidence_quote="许师姐又来了",
                ),
            ],
        ))
        registry = identity_authority_registry(bible, [])
        entry = _entry(registry, "bible:许清")
        assert entry["source_labels"] == ["许清", "许师姐"]

    def test_alias_matching_canonical_name_is_not_duplicated(self) -> None:
        bible = _bible(Character(
            name="许清", role="重要配角", appearance_canonical="青衣长剑",
            aliases=[CharacterAlias(
                text="许清", name_kind="personal_name",
                evidence_chapter_index=1, evidence_quote="许清点头",
            )],
        ))
        registry = identity_authority_registry(bible, [])
        entry = _entry(registry, "bible:许清")
        assert entry["source_labels"] == ["许清"]

    def test_missing_aliases_attribute_does_not_crash(self) -> None:
        # 防御性核验：即便传入的对象没有 aliases 属性（如旧数据反序列化边缘
        # 情况），getattr 默认空列表必须让注册照常完成，不得抛异常。
        class LegacyCharacter:
            name = "老王"

        bible = Bible(
            world=World(visual_style_canonical="国风"),
            characters=[Character(name="许清", role="配角", appearance_canonical="青衣")],
        )
        # 直接在已校验过的 Bible 对象后追加一个非 Character 的旁路对象，
        # 模拟 getattr(character, "aliases", None) 命中缺失分支。
        bible.characters.append(LegacyCharacter())  # type: ignore[arg-type]
        registry = identity_authority_registry(bible, [])
        entry = _entry(registry, "bible:老王")
        assert entry["source_labels"] == ["老王"]
