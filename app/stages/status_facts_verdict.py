"""状态事实回填——归属/关系候选判别裁决与区间核验。"""
from __future__ import annotations

import hashlib
import json
from typing import Any


from app.harness import model_gateway
from app.source_excerpt import (
    index_source_segments,
)

from .alias_verdict import _ALIAS_VERDICT_NO_MATCH_LABEL, _AliasVerdictResponse
from .identity_evidence import _quote_comparison_variants


# ---------- A2. 状态事实回填（认知层，见 docs/CHARACTER_COGNITION_LAYER_DESIGN.md §4.1） ----------
#
# 状态事实（Character.affiliations 阵营归属 / Character.relations 对人关系）与层一别名的
# 核心区别：别名恒真（一次核验永久生效），状态事实带"有效区间"，需要"截至第 N 章"的区间
# 语义（设计文档 §3.2）。核验管线完全复用层一已经用三条真实事故验证过的机制——不重新
# 实现语义判断（禁止黑白名单式修复，任何具体人名/势力名/称谓都不得硬编码特判）：
# - 核心证据（申报角色与归属/关系对象是否真的在该章共现、引句是否逐字命中）复用
#   `_alias_declaration_verified`（判据模式不变：把"申报的别名文本"换成"归属组织名/
#   关系对象人名"，把"角色规范名或已确认别名"作为共现锚点，两者结构完全一致）；
# - 模型申报章节没通过共现闸时，复用 `_find_alias_bridge_chapter` 在全书范围内确定性
#   检索桥接章（不受 ALIAS_BACKFILL_SOURCE_BUDGET_CHARS 预算限制，见该函数 docstring）；
# - "同章共现≠指代同一人"的裁决闸同样成立：主角在一章出现几十次，跟任何词都共现，
#   共现只是必要条件——复用 `_alias_verdict_candidates`（该章出场的全部人物谱角色，
#   零语义结构扫描）与 `_alias_verdict_dossier`（卷宗覆盖全部候选证据，不止被测对象
#   周围，见该函数 docstring 对"孟兄/孟浩"真实回归的说明）、`_alias_verdict_pin_segment`
#   （段号钉证，不比对模型转录引句）。唯一新增的是 `_status_fact_verdict_call`：
#   `_alias_verdict_call` 的提示词是"称谓 X 指代候选中的谁"这句话术专用于别名场景
#   （term-to-person），归属/关系要问的是"这段证据里，与某势力/某人存在这层关系的其实
#   是候选中的谁"（fact-to-person）——语义不同不能共用同一句提示词，但候选判别机制
#   本身（枚举收紧候选/段号、"都不是/无法确定"选项、拒绝是非题式确认偏误）完全照搬，
#   不是另起炉灶。
#
# 额外新增的核验环节：有效区间起止章（valid_from_chapter/valid_to_chapter）本身也应该
# 有证据支撑，不能让模型随口给一个区间——见 `_status_fact_interval_resolution`。但区间
# 边界与核心事实（角色+归属/关系对象+证据章+引句，已过声明核验/桥接检索+候选判别裁决）
# 是两个独立核验的东西：边界外推没有独立支撑时只回落该边界本身（标注为回落值），不
# 拒绝已核验的核心事实；边界与核心证据矛盾（如终点早于证据章）才整条拒绝。
#
# 另一处事故修复（引句双锚定，见 `_status_fact_quote_dual_anchor_verified`）：首批真实
# 回填产出 6 条，人工复核发现 3 条的 evidence_quote 里没有主体——章级共现闸（判据 3）
# 按整章判断，被登记引用的却只是章内一句，章级通过不等于这一句里真锚定了主体，其中
# 一条（关系事实"王腾飞→韩宗"，引句是韩宗对孟浩说话）是彻头彻尾的假事实，另两条结论
# 为真但引句不合格——不可核验的正确答案与错误答案是同一等级的东西，一律拒绝。这道闸
# 加在核心证据核验之后、候选判别裁决之前，不替换、不削弱既有三闸与候选判别，独立生效。

_STATUS_FACT_VERDICT_STAGE_KEY = "character_status_fact_backfill_verdict"


