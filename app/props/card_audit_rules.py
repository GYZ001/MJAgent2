"""道具卡「按现行规则复核」的判据：别名歧义的数据推导、模型调用的提示词与
响应契约、以及对模型返回值的代码核验（CLAUDE.md「禁止黑白名单修复」——模型
提名、代码只核验边界，不自己按词表猜语义）。

复核对象是存量道具卡（已建成、可能是在规则升级前写的），分两件事：
1. 外观子句：把 ``appearance_canonical`` 按 ``judge.split_appearance_
   clauses`` 切成编号子句，模型对每条判定是否属于「别的物件/动作痕迹」
   「剧情时点状态」「不是外观的说明文字」三类之一而该删除；物件固有的旧化/
   包浆/磨损与表面图案（含照片画面内容）保留——两轴判据见
   ``judge.PROP_APPEARANCE_INHERENT_FEATURE_RULE_TEXT``。
2. 别名：(a) 数据推导——某别名与同项目另一张卡的卡名逐字相同、或同时是两张
   以上卡的别名，判定为歧义（纯代码，不发模型调用，见 ``ambiguous_aliases_
   to_drop``）；(b) 模型提名——哪些别名「单独报出来只剩品类名，指认不到这
   一件」，代码核验其确实在别名列表里才采信。

2026-10-03-v2 新增「归属证据」核验（CLAUDE.md「模型提名、代码核验」）：模型
把某条子句判定为 ``other_object_or_mark``（别的物件/动作痕迹）时，必须在
``owner`` 字段指认这条外观信息实际属于谁——要么逐字写出本项目**另一张**道具
卡的卡名或别名（由 ``catalog_text_for_prompt`` 提示词里逐字给出），要么写
``OWNER_PERSON_OR_ACTION``（人物身体或人物动作留下的痕迹，没有自己的道具
卡、也不该有）。代码核验 ``owner`` 落在这两类之一才采信删除；``owner`` 指向
一件本项目没有道具卡的东西（行李牌、蜡烛、表袋、红绳、汤碗、瓷瓶——这类
容器/配件参见 ``judge`` 模块对应规则），降级为「存疑」而不是删除：这条外观
信息没有自己的卡可以落脚，删掉就会让它在任何卡里都不存在。

``uncertain`` 字段（模型自述拿不准）：子句该不该删/该归哪一类，模型如果
自己也判断不了，填 ``uncertain: true`` 并在 ``reason`` 里写清楚为什么犹豫
（包括主体歧义——这处描述到底该算这件道具本体的还是容器/配件的，模型自己
分不清）；代码据此一律不采信删除，只记一条「模型自述拿不准」的存疑，交由
``app.props.card_audit_consensus`` 两次判定合并时与其它存疑来源一并透出，
供人工确认——不按字符串关键词猜模型是不是在表达犹豫（CLAUDE.md「禁止黑白
名单」），拿不准必须由模型自己用这个显式字段声明。

两类判定可以合并进同一次模型调用（``request_prop_card_audit_judgment``），
降低成本。模型返回值一律先过代码核验（下标越界/重复/类别不在允许集合/别名
不在列表里/owner 不在允许集合的一律丢弃或降级，不采信、不报错中止），核验
细节与 ``app.props.judge.rebuild_appearance_excluding`` 的"只删不改写"约束
共同构成"返回子句编号→代码核验→原文拼接"的完整链路，模型本身不直接产出新
外观文本。
"""
from __future__ import annotations

import hashlib
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.harness import model_gateway
from app.schemas import Prop

from . import judge

ALLOWED_REMOVE_CATEGORIES = frozenset({"other_object_or_mark", "plot_state", "not_appearance"})
#: ``owner`` 字段的合法哨兵值：人物身体或人物动作留下的痕迹，没有、也不该有
#: 自己的道具卡。与"本项目另一张卡的卡名/别名"共同构成 owner 的完整合法集合
#: （schema 允许的取值与核验接受的集合两侧对齐，CLAUDE.md「模型契约两侧必须
#: 对齐」）。
OWNER_PERSON_OR_ACTION = "person_or_action"
#: 模型判定 ``remove=true`` 却给出三类之外的 ``category`` 时的存疑类型（审查
#: 发现，2026-10-03 新增）：不是不采信删除就悄悄丢弃，而是降级成和
#: ``owner_without_card``/``model_self_doubt`` 同级的「待人工确认」记录——与
#: ``app.props.card_audit_consensus`` 的 ``DOUBT_TYPE_*`` 同一套字符串字面量
#: 约定（该模块反向引用本模块，不能从这里 import 它，两边各自定义同一字符串）。
DOUBT_TYPE_INVALID_CATEGORY = "invalid_category"


