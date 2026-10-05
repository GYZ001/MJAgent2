"""存量分镜「最小修改重写」（P0，2026-10-05，《顾念长安》第 1 集真实回归驱动，
完整背景见 ``app.domain.storyboard_ops.prop_continuity_review`` 模块 docstring）。

## 为什么不能用整段重生成修存量分镜

``app.production.storyboard_identity_regenerate.regenerate_identity_candidate``
对目标段是从头重新生成（``_generate_all_segment_prompts`` 只认 ``revision_notes``
这一条修改意见，不认「保留其余文字」），而第 1 集很多段的正文是用户逐帧审片后
用「修订本段」带修改意见人工调过的（例如围巾改走向、插头两孔空着、反打背景改成
餐厅）——那些调整没有持久化成任何结构化字段，从头重写会把它们连同本来就对的
部分一起冲掉。存量复核发现的违规通常只是一两句话的局部问题，不需要也不应该
重写整段：本模块把「发一次最小替换提案 + 代码核验区间 + 按区间应用」做成一套
独立于整段重生成的保存路径，``prop_continuity_review`` 只在这条路径核验失败
（没有一条替换能安全套用）时才把该段原样标记为「需要人工修订」，从不自动回落
到整段重生成。

## 哪些类别适合局部替换，哪些需要人工修订本段

``_LOCALLY_PATCHABLE_KINDS`` 对 ``storyboard_prose_review._KIND_RULES`` 的
11 类逐一给出判断（见各条内联理由）；判据是「这一类违规的 fix 建议是否只需要
替换/补写一两句话，还是需要新增或拆分镜头、调整台词占位符位置这类结构性改动」
——后者局部替换无法安全完成，存量模式下不自动改，只在复核结果里原样保留供人工
走单段「修订本段」处理。模块加载时 assert 两份取值集合完全相等
（``test_storyboard_prop_continuity_minimal_patch.py`` 同步守着），防止
``storyboard_prose_review`` 新增第十二类时这里漏判，默认把漏判的类别当「不可
局部改」（更安全的一侧，不是更宽松的一侧）。

## 代码核验：quote 必须逐字、唯一、与已核验违规重叠、互不重叠

模型提名的每条替换（``quote``/``replacement``）要能应用，必须同时满足：
① ``quote`` 在当前 ``prompt_text`` 里逐字出现且只出现一次（``str.count``，
不用 ``textmatch.condense`` 模糊匹配——替换要精确定位字符区间，不能用会丢失
位置信息的归一化匹配）；② ``quote`` 的字符区间与某条已核验违规自己的 ``quote``
区间重叠（``_violation_anchor_spans`` 负责在当前正文里定位违规锚点，定位不到
的违规——两次独立模型调用之间偶发的标点差异——不参与重叠判断，不放宽核验）；
③ 与本次已接受的其它替换互不重叠（按模型给出的顺序贪心接受，后到的与先接受的
重叠就拒绝，可见记录原因，不做无依据的优先级裁决）；④ ``replacement`` 非空。
全部满足才接受；不满足的单独拒绝，记在结果里（``RejectedReplacement``），不
拖累同段其它替换，也不静默。接受的替换按区间从后往前应用到原字符串切片上——
区间外的文本就是原字符串未被触及的那部分，逐字不变由切片构造本身保证，不是
事后校验出来的。

## 为什么要镜像应用到 speech_template

``identity_workspace.prepare_identity_candidate`` 末尾会用
``result.get("speech_template") or result.get("prompt_text")`` 重新渲染
``prompt_text``（``storyboard_speech_render.render_segment_speech`` 同一套
逻辑）——段落有台词占位符时，真正的可编辑源文本是 ``speech_template``（带
``{{speech:Uxx}}`` 占位符），``prompt_text`` 只是它展开后的派生结果。只改
``prompt_text``、不镜像改 ``speech_template`` 的话，保存那一刻会被原模板
悄悄渲染回去，替换形同没发生——``build_patch_candidate`` 对非空
``speech_template`` 要求同一组替换也能在它里面逐字定位且互不重叠，定位不到
就整体返回 ``None``（这段不能安全套用这次局部修改），不静默丢替换。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app import textmatch
from app.harness import model_gateway
from app.production.storyboard_prose_review import ProseViolation, _KIND_RULES

#: 11 类判据逐一判断能否用「替换一两句话」解决，理由见模块 docstring。
#: True＝可局部替换修复；False＝需要调整镜头结构/台词占位符位置，存量模式下
#: 不自动改，只报告需要人工修订本段。
_LOCALLY_PATCHABLE_KINDS: dict[str, bool] = {
    # 需要拆分/合并/重排这一镜里的动作顺序才能降密度，不是换一句话能解决的。
    "action_density": False,
    # fix 的两种写法都是把「脸上颜色」那一句话换成别的写法——纯文本替换。
    "skin_blush": True,
    # 一种 fix 分支（换成不说话的动作）是纯文本替换，但另一种分支要求新增台词
    # 占位符——涉及台词合同与说话人占位符的落位，局部替换不足以保证正确，
    # 按更安全的一侧判不可局部改。
    "unvoiced_speech": False,
    # fix 是改回站位或补写一句换位动作，纯文本替换。
    "screen_side": True,
    # fix 是补一句拿取/放置动作或改成一致初始状态，纯文本替换。
    "prop_appearance": True,
    # fix 是删掉重复描述那句话（可换成不重复的更短文字），纯文本替换。
    "prop_duplication": True,
    # fix 是删去重演转换过程的那句描述，改成直接从完成后姿态起幅，纯文本替换。
    "repeated_transition_action": True,
    # fix 要求拆成两个镜头、之间硬切——改变镜头数量/编号，结构性改动。
    "time_jump": False,
    # 同 time_jump：fix 要求拆成两个镜头，结构性改动。
    "impossible_camera_move": False,
    # fix 是把否定句换成对应的正面写法，纯文本替换。
    "negated_action": True,
    # fix 是把状态描述改回一致状态（必要时补写变回动作），纯文本替换。
    "prop_state_regression": True,
}

assert set(_LOCALLY_PATCHABLE_KINDS) == set(_KIND_RULES), (
    "_LOCALLY_PATCHABLE_KINDS 必须覆盖 storyboard_prose_review._KIND_RULES 的全部"
    "取值且不多不少——新增判据类别必须显式判断能否局部替换，见模块 docstring"
)


def is_locally_patchable(kind: str) -> bool:
    """未知 kind（理论上不会出现，``_verified_violations`` 已经把 kind 限制在
    ``_KIND_RULES`` 内）按不可局部改处理——更安全的一侧，不是更宽松的一侧。"""
    return _LOCALLY_PATCHABLE_KINDS.get(kind, False)


class _PatchReplacement(BaseModel):
    quote: str = ""
    replacement: str = ""


class _PatchResponse(BaseModel):
    replacements: list[_PatchReplacement] = Field(default_factory=list)


@dataclass
class AcceptedReplacement:
    quote: str
    replacement: str
    start: int
    end: int


@dataclass
class RejectedReplacement:
    quote: str
    replacement: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"quote": self.quote, "replacement": self.replacement, "reason": self.reason}


def _literal_span(text: str, quote: str) -> tuple[int, int] | None:
    """``quote`` 在 ``text`` 里逐字出现且只出现一次时返回字符区间，否则 None——
    与 ``storyboard_prose_review._verbatim_in`` 的 ``textmatch.condense`` 模糊
    匹配不是同一个判据：这里要精确字符位置去做替换，模糊匹配会丢失位置信息。"""
    if not quote or text.count(quote) != 1:
        return None
    start = text.find(quote)
    return start, start + len(quote)


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _violation_anchor_spans(prompt_text: str, violations: list[ProseViolation]) -> list[tuple[int, int]]:
    """已核验违规自己的 ``quote`` 在当前正文里的字符区间，供替换项核验「是否与
    某条违规重叠」；定位不到的违规（两次独立模型调用之间偶发的标点差异，或
    违规本身在正文里不止出现一次）不参与重叠判断——不是放宽核验，是这条锚点
    此刻在正文里没有唯一确切位置可用。"""
    spans = []
    for v in violations:
        span = _literal_span(prompt_text, v.quote)
        if span is not None:
            spans.append(span)
    return spans


def validate_replacements(
    prompt_text: str, raw: list[_PatchReplacement], *, violations: list[ProseViolation],
) -> tuple[list[AcceptedReplacement], list[RejectedReplacement]]:
    """模型提名、代码核验——核验规则见模块 docstring。按模型给出的顺序贪心
    接受，与已接受区间重叠的后到项目被拒绝（可见记录原因），不做无依据的
    优先级裁决。"""
    anchors = _violation_anchor_spans(prompt_text, violations)
    accepted: list[AcceptedReplacement] = []
    rejected: list[RejectedReplacement] = []
    for item in raw:
        if not item.replacement.strip():
            rejected.append(RejectedReplacement(item.quote, item.replacement, "replacement 为空"))
            continue
        span = _literal_span(prompt_text, item.quote)
        if span is None:
            rejected.append(RejectedReplacement(item.quote, item.replacement, "quote 在正文里找不到或不止出现一次"))
            continue
        if not any(_overlaps(span, a) for a in anchors):
            rejected.append(RejectedReplacement(item.quote, item.replacement, "quote 与已核验违规的原文区间不重叠"))
            continue
        if any(_overlaps(span, (a.start, a.end)) for a in accepted):
            rejected.append(RejectedReplacement(item.quote, item.replacement, "与另一条已接受的替换区间重叠"))
            continue
        accepted.append(AcceptedReplacement(item.quote, item.replacement, span[0], span[1]))
    return accepted, rejected


def apply_replacements(text: str, accepted: list[AcceptedReplacement]) -> str:
    """按区间从后往前替换；区间外的文本是原字符串未被触及的切片，逐字不变由
    构造本身保证——调用方必须保证 ``accepted`` 互不重叠（``validate_replacements``
    已核验）。"""
    result = text
    for item in sorted(accepted, key=lambda r: r.start, reverse=True):
        result = result[: item.start] + item.replacement + result[item.end :]
    return result


def build_patch_candidate(original: dict[str, Any], accepted: list[AcceptedReplacement]) -> dict[str, Any] | None:
    """构造交给 ``identity_workspace.save_identity_candidate`` 的候选：只带
    ``prompt_text``（与存在时镜像改过的 ``speech_template``）两个键，其余键不
    出现在候选里——``prepare_identity_candidate`` 对不在候选里的键原样沿用
    ``original`` 的值，天然满足「只改了 prompt_text、其余字段不变」，不需要
    手工把 original 的其它字段也抄进候选。``speech_template`` 非空时必须镜像
    应用同一组替换，理由见模块 docstring；镜像失败（quote 在 speech_template
    里定位不到或镜像后互相重叠）返回 ``None``，表示这段不能安全套用。"""
    patched_prompt_text = apply_replacements(str(original.get("prompt_text") or ""), accepted)
    candidate: dict[str, Any] = {"prompt_text": patched_prompt_text}
    template = str(original.get("speech_template") or "")
    if not template:
        return candidate
    mirrored: list[AcceptedReplacement] = []
    for item in accepted:
        span = _literal_span(template, item.quote)
        if span is None:
            return None
        mirrored.append(AcceptedReplacement(item.quote, item.replacement, span[0], span[1]))
    ordered = sorted(mirrored, key=lambda r: r.start)
    if any(_overlaps((a.start, a.end), (b.start, b.end)) for a, b in zip(ordered, ordered[1:])):
        return None
    candidate["speech_template"] = apply_replacements(template, mirrored)
    return candidate


def _continuity_memo_text_fields(memo: dict[str, Any]) -> list[tuple[str, str]]:
    """扫描 ``app.production.storyboard_continuity_memo._AiContinuityMemo`` 的
    全部自由文本字段——``layout``/``travel_direction``/两条 source_quote 都是
    本段结束时状态的文字记录，``screen_side`` 的 fix 直接改站位描述，若替换后
    ``travel_direction`` 仍引用改之前的站位原文就是同一类矛盾，不能只扫
    ``layout``（CLAUDE.md「空集合不等于无需检查」同理：少扫的字段不等于那里
    不会矛盾）。"""
    fields: list[tuple[str, str]] = [
        ("layout", str(memo.get("layout") or "")),
        ("travel_direction", str(memo.get("travel_direction") or "")),
        ("time_of_day_source_quote", str(memo.get("time_of_day_source_quote") or "")),
        ("layout_change_source_quote", str(memo.get("layout_change_source_quote") or "")),
    ]
    for prop in memo.get("props") or []:
        name = str(prop.get("name") or "")
        for key in ("location", "state", "form"):
            fields.append((f"props[{name}].{key}", str(prop.get(key) or "")))
    for character in memo.get("characters") or []:
        identity_id = str(character.get("identity_id") or "")
        for key in ("location", "wardrobe", "emotion"):
            fields.append((f"characters[{identity_id}].{key}", str(character.get(key) or "")))
    return fields


def continuity_memo_conflicts(continuity_memo: dict[str, Any], accepted: list[AcceptedReplacement]) -> list[str]:
    """已核验违规的 ``quote``（改之前被判定为问题的那句原文）如果逐字包含在本段
    自己 ``continuity_memo`` 的任一字段文本里，说明这份备忘是照着改之前的正文
    记录的，局部替换后正文已经不是那个状态，但备忘没有跟着改——只报告，不自动
    改写备忘（``app.production.storyboard_continuity_memo`` 的跨段权威语义很
    精细，本模块不代替它判断该怎么改），调用方把结果写进可见的复核结果。判据
    是字符串包含关系（``textmatch.condense`` 容忍标点/空白差异），从这次实际
    发生的替换数据推导，不是对字段名或关键词的穷举猜测。"""
    conflicts: list[str] = []
    for item in accepted:
        condensed_quote = textmatch.condense(item.quote)
        if not condensed_quote:
            continue
        for label, text in _continuity_memo_text_fields(continuity_memo):
            if text and condensed_quote in textmatch.condense(text):
                conflicts.append(f"continuity_memo.{label} 仍记录着改之前的原文「{item.quote[:40]}」，与改后正文矛盾，需人工核对")
    return conflicts


#: 替换提案调用只需要一份不长的清单，不是整段 prompt_text。
_PATCH_ANSWER_TOKENS = 1200

_PATCH_SYSTEM_PROMPT = (
    "你是短剧分镜正文的最小修改员。下面给你这一段当前的分镜正文（prompt_text，逐字）与"
    "已核验的违规清单；只需要针对清单里列出的问题，各给出一条替换：quote 必须逐字照抄"
    "prompt_text 里要替换的原文片段，且这段原文在 prompt_text 里只能出现一次（否则无法"
    "定位，这一条就不要给出）；replacement 填替换后的文字，不能为空。只处理清单列出的"
    "问题，不要顺带改写、删除或新增清单之外的任何文字，其余正文必须逐字保持原样；一条"
    "违规如果没办法用一次简单替换解决，就不要为它生成替换项，宁可少给不要编造。只输出"
    "符合 Schema 的一个 JSON 对象，不输出 Markdown 或解释。"
)


async def propose_minimal_patch(
    *, episode_id: str, segment_no: int, prompt_text: str, violations: list[ProseViolation],
) -> list[_PatchReplacement]:
    """只对局部可修的违规发起一次最小替换提案调用；调用方必须先用
    ``is_locally_patchable`` 过滤出 ``violations``，本函数不重复过滤。调用失败
    （供应商错误/格式修复耗尽）原样向上抛出——与 ``storyboard_prose_review.
    _review_segment`` 不是同一取舍：那边处在生成主链路里，复核失败不能让生成
    本身失败；这里的调用方（``prop_continuity_review.rewrite_flagged_segments``）
    已经自带逐段 ``try/except`` 隔离，真实的供应商/网络失败需要原样冒泡成
    ``outcome.error``，不能在这一层被吞成『没有可应用的修改』——那会把『模型
    确实没找到能改的地方』与『请求根本没发出去/没拿到有效回应』混成同一种
    结果，拦住用户时却给不出真实原因。"""
    payload = {
        "segment_no": segment_no,
        "prompt_text": prompt_text,
        "violations": [
            {"kind": v.kind, "quote": v.quote, "previous_quote": v.previous_quote, "prop_name": v.prop_name, "fix": v.fix}
            for v in violations
        ],
        "output_schema": _PatchResponse.model_json_schema(),
    }
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    response = await model_gateway.chat_structured(
        [
            {"role": "system", "content": _PATCH_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        model_type=_PatchResponse,
        validate=None,
        operation_id=f"storyboard_prop_continuity_patch_{episode_id}_{segment_no}_{fingerprint}",
        max_tokens=_PATCH_ANSWER_TOKENS,
        format_retry_limit=1,
        semantic_retry_limit=0,
        temperature=0.2,
        call_meta={
            "stage_key": "storyboard_prop_continuity_review", "call_role": "storyboard_prop_continuity_minimal_patch",
            "initiator_label": "存量分镜最小修改", "episode_id": episode_id, "segment_no": segment_no,
        },
    )
    return response.replacements