async def _status_fact_verdict_call(
    *, fact_noun: str, claim_text: str, dossier: list[dict[str, Any]],
    candidates: list[str], project_id: str | None,
) -> _AliasVerdictResponse:
    """状态事实（归属/关系）候选判别裁决：与 `_alias_verdict_call` 同一范式（代码检索
    卷宗 → 模型在候选集中独立判别 → 段号钉证），复用其响应结构（`_AliasVerdictResponse`，
    字段本身零语义，候选/段号/引句三项对别名与状态事实同样适用，不需要另造一个响应类）、
    候选与卷宗来源（`_alias_verdict_candidates`/`_alias_verdict_dossier`，调用方负责传入）
    与钉证核验（`_alias_verdict_pin_segment`，调用方负责调用）——本函数只负责提问措辞与
    发起这一次独立模型调用，不重复实现候选判别的机制本身。

    `fact_noun` 是自然语言里的关系性质描述（"势力归属"/"人物关系"），`claim_text` 是被
    判别的归属对象（org）或关系对象（to）文本。返回值语义与 `_alias_verdict_call` 完全
    一致：`selected_candidate` 命中候选集之外（含"都不是/无法确定"）一律视为没有确认
    申报的假设，调用方据此拒绝登记。

    真实事故（proj_3ac0b627fa46 全量回填 22 条申报 0 条通过，误诊为"区间核验过严"，
    追查后发现区间核验从未被触及——全部卡在这一步）：提问措辞早先写成"这段证据所描述
    的{{fact_noun}}（对象：'{{claim_text}}'）实际说的是候选中的哪一位本人"，对关系事实
    （`fact_noun`="人物关系"）是道错题——`claim_text` 此时是 `to`（关系对象，本身就是
    候选集里一个现成的、无歧义的人名），模型据此老老实实回答"'{{claim_text}}'这个名字
    指的就是候选里的{{claim_text}}本人"（如 claim_text="韩宗" → selected_candidate="韩宗"，
    provider_calls 10692/10693/10695 等历史记录可查），但调用方 `_status_fact_evidence_
    resolution` 比对的是 `selected_candidate != subject_name`（subject_name 是关系的
    发起方，如"孟浩"，结构上恒不等于 `to`）——问的是"claim_text 这个词指代谁"，答案
    自然是 claim_text 自己，比对目标却是 subject_name，二者结构性错位，导致人物关系
    100% 必然 candidate_mismatch，与证据是否真实成立无关。现改为明确要求模型回答"谁
    拥有/构成这层{{fact_noun}}"（fact-to-person，与本模块顶部设计注释"归属/关系要问的
    是'与某势力/某人存在这层关系的其实是候选中的谁'"一致），并把 `claim_text` 从候选
    列表里剔除（调用方负责，见 `_status_fact_evidence_resolution`）——它结构上不可能是
    正确答案，留在候选里只会引诱模型选择那个"显而易见"但错误的选项。"""
    catalog = "\n\n".join(
        f"[第{item['chapter_idx']}章·段{item['segment_index']}] {item['text']}"
        for item in dossier
    )
    segment_indexes = [item["segment_index"] for item in dossier]
    candidate_options = [*candidates, _ALIAS_VERDICT_NO_MATCH_LABEL]
    candidate_list = "、".join(candidates)
    prompt = f"""下面是原著第 {dossier[0]['chapter_idx']} 章中与"{claim_text}"相关的原文段落
（含前后语境，出现顺序不代表任何推断结论），每段前面标了段号：
{catalog}

该章出场的人物谱角色候选（判别范围仅限这些人，不要引入候选之外的人）：
{candidate_list}

以上段落是"候选中某人与"{claim_text}"存在这层{fact_noun}"这一申报的证据来源。任务：仅
依据原文段落本身，判断真正与"{claim_text}"存在这层{fact_noun}的，实际上是候选中的哪一位
本人。
- selected_candidate 回答的是"拥有/构成这层{fact_noun}的那个人是谁"，不是"'{claim_text}'
  这个名字本身指代候选中的谁"——即使"{claim_text}"恰好也是一个现成的人名，也不能因为
  这一点就直接选它，必须依据原文证据确认候选中"与它存在这层{fact_noun}"的是谁；
- selected_candidate 必须从候选列表中选一个精确姓名，或者在证据不足以确定具体是谁时
  选"{_ALIAS_VERDICT_NO_MATCH_LABEL}"；不要因为某个候选在段落里出现次数多就倾向选他，
  只依据原文是否真的能确定这段{fact_noun}说的就是他本人；
- supporting_segment_index 必须填上面某一段落标注的段号（取值只能是 {segment_indexes}
  之一），选你得出这个结论最主要依据的那一段，不要凭空填一个没在目录里出现的段号；
- supporting_quote 可选，若填写请给该段里的一句原文摘录供人工复核参考，不要求逐字
  精确，留空也可以。
只输出符合 Schema 的 JSON。"""
    operation_id = _STATUS_FACT_VERDICT_STAGE_KEY + ":" + hashlib.sha256(
        json.dumps(
            {
                "fact_noun": fact_noun, "claim_text": claim_text, "candidates": candidates,
                "dossier": [
                    (item["chapter_idx"], item["segment_index"]) for item in dossier
                ],
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    schema = _AliasVerdictResponse.model_json_schema()
    schema["properties"]["supporting_segment_index"]["enum"] = segment_indexes
    schema["properties"]["selected_candidate"]["enum"] = candidate_options
    return await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_AliasVerdictResponse,
        validate=None,
        operation_id=operation_id,
        max_tokens=500,
        temperature=0.0,  # 与 _alias_verdict_call 同一理由：判别结论要稳定复现
        format_retry_limit=1,
        semantic_retry_limit=1,
        output_schema=schema,
        call_meta={
            "stage": "状态事实回填裁决",
            "stage_key": _STATUS_FACT_VERDICT_STAGE_KEY,
            "call_role": "stage_generate",
            "call_role_label": "状态事实裁决",
            "expected_json": True,
            "project_id": project_id,
            "fact_noun": fact_noun,
            "claim_text": claim_text,
            "candidates": candidates,
        },
    )


def _status_fact_interval_resolution(
    chapters_by_idx: dict[int, str],
    anchor_texts: set[str],
    object_anchor_texts: set[str],
    resolved_chapter_index: int,
    declared_valid_from_chapter: int | None,
    declared_valid_to_chapter: int | None,
) -> tuple[int, bool, int | None, bool] | None:
    """有效区间起止章核验：不能让模型随口给一个区间（不确定不登记，安全默认同核心
    证据），但"区间边界没独立证据"与"核心事实没证据"是两件不同的事，不能绑在一起
    处理——见下方"拆分处置"说明（事故修复：状态事实回填 100% 拒绝）。

    - 起点/终点若未申报（None）：起点回退为核心证据所在的 `resolved_chapter_index`
      （它已经过声明核验或桥接检索，本身就是有证据支撑的章节）；终点回退为 None，
      代表"尚无证据表明已失效"，与 `character_portraits` 表 `ep_end IS NULL` 的既有
      查询惯例同构（设计文档 §3.2）。
    - 起点/终点若申报了与 `resolved_chapter_index` 不同的章节：该章节必须独立通过
      与核心证据完全同一条闸——`_status_fact_boundary_dual_anchor_verified`（复用
      `_status_fact_quote_dual_anchor_verified` 这条双锚定原语，按自然段而非整章
      判断）。

      缺陷修复：此前这里仍是"claim_text 与 anchor_texts 之一同时出现在章节任意
      位置"的章级共现判据，与十几行外的 `_status_fact_quote_dual_anchor_verified`
      自相矛盾——那条闸就是专门为了堵住章级共现"对主角近乎零过滤力"这一漏洞才加的
      （见其 docstring 引用的真实事故：王腾飞在第27章章级共现 5 次、共现闸判定
      通过，但被登记的引句其实是韩宗对孟浩说话，那句话里根本没有王腾飞）。边界
      判定如果继续用章级共现，同一个漏洞原样留在这里：主体若是主角（几乎每章
      无处不在），边界章又恰好在别处提到了归属对象/关系对象，`valid_from_is_
      fallback=False`（含义是"该边界经过独立核验"）就会被错误地记成 False——而
      该章其实从未在同一段文字里把这条边界与主体真正连接起来。现在边界判定与
      核心证据共用同一条双锚定原语，不再有这个不一致。
    - 边界与核心证据点矛盾（起点晚于 `resolved_chapter_index`、终点早于它）：这是
      自相矛盾——核心证据本身已经证明该事实在 `resolved_chapter_index` 这一章成立，
      不是"外推不足"，返回 None 交由调用方整体拒绝（§4 反例，不可放松）。

    拆分处置（不是放宽标准）：申报的边界与核心证据不矛盾、但也找不到独立双锚定
    支撑（既不等于 `resolved_chapter_index`，边界章又没有任何一段同时锚定主体
    与对象）——这种"未核验的外推"只丢弃该边界本身，不牵连已经过声明核验/候选
    判别的核心事实：对应边界回落为默认值（起点回落为 `resolved_chapter_index`、
    终点回落为 None），并把返回的第 2/4 位标记为 True，供调用方在
    `CharacterAffiliation.valid_from_is_fallback`/`valid_to_is_fallback` 如实标注——
    这是代码回落的默认值，不代表这就是模型申报并核验通过的原始边界。旧实现把这种
    "外推不成立"与"核心矛盾"一视同仁地整条拒绝，等于让未核验的边界外推否决了已核验
    的核心事实，用错了地方（真实项目 proj_3ac0b627fa46 dry-run 复现：22 条申报里这条
    规则从未被实际触发过——回填 100% 拒绝的真正原因在候选判别裁决环节，见
    `_status_fact_verdict_call` docstring；但这条规则本身仍是一处真实的过严设计，
    一旦上游问题修复、更多事实进入这一步，就会开始误杀，因此一并修正）。

    `object_anchor_texts` 与调用方 `_status_fact_evidence_resolution` 核验核心
    证据引句双锚定时用的是同一个集合（归属对象/关系对象的规范名∪已确认别名），
    不是把 `claim_text` 原样传入——边界章里对象出现的具体措辞未必与核心证据章
    完全一致（例如对象本身是某个已确认别名的角色，见该函数关于 `object_anchor_
    texts` 构造方式的说明），双锚定既然要求"同一条原语"，就应当连锚点集合本身
    也保持一致，不能只搬运判据形状、锚点集合却各用各的。
    """
    valid_from_chapter = resolved_chapter_index
    valid_from_is_fallback = False
    if declared_valid_from_chapter is not None and declared_valid_from_chapter != resolved_chapter_index:
        if declared_valid_from_chapter > resolved_chapter_index:
            return None  # 矛盾：起点不能晚于核心证据章，整条拒绝
        boundary_text = chapters_by_idx.get(declared_valid_from_chapter, "")
        if boundary_text and _status_fact_boundary_dual_anchor_verified(
            boundary_text, anchor_texts, object_anchor_texts,
        ):
            valid_from_chapter = declared_valid_from_chapter
        else:
            valid_from_is_fallback = True  # 外推无独立支撑：不采信，回落为核心证据章

    valid_to_chapter: int | None = None
    valid_to_is_fallback = False
    if declared_valid_to_chapter is not None:
        if declared_valid_to_chapter == resolved_chapter_index:
            valid_to_chapter = resolved_chapter_index
        elif declared_valid_to_chapter < resolved_chapter_index:
            return None  # 矛盾：终点不能早于核心证据章，整条拒绝
        else:
            boundary_text = chapters_by_idx.get(declared_valid_to_chapter, "")
            if boundary_text and _status_fact_boundary_dual_anchor_verified(
                boundary_text, anchor_texts, object_anchor_texts,
            ):
                valid_to_chapter = declared_valid_to_chapter
            else:
                valid_to_is_fallback = True  # 外推无独立支撑：不采信，回落为开放终点

    return valid_from_chapter, valid_from_is_fallback, valid_to_chapter, valid_to_is_fallback


def _status_fact_quote_dual_anchor_verified(
    quote: str,
    subject_anchor_texts: set[str],
    object_anchor_texts: set[str],
) -> bool:
    """状态事实引句双锚定核验（事故修复：真实人物谱回填出现的假事实——"王腾飞 同党/
    同门→韩宗"，引用的是"韩宗看都不看其他人一眼，望着孟浩，冷淡开口"这句，句中根本
    没有王腾飞，是韩宗对孟浩说话，与王腾飞无关；另两条"孟浩→靠山宗""许清→靠山宗"引句
    也分别缺主体、只剩三个字的组织名，同一漏洞的三种呈现）。

    根因：`_alias_declaration_verified`/`_find_alias_bridge_chapter` 的"共现"判据是
    按章节整体判断的（claim_text 与 anchor_texts 之一同时出现在该章原文任意位置即算
    通过），但被实际登记、供人工复核的 evidence_quote 只是章节内的一句/一段——章级
    共现通过不代表这一句里真的锚定了主体。没有主体锚点就无法区分"真但证据差"与"假"，
    两者外观完全一致（都是"claim_text 在引句里，主体不在"），所以一律拒绝，不区分
    对待——不可核验的正确答案与错误答案是同一等级的东西。

    条件（归属/关系两类结构相同，调用方按语义传入对应的 subject/object 锚点集合）：
    引句必须同时包含 subject_anchor_texts（主体角色的规范名或已确认别名）中至少一项，
    与 object_anchor_texts（归属对象 org 本身；或关系对象 to 的规范名/已确认别名）中
    至少一项——且必须在同一种引号候选形式（`_quote_comparison_variants`，处理全角/
    半角引号导致的假阴性）下同时命中，不能分别用不同形式各自命中一侧再拼凑。任一侧
    缺失整条拒绝，不尝试放宽或"修补"引句去凑双锚定（不确定不登记，安全默认）。

    这道闸加在核心证据核验（声明核验/桥接检索）之后、候选判别裁决之前——不满足直接
    拒绝，省一次候选判别模型调用；不替换、不削弱既有三闸（章级共现、逐字引句在原文、
    候选判别）与后续段号钉证，只是额外补上"引句本身双锚定"这一层，四闸独立生效。
    """
    quote = (quote or "").strip()
    if not quote:
        return False
    subject_forms = [s for s in subject_anchor_texts if s]
    object_forms = [o for o in object_anchor_texts if o]
    if not subject_forms or not object_forms:
        return False
    for candidate in _quote_comparison_variants(quote):
        if (
            any(s in candidate for s in subject_forms)
            and any(o in candidate for o in object_forms)
        ):
            return True
    return False


def _status_fact_boundary_dual_anchor_verified(
    chapter_text: str,
    subject_anchor_texts: set[str],
    object_anchor_texts: set[str],
) -> bool:
    """区间边界章的双锚定核验（缺陷修复，见 `_status_fact_interval_resolution`
    docstring"边界章级共现"一节）：与核心证据共用同一条原语
    `_status_fact_quote_dual_anchor_verified`，只是这里没有一句现成的
    evidence_quote 可以直接判断，需要先在边界章内部确定性检索出候选"quote"。

    做法：把边界章按 `index_source_segments`（与 `_alias_verdict_dossier` 同一
    分段工具、同一默认粒度，不另起一套分段规则）切成自然段，只要存在至少一段
    本身就同时双锚定通过（引号候选形式下同一形式内同时含主体锚点与对象锚点
    各至少一项），就认为这条边界有独立支撑，返回 True。

    这不是把"整章共现"换成"整章双锚定"（那仍然不够——主体在第一段、对象在
    最后一段，整章拼起来一样能双双命中，跟章级共现是同一个漏洞的另一种写法）：
    双锚定必须发生在同一自然段内，与核心证据的 evidence_quote 是"原文里的
    一句/一段"这一颗粒度完全对齐，不接受跨段拼凑。全书任何一段都不满足时
    返回 False，交由调用方按"拆分处置"回落为 fallback，不牵连核心事实。"""
    for segment in index_source_segments(chapter_text):
        if _status_fact_quote_dual_anchor_verified(
            segment.text, subject_anchor_texts, object_anchor_texts,
        ):
            return True
    return False
