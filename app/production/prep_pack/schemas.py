"""Model response schemas (structured-output Pydantic models) for episode
asset-mapping chunk extraction.

Split out of app/production/prep_pack.py.
"""
from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
)
from typing import Any


class _ModelCharacterMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    # 1.5.0 (kept in 2.0.0): model-declared prior-knowledge hypothesis (real
    # EP5 finding: outright banning this discarded a genuinely CORRECT guess
    # -- see _prep_pack_verify_true_name_hypothesis below). display_name
    # must still be the verbatim in-episode term of address; this field is
    # never used to replace it, only as an unverified candidate for _pass to
    # check.
    suspected_true_name: str | None
    # 2.0.0: this mention's own claim of which segments (global 1-based,
    # same numbering the model was shown in this chunk) it is actually
    # ON-SCREEN in -- not merely named/recalled/heard-of elsewhere. This
    # replaces the old event_id/source_span indirection: segment_indexes IS
    # now the segment-attribution claim (see _prep_pack_gate_segment_indexes
    # for the deterministic per-segment literal-evidence gate every
    # declared index must clear before being trusted).
    segment_indexes: list[int]


class _ModelSceneMention(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str
    suspected_true_name: str | None  # isomorphic to the character field above
    segment_indexes: list[int]
    # 2.0.2 (real regression fix, see PREP_PACK_VERSION's 2.0.2 note above):
    # a verbatim excerpt from one of this mention's own segment_indexes that
    # supports "this is that place" -- isomorphic to the old, now-removed
    # event_chain[].source_evidence[].quote, just declared at mention grain
    # instead of event grain. Required (not Optional) matching this module's
    # strict-schema convention; legal to be "" when this mention genuinely
    # has no excerptable evidence in this chunk (never fabricate one). This
    # is the sole reason the field exists: display_name/canonical scene
    # names are frequently model-synthesized labels that never appear
    # verbatim in the source text (real EP1: "大青山山顶" vs source "这青山
    # 顶端"), so they cannot themselves serve as independent local-text-
    # anchor evidence for resolution/discovery scene bindings -- see
    # _prep_pack_local_text_anchor's "同义反复" note and _pass()'s scene
    # anchor-candidate section below for how this flows into anchor_phrase.
    quote: str
    # 2026-09-30（真实案例，proj_ca86b15ab7d7 EP2「回民街巷子」）新增：quote
    # 要求一段连续原文，但 display_name 本身常常是综合/省略说法（"回民街
    # 巷子"），这次模型给的 quote 又恰好没能覆盖地点本身的字面称呼时，三路
    # 候选（canonical_scene_name/name/quote）全部不是原文连续字面，场景
    # 绑定的 anchor_phrase 缺失、整段被判未解析。source_wording 单独交出
    # "这个地点在 segment_indexes 所指原文里的称呼"本身——从原文逐字复制的
    # 一段连续文字，通常比 quote 短（例如原文写"两人走在回民街的青石板路
    # 上"就填"回民街"），不改字、不增字、不把原文不相邻的两处文字拼接在
    # 一起；没有可摘录的原文称呼就填空字符串，绝不编造。必填（非
    # Optional），跟 quote 同一 strict-schema 惯例。
    source_wording: str


class _ModelPropMention(BaseModel):
    """2.0.0, new: a physical object/item the episode actually shows on
    screen. No bible image library exists for props (unlike characters/
    scenes) -- this is a text-only asset, ``description`` is its only
    payload, never a portrait_id/scene_reference_id/visual_entity_id.

    2026-09-28: added ``plot_significant``/``plot_significant_quote`` (model
    nomination, code-verified downstream by app.props.judge.
    is_key_prop_mention) -- a foreshadowing/keepsake/hand-off prop can be a
    single terse mention that clears none of the existing structural gates
    (segment count >= 2, description clause count >= 3, source occurrence
    count >= 2); the real EP1 case this fixes is a heirloom felt through a
    character's sweater ("隔着毛衫也能摸出边缘的凸弧") that never gets a
    multi-clause description and is easy to under-count against the raw
    source text. Required (not Optional), matching _ModelSceneMention.quote's
    strict-schema convention: legal to be False/"" when this mention is
    genuinely not plot-significant, never fabricated to look important."""
    model_config = ConfigDict(extra="forbid")
    label: str
    description: str
    segment_indexes: list[int]
    plot_significant: bool
    plot_significant_quote: str
    # 2026-09-30（真实案例，proj_ca86b15ab7d7 EP2：原文「一支缠着细银丝的
    # 木簪」被 label 概括成「缠银丝木簪」、原文「那枚旧旧的木星星」被 label
    # 概括成「小木星星」）：label 允许是概括/规范化写法，但逐字核验因此找不
    # 到锚点，这类真实道具被静默丢弃。source_wording 单独交出"这件道具在
    # segment_indexes 所指原文里的称呼"本身——从原文逐字复制的一段连续
    # 文字（名词短语，例如上面两例分别填"缠着细银丝的木簪"/"木簪"、"木星
    # 星"），不改字、不增字、不把原文不相邻的两处文字拼接在一起；没有可
    # 摘录的原文称呼就填空字符串，绝不编造。必填（非 Optional），跟 quote/
    # plot_significant_quote 同一 strict-schema 惯例。
    source_wording: str
    # 2026-09-30（主会话复核后改，见 app.props.card_match 模块 docstring
    # 「模型提名、代码核验」一节）：纯字符串包含关系无法分辨"同一件物品的
    # 缩略说法"（木星星⊂小木星星）与"恰好包含该词的另一件东西"（水晶球⊂
    # 老式水晶球，博物馆展品，不是同一件），这需要语义判断，不能让代码自己
    # 猜。known_prop_name 让模型明确提名："这件道具如果就是已登记道具名单
    # 中的某一件（同一件实物，不只是同类或名字相近的东西），从名单里逐字
    # 复制那个名字；原文里这是另一件东西、或名单里没有它，填空字符串。"
    # 提名只是候选，不是免核验通道——下游仍要求 label/source_wording 至少
    # 一个真的逐字出现在这条提及自己的证据原文里才会采信（见 app.props.
    # card_match._resolve_nominated_card），提名的卡不存在或证据核验不过时
    # 退回常规判据，不静默信任模型的提名。必填（非 Optional），跟 quote/
    # source_wording 同一 strict-schema 惯例，合法值包括空字符串。
    known_prop_name: str


class _ChunkResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    characters: list[_ModelCharacterMention]
    scenes: list[_ModelSceneMention]
    props: list[_ModelPropMention]
    # 1.4.1 introduced a model-self-reported `paratext_segments` field here
    # (chapter title / author's note segments, own wording+temperature,
    # independent of app.source_paratext.PARATEXT_RULE). Retired 2026-08-27
    # (logs/paratext_single_source_plan.md): paratext is now a deterministic
    # projection of chapters.paratext_json (persisted per-chapter offsets,
    # same PARATEXT_RULE the world bible uses) onto this episode's segments
    # -- see _generate_prep_pack_once's paratext_regions/deterministic_
    # paratext_segments. The model is no longer asked to judge this at all;
    # asking it twice (once here, once for the world bible) with two
    # different wordings/temperatures for the same underlying question was
    # the exact duplication the retirement plan targeted.


def _response_format(model_type: type[BaseModel], name: str) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": model_type.model_json_schema(),
        },
    }


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

