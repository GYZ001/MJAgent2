"""稳定称谓建卡：真名揭晓之前就把视觉身份钉住。

用户诉求（2026-08-31）：有的角色戏份很重，但第一集只给称呼、不给真名，于是
拿不到定妆照，分镜台每个镜头各自想象她长什么样，样貌逐镜漂移；而且这不是第一
集独有——真名揭晓之前的每一集都会重演一次。

为什么"去后文查真名"救不了这一类：实测 ``proj_f8cf2eeb2e66`` 的「许师姐」在
第 1、5、6、8、10、12… 共 67 章逐字出现，而她的真名「许清」在**前 30 章里
一次都没出现**——远在 ``IDENTITY_DISCOVERY_FORWARD_CHAPTERS`` 的前瞻窗口之外，
把窗口放大还会撞上"前瞻只能消歧、不得把后文剧情拉进本集"这条红线。

症结也不在别名机制：``functional_candidate_verdict`` 只能把一个未解析标签绑到
**已经存在**的角色卡上，而第一集人物谱是空的，候选集里根本没有正确答案，必然
落 ``functional_extras`` 当无图群演。缺的是第一张多米诺——先建卡。

判据是纯字符串包含的跨章计数（零语义、零模型调用，与
``true_name.py::_prep_pack_true_name_dossier`` 同一个原语）：标签在全书逐字命中
的章数 > ``PERSISTENT_APPELLATION_MIN_CHAPTERS``。实测这一集的分离度没有灰带——
孟浩 1609 章、许师姐 67 章、王有材 30 章（后两者都是真身份），而一次性描述是
「虎头虎脑的少年」2 章、「绿袍男子」1 章、「白白净净较胖的少年」0 章。

一致性由既有机制接力，本模块不自己实现：
- 建卡时 ``ensure_character_card`` 先走 ``resolve_card_build_or_merge``——如果这
  个称谓其实是人物谱里某人的另一种叫法，登记别名、复用那张卡，不建第二张；
  返回的 ``name`` 是**归属者的规范名**，于是后来的各种代称都收敛到最初那张卡。
- 真名揭晓时走 ``card_rebind.rebind_character_card``，它是
  ``UPDATE character_portraits SET character_name=?`` 原地改名，定妆照那一行不动
  ——从第一集到真名揭晓，用的一直是同一张图。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.harness import model_gateway

log = logging.getLogger(__name__)

# 用户定的产品口径（2026-08-31）：全书出现超过 2 章就算一个角色，不再当群演。
PERSISTENT_APPELLATION_MIN_CHAPTERS = 2
# 跨章片段最多取几章、每处前后各取多少字给模型看。
_IDENTITY_CONTEXT_CHAPTERS = 6
_IDENTITY_CONTEXT_WINDOW = 90


def label_chapter_span(conn, project_id: str, label: str) -> int:
    """标签在本项目全书逐字命中的章数。零语义：只做字符串包含。"""
    label = str(label or "").strip()
    if not label:
        return 0
    rows = conn.execute(
        "SELECT content FROM chapters WHERE project_id=?", (project_id,),
    ).fetchall()
    return sum(1 for row in rows if label in str(row["content"] or ""))


def label_chapter_contexts(conn, project_id: str, label: str) -> list[dict[str, Any]]:
    """标签在各章首次逐字出现处的前后片段（最多 _IDENTITY_CONTEXT_CHAPTERS 章），供同一人核验。"""
    label = str(label or "").strip()
    if not label:
        return []
    rows = conn.execute(
        "SELECT idx, content FROM chapters WHERE project_id=? ORDER BY idx", (project_id,),
    ).fetchall()
    contexts: list[dict[str, Any]] = []
    for row in rows:
        content = str(row["content"] or "")
        position = content.find(label)
        if position < 0:
            continue
        start = max(0, position - _IDENTITY_CONTEXT_WINDOW)
        end = min(len(content), position + len(label) + _IDENTITY_CONTEXT_WINDOW)
        contexts.append({"chapter_idx": int(row["idx"] or 0), "text": content[start:end].replace("\n", " ")})
        if len(contexts) >= _IDENTITY_CONTEXT_CHAPTERS:
            break
    return contexts


class _AppellationIdentityVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    same_individual: bool
    reason: str = ""


async def appellation_denotes_one_person(
    label: str, contexts: list[dict[str, Any]], *, project_id: str, episode_id: str | None,
) -> bool:
    """跨章逐字命中只证明"这个词反复出现"，证明不了"每次都是同一个人"——
    「受伤的修士」在第 11 集（右肩受伤者）与第 12 集（手臂受伤者）是两场打斗里的两个人
    （2026-09-14 实测），按同一张卡绑定会把不同群演并成一张脸。这里把各章片段交给模型
    判断是否同一人；判不出、调用失败都按"不是"处理——不确定不绑，与候选判别同一套纪律。
    """
    if len(contexts) < 2:
        return False
    catalog = "\n\n".join(f"[第{item['chapter_idx']}章] …{item['text']}…" for item in contexts)
    prompt = f"""下面是称谓「{label}」在小说不同章节里的出现片段（每段前标了章号）：
{catalog}