def ambiguous_aliases_to_drop(prop: Prop, all_props: Sequence[Prop]) -> list[dict[str, str]]:
    """数据推导歧义剪除（判据 3a，纯代码、不发模型调用）：``prop`` 的某个别名
    若与同项目另一张卡的卡名逐字相同，或同时是两张以上卡共享的别名，就判定
    为歧义，从 ``prop`` 这一侧（非卡名那一侧）剪除——对称地，另一张卡在它
    自己的复核轮次里会剪掉同一个歧义别名，不在这里跨卡一次性改两张卡。"""
    other_names = {str(p.name or "").strip() for p in all_props if p.name != prop.name}
    alias_owner_count: dict[str, int] = {}
    for p in all_props:
        for alias in p.aliases:
            alias_owner_count[alias] = alias_owner_count.get(alias, 0) + 1
    out: list[dict[str, str]] = []
    for alias in prop.aliases:
        if alias in other_names:
            out.append({
                "alias": alias, "source": "ambiguous_data",
                "reason": f"与既有卡「{alias}」的卡名逐字相同，歧义",
            })
        elif alias_owner_count.get(alias, 0) >= 2:
            out.append({
                "alias": alias, "source": "ambiguous_data",
                "reason": "同时是两张以上道具卡的别名，歧义",
            })
    return out


def other_card_identifiers(prop: Prop, all_props: Sequence[Prop]) -> frozenset[str]:
    """同项目除本卡之外，全部卡名 + 别名的集合——供 owner 核验逐字比对（判据
    见模块 docstring 的「归属证据」）。"""
    out: set[str] = set()
    for p in all_props:
        if p.name == prop.name:
            continue
        out.add(p.name)
        out.update(p.aliases)
    return frozenset(out)


def catalog_text_for_prompt(prop: Prop, all_props: Sequence[Prop]) -> str:
    """给模型看的"本项目全部道具卡名与别名"清单文本（排除本卡自己），要求
    模型判定 ``other_object_or_mark`` 时从这份清单逐字取用 owner。"""
    lines = []
    for p in all_props:
        if p.name == prop.name:
            continue
        names = "、".join([p.name, *p.aliases]) if p.aliases else p.name
        lines.append(f"- {names}")
    return "\n".join(lines) if lines else "（本项目没有其它道具卡）"


