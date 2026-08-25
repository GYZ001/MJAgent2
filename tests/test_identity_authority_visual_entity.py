"""红→绿测试：``app.identity_authority.visual_entity_id_for_resolution``。

覆盖 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.2/§8 的可机械判定判据：
1. 同一 (source_label, scope_qualifier) 在不同集/不同批派生出同一实体 ID。
2. 派生输入不含任何随集/随批变化的量（改变 episode_no、
   identity_scope_fingerprint、identity_group、evidence_ref 均不改变结果）。
3. 已具名分支与 authority_id_for_resolution 的具名分支同格式（`bible:{name}`）。
4. 不同 (source_label, scope_qualifier) 派生不同实体 ID（scope_qualifier 用于
   区分"同一称谓指不同人"）。
5. 命名权威（authority_id_for_resolution）行为逐字不变，不受本次改动影响。
"""
from __future__ import annotations

from app.identity_authority import (
    authority_id_for_resolution,
    visual_entity_id_for_resolution,
)


def _ep1_functional(**overrides) -> dict:
    base = {
        "source_label": "小胖子",
        "canonical_name": "小胖子",
        "resolution": "functional_identity",
        "identity_group": "current-1:F1",
        "identity_scope_fingerprint": "source-sha-episode-1",
        "scope_qualifier": "",
    }
    base.update(overrides)
    return base


def _ep10_functional(**overrides) -> dict:
    # 同一个称谓「小胖子」，在完全不同的一集：不同的 identity_group token、
    # 不同的 identity_scope_fingerprint（模拟"换一次模型调用=换一集"）。
    base = {
        "source_label": "小胖子",
        "canonical_name": "小胖子",
        "resolution": "functional_identity",
        "identity_group": "current-10:F7",
        "identity_scope_fingerprint": "source-sha-episode-10-different-evidence-set",
        "scope_qualifier": "",
    }
    base.update(overrides)
    return base


class TestCrossEpisodeStability:
    def test_same_source_label_and_qualifier_stable_across_episodes(self) -> None:
        ep1 = visual_entity_id_for_resolution(_ep1_functional())
        ep10 = visual_entity_id_for_resolution(_ep10_functional())
        assert ep1 == ep10
        assert ep1.startswith("entity:")

    def test_xu_qing_style_progression_stable_until_named(self) -> None:
        # 许清案例（设计文档 §1）：EP1 无图群演「银色长袍女子」、EP5「许姓女子」、
        # EP6「许师姐」——三种不同措辞但 scope_qualifier 相同（均未消歧出第二个
        # 同称谓的人），只要 source_label 相同就必须落到同一个实体。
        # 这里用同一措辞跨集出现来验证"稳定"这一半；不同措辞属于层一别名
        # 折叠范畴（P0 第 2/3 项，不在本函数职责内，见设计文档 §4.1）。
        ep1 = visual_entity_id_for_resolution({
            "source_label": "许师姐",
            "canonical_name": "许师姐",
            "resolution": "functional_identity",
            "identity_group": "current-1:F3",
            "identity_scope_fingerprint": "ep1-fingerprint",
            "scope_qualifier": "",
        })
        ep6 = visual_entity_id_for_resolution({
            "source_label": "许师姐",
            "canonical_name": "许师姐",
            "resolution": "functional_identity",
            "identity_group": "current-6:F9",
            "identity_scope_fingerprint": "ep6-fingerprint-totally-different",
            "scope_qualifier": "",
        })
        assert ep1 == ep6


class TestDerivationExcludesPerEpisodeSignals:
    """派生输入不含任何随集变化的量：单独改变每一个候选"泄露源"，结果不变。"""

    def test_changing_identity_scope_fingerprint_alone_does_not_change_id(self) -> None:
        a = visual_entity_id_for_resolution(_ep1_functional(
            identity_scope_fingerprint="fingerprint-A",
        ))
        b = visual_entity_id_for_resolution(_ep1_functional(
            identity_scope_fingerprint="fingerprint-B-completely-different",
        ))
        assert a == b

    def test_changing_identity_group_alone_does_not_change_id(self) -> None:
        a = visual_entity_id_for_resolution(_ep1_functional(
            identity_group="current-1:F1",
        ))
        b = visual_entity_id_for_resolution(_ep1_functional(
            identity_group="current-99:F42",
        ))
        assert a == b

    def test_changing_episode_or_evidence_markers_alone_does_not_change_id(self) -> None:
        a = visual_entity_id_for_resolution(_ep1_functional(
            episode_no=1,
            evidence_ref="E007",
            source_segment_id="seg-1",
        ))
        b = visual_entity_id_for_resolution(_ep1_functional(
            episode_no=10,
            evidence_ref="E034",
            source_segment_id="seg-999",
        ))
        assert a == b


