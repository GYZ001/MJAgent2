"""人物谱契约：角色、别名、外观证据、阵营与人物关系（PRD 人物谱一致性）。

CharacterAlias/AppearanceEvidence/CharacterAffiliation/CharacterRelation 均为
"模型申报 + 代码核验后才允许落库"的证据锚点结构，彼此同构（见各自 docstring）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

class Relationship(BaseModel):
    to: str
    relation: str


class CharacterAlias(BaseModel):
    """一条别名证据：模型申报 + 代码核验后才允许落库（不确定不登记）。
    与 Scene.aliases（纯字符串列表）不同——人物别名判错代价更高（身份分裂/合并事故，
    见 docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §2.6/§2.7），所以每条都必须自带可机械
    核验的证据锚点，不能只是模型自称。"""

    text: str                      # 逐字称谓/别名字符串（如"许师姐""银色长袍女子""小胖子"）
    # personal_name/honorific/referential，取值与 app.portraits.IDENTITY_NAME_FORM_*
    # （app/portraits.py:124-126）一致——不新造平行词表，schemas.py 不反向导入 app.portraits
    # 避免循环引用，两处的字符串常量必须保持同步。
    name_kind: str
    evidence_chapter_index: int    # 证据锚点：原著章节序号（对应源章节的 idx 字段）
    # 证据锚点：逐字引句；必须能在该章节原文中作为子串命中，且该章节内需能找到角色
    # 规范名或该角色其它已确认别名（共现依据）——两者有一处不满足就不登记。
    evidence_quote: str
    # 能否作为全局身份凭证（折入 identity_authority_registry 的 source_labels，
    # 参与身份决议的排他判断），与"能否作为可检索的称呼线索"（app/production/
    # prep_pack.py 的 _prep_pack_bible_alias_owner 等通道，直接读本字段所在列表，
    # 不看 is_exclusive）是两件不同的事——别名本身永不因排他性判定失败被删除。
    # 默认 True 是刻意的：存量数据在迁移前行为与今天完全一致，不产生安全倒退，
    # 也不破坏现有直接构造 CharacterAlias(...) 的测试。真正决定这个值的是
    # app.stages._alias_verdict_call 新增的排他性裁决（见该函数），或若干
    # 共现免检通道（app/portraits/card_aliases.py、card_rebind.py 等，均显式
    # 写 False——它们从未为排他性做过任何核验，不该假装做过）。
    is_exclusive: bool = True


class AppearanceEvidence(BaseModel):
    """appearance_canonical 里"标志性特征"部分的证据锚点：模型申报 + 代码核验后才允许保留
    对应文字（不确定不登记，登记失败时该特征需要从 appearance_canonical 里退回通用形态，
    不是拒绝整条角色——结构与 CharacterAlias 完全同构，见 app/schemas.py 的 CharacterAlias）。
    王有材事故修复新增，见 logs/appearance_provenance_plan.md。"""

    evidence_chapter_index: int   # 原著章节序号（对应源章节的 idx 字段）
    # 原文逐字短句：必须原样连续照抄、不得跨句拼接或用省略号连接多处；核验规则见
    # app/stages._appearance_evidence_verified（长度上限 APPEARANCE_EVIDENCE_QUOTE_MAX_CHARS）。
    evidence_quote: str


class CharacterAffiliation(BaseModel):
    """一条阵营/宗门归属证据：模型申报 + 代码核验后才允许落库（不确定不登记）。
    与 CharacterAlias 的区别：这是状态事实（有效区间），不是恒真事实——需要
    valid_from_chapter/valid_to_chapter 支持"截至第 N 章"查询（见
    docs/CHARACTER_COGNITION_LAYER_DESIGN.md §3.2）。"""

    org: str                        # 归属对象逐字文本（宗门/阵营/势力名，如"血妖宗""靠山宗"）
    relation_kind: str              # 归属性质自由文本（如 membership/allegiance/hostility），
                                     # 不设 Literal 枚举——与 CharacterAlias.name_kind 同一
                                     # 宽松校验风格，避免模型申报值卡在硬枚举上被整条拒绝
    evidence_chapter_index: int     # 证据锚点：原著章节序号
    evidence_quote: str              # 证据锚点：逐字引句，核验规则与 CharacterAlias 完全一致
                                     # （逐字子串命中 + 角色本人在同段/同章共现）
    valid_from_chapter: int         # 有效区间起点（含）；未申报、或申报了但无法独立核验时
                                     # 代码回退为 evidence_chapter_index（见 valid_from_is_fallback）
    valid_to_chapter: int | None = None   # 有效区间终点（含）；None=尚无证据表明已失效（未申报、
                                     # 或申报了但无法独立核验时代码回退为 None，见 valid_to_is_fallback）
    # 回落标注（事故修复：状态事实回填 100% 拒绝一事排查出的相关修正，见
    # `app/stages._status_fact_interval_resolution` docstring 的完整说明）：核心事实
    # （角色 + 归属对象 + 证据章 + 引句）与区间边界是两件事——前者已经过
    # 候选判别裁决核验，后者只是模型对"从哪章起/到哪章止"的外推猜测。外推猜测若无法独立找到
    # 共现证据支撑，不应该拖累已核验的核心事实一起被拒绝（那是用未核验的部分否决已核验的部分），
    # 但也不能悄悄冒充"这就是模型申报并核验通过的边界"——所以回落发生时用这两个布尔位如实
    # 标注：True=当前 valid_from_chapter/valid_to_chapter 是代码回落的默认值（模型原申报的边界
    # 未被采信，不代表这就是模型的原始申报值）；False=模型未申报该边界（值恰好等于默认值），
    # 或模型申报的边界本身独立核验通过（值就是模型的原始申报值）。矛盾边界（如申报的终点早于
    # 证据章）不属于本标注范围——那种情况下整条事实都不会被登记。
    valid_from_is_fallback: bool = False
    valid_to_is_fallback: bool = False


class CharacterRelation(BaseModel):
    """一条对人关系证据（与既有 Relationship 不同：Relationship 是无证据锚点的静态叙事关系，
    供人物谱正文可读性使用；这是有证据锚点 + 有效区间的结构化状态事实，供判别式提问使用，
    两者并存、互不替代，与 aliases 新增时"不改写既有字段"的纪律一致）。"""

    to: str                         # 关系对象：人物谱规范名（必须是 bible.characters 中已有的名字）
    relation_kind: str              # 关系性质自由文本（如 ally/rival/hostile/master_disciple）
    evidence_chapter_index: int
    evidence_quote: str
    valid_from_chapter: int         # 语义与 CharacterAffiliation 同名字段完全一致，见其注释
    valid_to_chapter: int | None = None
    valid_from_is_fallback: bool = False
    valid_to_is_fallback: bool = False


class Character(BaseModel):
    name: str
    role: str
    appearance_canonical: str
    personality: str = ""
    speech_style: str = ""
    relationships: list[Relationship] = Field(default_factory=list)
    # 是否核验到本人在场，只记录事实，不决定出不出定妆图。人物谱有卡就应定妆。
    presence_status: Literal["onstage", "mentioned_only", "unresolved"] = "onstage"
    importance_score: float = 0.0
    importance_signals: list[str] = Field(default_factory=list)
    portrait_eligible: bool = True
    appearance_status: Literal["grounded", "insufficient_evidence", "deferred"] = "grounded"
    # 从原文与 world.era 判定出的年代服饰合同，作为定妆提示词的独立硬约束。
    period_costume_canonical: str = ""
    # 定妆照（圣经定稿后由 Seedream 生成，跨集一致性的视觉锚点；LLM 输出中不含以下字段）
    ref_image_path: str | None = None
    # 画像描述覆盖：人工编辑的定妆照生成词；为空时用 锚点串+画风 合成的默认描述（refs.portrait_prompt）
    portrait_prompt_override: str | None = None
    # 人物谱别名位（层一，docs/CHARACTER_IDENTITY_ENTITY_DESIGN.md §4.1）：全书范围内
    # 该角色在原文中出现过的其它称谓，逐条带证据锚点。旧 bible_json 没有这个键时，
    # default_factory 给空列表，反序列化不受影响。
    aliases: list[CharacterAlias] = Field(default_factory=list)
    # 认知层状态事实（docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.1）：带有效区间的
    # 阵营归属 / 对人关系，均为纯增量字段，旧 bible_json 没有这两个键时 default_factory
    # 给空列表，反序列化不受影响。可由名字与关系确定性推导的信息（姓氏、称谓惯例）不
    # 存储于此——存冗余早晚不一致，查询时现算。
    affiliations: list[CharacterAffiliation] = Field(default_factory=list)
    relations: list[CharacterRelation] = Field(default_factory=list)
    # 外观标志性特征的证据锚点（王有材事故修复新增，见
    # logs/appearance_provenance_plan.md）。只对 appearance_canonical 里"通用形态之外的
    # 标志性特征"部分需要；通用形态允许合理设定，不需要证据。旧 bible_json 没有这个键时
    # default_factory 给空列表，反序列化不受影响；空列表是诚实默认值（"这个角色没有可验证
    # 的标志性特征"），不是缺陷信号，不应触发任何拦截。
    source_evidence: list[AppearanceEvidence] = Field(default_factory=list)


def character_is_portrait_eligible(character: Character | dict) -> bool:
    """人物谱有卡且外观已落地就定妆。在不在场不参与这个判断。"""
    if isinstance(character, dict):
        name = character.get("name")
        eligible = character.get("portrait_eligible", True)
        status = character.get("appearance_status", "grounded")
    else:
        name = character.name
        eligible = character.portrait_eligible
        status = character.appearance_status
    return bool(str(name or "").strip()) and bool(eligible) and status == "grounded"