def verify_clause_removal_verdicts(
    clause_count: int, raw_verdicts: list[dict[str, Any]], other_identifiers: frozenset[str],
) -> tuple[set[int], list[dict[str, Any]], frozenset[int], list[dict[str, Any]]]:
    """代码核验**单次**模型调用返回的子句判定：下标必须在 ``[1, clause_count]``
    范围内、不重复；``uncertain=true`` 的子句一律不采信删除，转成
    ``model_self_doubt`` 存疑；``remove=true`` 时 ``category`` 不落在
    ``ALLOWED_REMOVE_CATEGORIES`` 里的（schema 没有收紧枚举，容忍模型措辞
    漂移），降级为 ``invalid_category`` 存疑，不静默丢弃这条判定；
    ``category == "other_object_or_mark"`` 时 ``owner`` 必须逐字命中
    ``other_identifiers`` 或等于 ``OWNER_PERSON_OR_ACTION``，否则降级为
    ``owner_without_card`` 存疑（不删，不是不采信——这条判定仍然"被判定过"，
    只是归属没有自己的卡落脚）。

    返回 ``(removed_indexes, removed_records, missing_indexes, owner_doubts)``；
    两次独立调用各自先过这个函数，再由
    ``app.props.card_audit_consensus.merge_clause_judgments`` 取交集——这里
    只做单次核验，不做"两次是否一致"的判断。``missing_indexes`` 语义同此前
    （模型完全没有给出判定的下标，CLAUDE.md「单次长调用会漏掉整个类别，必须
    有可见信号」），调用方应当把它当"本轮判定不完整"处理。
    """
    removed_indexes: set[int] = set()
    removed_records: list[dict[str, Any]] = []
    owner_doubts: list[dict[str, Any]] = []
    seen: set[int] = set()
    for verdict in raw_verdicts:
        if not isinstance(verdict, dict):
            continue  # 形状不对（例如测试桩/旧版本返回了裸字符串）：不采信，不中止整体复核
        try:
            index = int(verdict.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 1 or index > clause_count or index in seen:
            continue
        seen.add(index)
        reason = str(verdict.get("reason") or "")
        if bool(verdict.get("uncertain")):
            owner_doubts.append({"index": index, "doubt_type": "model_self_doubt", "reason": reason})
            continue  # 模型自述拿不准：不采信删除，只记存疑
        if not verdict.get("remove"):
            continue
        category = str(verdict.get("category") or "")
        if category not in ALLOWED_REMOVE_CATEGORIES:
            # 模型判定了 remove=true 却给了一个三类之外的 category：schema 本身
            # 没有收紧枚举（容忍模型措辞漂移），但不能像此前那样静默 continue 把
            # 这条判定直接吞掉——CLAUDE.md「schema 允许的，核验就不许拒绝」的同一
            # 精神反过来也成立：核验也不该在模型明确表达了"该删"之后，因为类别
            # 字符串不认识就让这条判定凭空消失、不留任何痕迹。降级为存疑，交人工
            # 看原文判断，不采信删除。
            owner_doubts.append({
                "index": index, "doubt_type": DOUBT_TYPE_INVALID_CATEGORY,
                "reason": reason, "category": category,
            })
            continue
        if category == "other_object_or_mark":
            owner = str(verdict.get("owner") or "").strip()
            if owner != OWNER_PERSON_OR_ACTION and owner not in other_identifiers:
                owner_doubts.append({
                    "index": index, "doubt_type": "owner_without_card", "reason": reason, "owner": owner,
                })
                continue  # 归属没有自己的卡：不删，记存疑，不让这条外观信息从所有卡里消失
        removed_indexes.add(index)
        removed_records.append({"index": index, "category": category, "reason": reason})
    missing_indexes = frozenset(range(1, clause_count + 1)) - seen
    return removed_indexes, removed_records, missing_indexes, owner_doubts


def verify_alias_removal_verdicts(
    aliases: Sequence[str], raw_verdicts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """代码核验**单次**模型调用提名的「只剩品类名」别名：必须确实在
    ``aliases`` 里才采信（判据 3b 的后半句），不在列表里的提名直接忽略。两次
    独立调用各自先过这个函数，再由
    ``app.props.card_audit_consensus.merge_alias_judgments`` 取交集。"""
    alias_set = set(aliases)
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for verdict in raw_verdicts:
        if not isinstance(verdict, dict):
            continue  # 形状不对（例如测试桩/旧版本返回了裸字符串）：不采信，不中止整体复核
        alias = str(verdict.get("alias") or "").strip()
        if not alias or alias not in alias_set or alias in seen or not verdict.get("category_only"):
            continue
        seen.add(alias)
        out.append({
            "alias": alias, "source": "model_nominated", "reason": str(verdict.get("reason") or ""),
        })
    return out


class _ClauseVerdictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    remove: bool = False
    category: str = ""
    owner: str = ""
    uncertain: bool = False
    reason: str = ""


class _AliasVerdictIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str
    category_only: bool = False
    reason: str = ""


class _PropCardAuditResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    clauses: list[_ClauseVerdictIn] = Field(default_factory=list)
    aliases: list[_AliasVerdictIn] = Field(default_factory=list)


def _audit_prompt(prop: Prop, clauses: list[str], owner_catalog_text: str) -> str:
    numbered = "\n".join(f"{i + 1}. {clause}" for i, clause in enumerate(clauses))
    alias_block = "、".join(prop.aliases) if prop.aliases else "（无）"
    return f"""任务：按现行规则复核一张已建好的漫剧道具卡，判定它的外观锚点子句与别名
里有哪些不该留（规则升级前建的卡可能带着旧规则的问题，需要重新核一遍）。

道具名：{prop.name}
外观锚点子句（已按分隔符编号，请逐条判定，不改写任何子句文字）：
{numbered}
当前别名：{alias_block}
本项目其它道具卡的卡名与别名（判定"别的物件"时，owner 必须从这份清单里逐字取用）：
{owner_catalog_text}

外观子句判定规则：
- {judge.PROP_APPEARANCE_OWN_RULE_TEXT}
- {judge.PROP_APPEARANCE_PLOT_STATE_RULE_TEXT}
- {judge.PROP_APPEARANCE_INHERENT_FEATURE_RULE_TEXT}
- 如果这件道具卡名指的是容器里的内容物、而外观子句写的是容器本身的样子（比如卡名是
  止血丹、子句写的是装它的瓷瓶；卡名是小馄饨、子句写的是盛着它的汤碗；卡名是热牛奶、
  子句写的是盛着它的马克杯），这种情况按"别的物件"判定，owner 要如实填容器的名字——
  如果容器在本项目没有自己的卡，代码会把这类判定降级为存疑而不是直接删除，你只需要
  按实际归属如实判定，不需要因为"容器没有卡"就改判保留。上面"固有外观"规则里已经说明
  随道具一起移动、没有独立呈现意义的配件（绳/盖子/表袋/挂牌）不算"别的物件"，这里的
  容器是另一种情况——容器本身是能单独摆出来、独立存在的东西，只是它恰好没有自己的
  道具卡。
- 不属于以上几类、但读起来根本不是外观描述的子句，同样判定删除，类别填 "not_appearance"，
  包括：纯剧情动作、与视觉无关的说明文字；画风质感说明（"真人实拍质感""光影精致统一"
  "国漫3D动画电影质感"之类）；构图/取景/拍摄手法要求；以及单独读起来不构成完整意思、
  像是从上一条否定句式里被切出来的残留片段（这些子句是按标点机械切分出来的，不理解
  "无/没有+并列项"这类共享否定的结构——比如上一条是"无印花"，紧跟着这一条却是"涂鸦等
  额外装饰"，两条单独看都不完整，这种情况要把它们一起判定删除，不要只删其中一半、让
  留下的另一半把"没有"读成"有"）。
对每一条子句都要给出判定：``remove`` 为 true 时 ``category`` 必须是
"other_object_or_mark"（别的物件本身或别的物件/动作在它上面留下的痕迹）、
"plot_state"（剧情事件造成的时点状态，含事后才有的标注/只在某段剧情才显示的屏幕
界面内容）、"not_appearance"（不是外观的说明文字）三者之一，并给出简短理由；
``category`` 是 "other_object_or_mark" 时必须同时填写 ``owner``：要么逐字抄写上面
"本项目其它道具卡的卡名与别名"清单里的一条，要么填 "{OWNER_PERSON_OR_ACTION}"（这处
痕迹是人物身体或人物动作留下的，不是另一件物件）。不删除的子句 ``remove`` 填 false。
如果某条子句你拿不准该归哪一类、该不该删、或者分不清这处描述该算这件道具本体的还是
算容器/配件的（主体歧义），``uncertain`` 填 true 并在 ``reason`` 里写清楚你犹豫的
理由供人工复核——不要猜测删除，也不要为了给出判定而硬选一个你并不确定的答案。

别名判定规则：
- {judge.PROP_ALIAS_OWN_RULE_TEXT}
对「当前别名」里符合"单独报出来只剩品类名、指认不到这一件具体道具"的，在 aliases 里标记
``category_only: true`` 并给出理由；不符合的不要列出或填 false。

输出 JSON：{{"clauses": [{{"index": int, "remove": bool, "category": str, "owner": str,
"uncertain": bool, "reason": str}}],
"aliases": [{{"alias": str, "category_only": bool, "reason": str}}]}}"""


async def request_prop_card_audit_judgment(
    prop: Prop, clauses: list[str], owner_catalog_text: str, *, call_tag: str,
) -> dict[str, list[dict[str, Any]]]:
    """发起**一次**模型调用取得子句/别名判定；返回值尚未经过代码核验，调用方
    必须调用 ``verify_clause_removal_verdicts``/``verify_alias_removal_
    verdicts`` 再采信。``call_tag``（"a"/"b"）参与 ``operation_id`` 哈希，
    使两次独立判定调用（``app.props.card_audit_consensus``）落在不同的
    ``operation_id`` 上——如果共用同一个 operation_id，模型网关的幂等缓存会
    让第二次调用直接拿到第一次的缓存结果，两次判定就不是真正独立的采样，而是
    同一次结果复制了两份，起不到"用两次独立采样换掉单次不稳定"的作用。"""
    prompt = _audit_prompt(prop, clauses, owner_catalog_text)
    response = await model_gateway.chat_structured(
        [{"role": "user", "content": prompt}],
        model_type=_PropCardAuditResponse,
        validate=None,
        operation_id="audit_prop_card:" + call_tag + ":" + hashlib.sha256(
            f"{prop.name}:{prop.appearance_canonical}:{judge.PROP_CARD_RULES_VERSION}".encode("utf-8")
        ).hexdigest(),
        temperature=0.1,
        max_tokens=1200,
        call_meta={"stage": "audit_prop_card", "prop_name": prop.name, "call_tag": call_tag},
    )
    # ``getattr``/``hasattr`` 兜底：生产路径里 ``chat_structured`` 已经按 ``model_type``
    # 校验过，``response`` 必然是 ``_PropCardAuditResponse`` 实例、字段必然齐全——这里
    # 的兜底只服务于测试桩（跨大量既有道具卡测试文件复用同一个通用 ``chat_structured``
    # 桩，不知道本次调用是"建卡"还是"复核"）：缺字段/字段形状不对时当"无判定"处理，
    # 下游 ``verify_*_verdicts`` 对此类条目同样会丢弃（不采信、不中止）。
    raw_clauses = getattr(response, "clauses", None) or []
    raw_aliases = getattr(response, "aliases", None) or []
    return {
        "clauses": [c.model_dump() if hasattr(c, "model_dump") else c for c in raw_clauses],
        "aliases": [a.model_dump() if hasattr(a, "model_dump") else a for a in raw_aliases],
    }