class TestNamedBranchSharesAuthorityFormat:
    def test_named_branch_matches_authority_id_named_format(self) -> None:
        value = {
            "source_label": "青衣人",
            "canonical_name": "丁力",
            "resolution": "future_identity",
            "identity_group": "episode:visitor",
        }
        assert visual_entity_id_for_resolution(value) == "bible:丁力"
        assert authority_id_for_resolution(value) == "bible:丁力"

    def test_reference_identity_branch_also_named(self) -> None:
        value = {
            "source_label": "王平",
            "canonical_name": "王平",
            "resolution": "reference_identity",
        }
        assert visual_entity_id_for_resolution(value) == "bible:王平"


class TestScopeQualifierDisambiguates:
    def test_different_scope_qualifier_yields_different_entity(self) -> None:
        # 同一批出现两个"师兄"，模型按规则 8 各自申报 scope_qualifier 区分。
        elder = visual_entity_id_for_resolution(_ep1_functional(
            scope_qualifier="年长的那位",
        ))
        younger = visual_entity_id_for_resolution(_ep1_functional(
            scope_qualifier="年轻的那位",
        ))
        assert elder != younger

    def test_empty_scope_qualifier_is_stable_default(self) -> None:
        a = visual_entity_id_for_resolution(_ep1_functional(scope_qualifier=""))
        b = visual_entity_id_for_resolution(_ep1_functional())
        assert a == b

    def test_different_source_label_yields_different_entity(self) -> None:
        li_fugui = visual_entity_id_for_resolution(_ep1_functional(
            source_label="小胖子", canonical_name="小胖子",
        ))
        xu_qing = visual_entity_id_for_resolution(_ep1_functional(
            source_label="许师姐", canonical_name="许师姐",
        ))
        assert li_fugui != xu_qing


class TestLabelNormalizationIsPureStringFolding:
    """归一化只折叠同一字符串的等价写法，不引入任何上下文依赖。"""

    def test_surrounding_whitespace_folds_to_same_entity(self) -> None:
        a = visual_entity_id_for_resolution(_ep1_functional(
            source_label="小胖子",
        ))
        b = visual_entity_id_for_resolution(_ep1_functional(
            source_label="  小胖子  ",
        ))
        assert a == b

    def test_repeated_internal_whitespace_collapses(self) -> None:
        a = visual_entity_id_for_resolution(_ep1_functional(
            source_label="三号 弟子",
        ))
        b = visual_entity_id_for_resolution(_ep1_functional(
            source_label="三号   弟子",
        ))
        assert a == b

    def test_fullwidth_digit_folds_to_halfwidth(self) -> None:
        # NFKC 规整：全角数字/兼容字符与半角写法折叠为同一称谓。
        a = visual_entity_id_for_resolution(_ep1_functional(
            source_label="3号弟子",
        ))
        b = visual_entity_id_for_resolution(_ep1_functional(
            source_label="３号弟子",  # full-width '3'
        ))
        assert a == b


class TestAuthorityIdForResolutionUnaffected:
    """命名权威函数逐字不变（设计文档 §8 判据 7），本次改动不得触碰其行为。"""

    def test_functional_authority_id_still_varies_with_scope_fingerprint(self) -> None:
        # authority_id_for_resolution 的既有（有意为之的）不稳定行为必须保留：
        # 这是它跟 visual_entity_id_for_resolution 的核心区别，不是缺陷。
        ep1 = authority_id_for_resolution(_ep1_functional())
        ep10 = authority_id_for_resolution(_ep10_functional())
        assert ep1 != ep10
        assert ep1.startswith("functional:")
        assert ep10.startswith("functional:")

    def test_named_authority_id_unchanged(self) -> None:
        value = {
            "source_label": "青衣人",
            "canonical_name": "丁力",
            "resolution": "future_identity",
        }
        assert authority_id_for_resolution(value) == "bible:丁力"