任务：只依据这些片段，判断各章里的「{label}」是否都指同一个人。
- same_individual 填 true 的依据：各片段的身份线索连贯——同一处所、同一段人物关系、前后事件相互承接，
  或作者把这个称谓固定用来指代某一个具体的人；
- same_individual 填 false 的依据：各片段各自描述不同场合里不同的人（例如不同打斗里各自受伤的修士、
  不同店铺里的掌柜、路上遇到的不同老者），或片段信息不足以确认是同一个人；
- 拿不准时填 false。reason 用一句话说明依据。
只输出符合 Schema 的 JSON。"""
    try:
        verdict = await model_gateway.chat_structured(
            [{"role": "user", "content": prompt}],
            model_type=_AppellationIdentityVerdict, validate=None,
            operation_id=f"persistent_appellation_identity_{episode_id or project_id}_{label}",
            max_tokens=400, temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001 模型调用失败按"不是同一人"处理：不确定不绑
        log.warning("[PERSISTENT_APPELLATION][核验失败] 「%s」：%s", label, str(exc)[:160])
        return False
    log.info("[PERSISTENT_APPELLATION][同一人核验] 「%s」 -> %s：%s", label, verdict.same_individual, verdict.reason[:80])
    return bool(verdict.same_individual)


def label_episode_anchor(segments: Any, label: str) -> dict[str, Any] | None:
    """标签在本集原文里的第一个字面锚点段（1-based 段号 + 该段原文）。

    与候选判别的钉证同形：调用方拿它写 provenance，要求锚点是代码检索出的
    真实原文而不是模型转录。本集里连一次字面出现都没有（标签是模型转述的
    描述短语）就返回 ``None``——钉不住就不建卡，与"不确定不绑"同一套纪律。
    """
    for index, segment in enumerate(segments or [], start=1):
        text = str(getattr(segment, "text", "") or "")
        if label and label in text:
            return {"segment_index": index, "text": text}
    return None


async def resolve_persistent_appellation(
    conn, *, project_id: str, episode_no: int, label: str, segments: Any, episode_id: str | None = None,
) -> dict[str, Any] | None:
    """跨章稳定的称谓 → 建卡出图，返回可直接并入候选判别结果的 payload。

    ``None`` 表示不适用（跨章次数不够、本集钉不住锚点、跨章片段经模型核验不是同一个人、
    建卡没成），调用方维持原行为让标签落 functional_extras——不确定不绑，与候选判别同一套纪律。

    绑定不以"已有定妆照"为门槛：出图已解耦到后台（下面 generate_portrait=False），
    刚建的卡在这一刻必然没图，若在此拒绝绑定，标签落群演、卡进不了准备包，
    分镜前资产准备也就永远轮不到给它补图——真实事故：第 11 集「大汉」建成
    「曹阳（大汉）」后仍以无图群演投产。生产侧取图按 ``bible:{name}`` 查人物谱
    当前定妆照（app.multiview / storyboard_ops.current_portraits），不看快照。
    """
    from app.portraits import ensure_character_card

    if label_chapter_span(conn, project_id, label) <= PERSISTENT_APPELLATION_MIN_CHAPTERS:
        return None
    anchor = label_episode_anchor(segments, label)
    if anchor is None:
        return None
    if not await appellation_denotes_one_person(
        label, label_chapter_contexts(conn, project_id, label), project_id=project_id, episode_id=episode_id,
    ):
        return None
    # require_identity_card：跨章复现已经是"这是个稳定身份"的结构证据，不能
    # 再让模型以"本集戏份少"把它降回路人——那正是漂移的来源。
    result = await ensure_character_card(
        project_id, label, episode_no,
        # 与 discovery 同一条：出图解耦到后台，这里只建卡。
        generate_portrait=False, require_identity_card=True,
        accept_thin_grounded_card=True,  # 外观被原文核验削薄时照建，不因长度让整集失败
    )
    status = str((result or {}).get("status") or "")
    # "exists" 是这个称谓命中了人物谱里已有角色的别名，返回的 name 是归属者的
    # 规范名——正是"后来的代称绑回最初那张卡"。"conflict" 一律不接（同一称呼
    # 命中多个角色是真实存在的合法数据，猜一个就会制造错误归属）。
    if status not in {"added", "exists"}:
        # 2026-09-15《龙猫出爪》主角「龙猫」在这里静默落成群演（外观核验删空 → error），
        # 整条链路没有一行记录；失败原因必须可见，否则只能事后翻供应商调用去猜。
        log.warning("[PERSISTENT_APPELLATION] 「%s」跨章同一人已确认但建卡未成：status=%s reason=%s",
                    label, status or "(空)", (result or {}).get("reason") or (result or {}).get("portrait_error") or "")
        return None
    canonical_name = str((result or {}).get("name") or "").strip() or label
    return {
        "resolved": True,
        "canonical_name": canonical_name,
        "persistent_appellation": True,
        "segment_index": anchor["segment_index"],
        "text": anchor["text"],
    }
