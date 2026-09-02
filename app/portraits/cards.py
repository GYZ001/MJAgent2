"""人物卡的评估与保证：assess_new_character/ensure_character_card，
以及 bible 的临时/待定角色视图。
"""

from __future__ import annotations

import json

from collections.abc import Callable

from pydantic import ValidationError

from app.db import get_conn, get_setting, new_id, now, set_setting
from app.errors import ContentGenerationError, code_ref
from app.harness import model_gateway
from app.portraits.card_owner import bible_known_labels
from app.refs import production_appearance_anchor
from app.schemas import Bible, Character, extract_json

from ._db_probe import _has_column
from .bible_compat import (  # noqa: F401 -- 重新导出，见下方模块末尾的说明注释
    bible_with_pending_characters_for_text,
    bible_with_provisional_characters,
)
from .card_merge import courtesy_name_redirect, resolve_card_build_or_merge, resolve_card_name
from .card_verdict import unimportant_verdict_result
from .constants import (
    APPEARANCE_MAX,
    APPEARANCE_MIN,
    CHARACTER_CARD_MAX_TOKENS,
)
from .discovery_fragments import (
    _bible_lock,
    _card_lock,
    _card_owner_lookup,
    _discovery_skip_key,
    _forward_fragments,
    _fragment_signature,
    _name_in_bible,
    _non_character_skip_key,
)
from .portrait_io import _append_character_to_bible, _generate_discovered_character_portrait

# 人物谱是"可以被选角、被定妆、能出镜表演的人"的登记表。宗门、地点、器物、
# 功法都不是人，它们属于场景库或 reference 身份，绝不能占据人物卡。
#
# 生产事故：模型给「靠山宗」的建卡理由是「属于独立的组织类出场单元…需单独建卡
# 保证漫剧场景一致性」，给「凝气卷」的理由是「靠山宗发放的修行典籍」——两次都
# 如实说明了这不是人，却照样入了人物谱。原因是建卡判定问的一直是"值不值得做
# 一致性锚点"，从来没有人问过"这是不是一个人"。
CHARACTER_SUBJECT_PERSON = "person"

# role 是合同枚举，不是自由文本。「靠山宗」当初写进来的 role 是"重要场景载体"，
# 根本不在允许值里，却因为只检查了非空而落库。
CHARACTER_CARD_ROLES = ("主角", "重要配角", "反派")


def _candidate_requires_identity_card(item: dict, known_names: set[str]) -> bool:
    """Only a new named identity that appears or speaks needs a visual card."""
    name = str(item.get("name") or "").strip()
    return bool(
        name and name not in known_names
        and str(item.get("identity_kind") or "named") == "named"
        and item.get("kind") != "mentioned"
    )


async def assess_new_character(name: str, fragments: str, *, style: str,
                               known_names: list[str], ep_label: str,
                               require_identity_card: bool = False,
                               chapters_by_idx: dict[int, str] | None = None) -> dict:
    """针对一个【具体名字】判断是否值得单独建卡（戏份够 / 画面多），并产出角色卡字段。
    返回 {important, reason, role, appearance_canonical, personality, speech_style,
    relationships, source_evidence}。

    chapters_by_idx：`_forward_fragments` 一并返回的全书原文查找表（未截断），用于核验
    模型申报的 source_evidence（王有材事故修复新增，见
    logs/appearance_provenance_plan.md）。生产路径（`ensure_character_card`）总是显式
    传入；测试直连本函数且不传时按 None 处理，此时无法核验任何证据，安全默认是"全部
    拒绝"（不确定不采信），不是静默放行。
    """
    from app.stages import _appearance_evidence_verified

    known = "、".join(known_names) or "（无）"
    identity_contract = (
        "身份消歧模型已用上下文确认这是稳定真名；本次任务不是重新判断戏份重要度，"
        "而是生成完整的最小人物卡。无论戏份多少都输出 important=true；"
        "原文对这个角色本人确有可视描写的字段，逐字取用；原文没写的可视字段，通用形态"
        "（年龄段/发型发色/服装款式颜色）按项目画风与身份合理设定，不写需要举证的"
        "标志性特征。"
        if require_identity_card else
        "请判断该称谓是否值得单独建人物卡并定妆。"
    )
    decision_contract = (
        f"- identity_card_required=true：固定输出 important=true，并完成 20~80 字"
        f" appearance_canonical；不得因只出现一次而拒绝建卡。"
        if require_identity_card else
        f"- important=true 仅当：「{name}」是【真正的新角色】，且在这段剧情里"
        "【反复出场 / 有正面戏份 / 画面感强】，值得稳定其外观。\n"
        "- important=false：路人、只被提及一两次、纯功能性提及，"
        "或其实是已有角色的别名/外号/尊称。"
    )
    prompt = f"""任务：判断小说角色「{name}」是否值得【单独建人物卡并定妆】（用作漫剧出镜的一致性锚点）。

身份合同：{identity_contract}

已有角色（若「{name}」其实是这些人的别名/外号/尊称，则 important=false）：
{known}

下面是原文中提及「{name}」的片段（{ep_label}）：
{fragments[:12000]}

判定口径：
{decision_contract}
- appearance_canonical 是"固定外观锚点串"：40~60 字，只写视觉可见信息，不写性格。通用
  形态（性别年龄感/发型发色/服装款式与颜色）原文没写处按画风（{style}）合理设定，不需要
  举证；标志性特征只有原文对这个角色本人确有描写才写，且要在 source_evidence 里给出
  evidence_chapter_index（取原文片段【第 N 章】块头里的数字）与 evidence_quote（支撑该
  特征的原文逐字短句，40 字以内、必须原样连续照抄，短句本身要能读出是在写这个角色
  本人，不是同段落里的其他人）；原文没有就不写，source_evidence 留空数组即可，不是缺陷。
- appearance_canonical 只允许常规完整着装、中性站姿下可直接看见、可跨镜稳定复现的静态形态；不得写性格、欲望、气质、眼神行为、对他人的注视方式、裸体、内衣、私密身体部位或必须暴露身体才能看见的特征。

先判断「{name}」指的是什么：
- subject_kind=person：一个具体的人（可以被选角、被定妆、能出镜表演）。
- subject_kind=organization：宗门、门派、家族、势力、商号等组织。
- subject_kind=place：地点、建筑、区域。
- subject_kind=object：器物、法宝、典籍、功法、丹药等物品。
- subject_kind=other：以上都不是。
人物谱只登记 person。组织、地点、器物即使在剧情里极其重要、也确实需要视觉一致性，
也一律 important=false——它们属于场景库，不属于人物谱。

- canonical_name 是这张卡的固定名字：「{name}」是人名就原样填；原文明确给出此人真名就填真名（须逐字出现在上面片段里）；
  「{name}」只是称呼/身份/外貌描述（老人、女孩、黑衣人）时，在它基础上加限定写成有区分度的固定称谓（如「守墓老人」），不得只填通称或凭空起名。
只输出一个 JSON 对象：
{{"subject_kind": "person|organization|place|object|other", "important": true/false, "canonical_name": str, "reason": "一句话依据", "role": "主角|重要配角|反派", "appearance_canonical": str, "personality": str, "speech_style": str, "relationships": [{{"to": str, "relation": str}}], "source_evidence": [{{"evidence_chapter_index": int, "evidence_quote": str}}]}}"""

    async def _assess_once(extra_instruction: str) -> dict:
        messages = [{"role": "user", "content": prompt + extra_instruction}]
        raw = await model_gateway.chat(
            messages,
            temperature=0.3,
            max_tokens=CHARACTER_CARD_MAX_TOKENS,
            call_meta={
                "stage": "assess_new_character",
                "character_name": name,
                "expected_json": True,
            },
        )
        try:
            return extract_json(raw)
        except ValueError as exc:
            raise ContentGenerationError(
                f"新角色「{name}」人物卡结构化输出不完整"
            ) from exc

    def _build_verdict(obj: dict) -> dict:
        important = bool(obj.get("important"))
        subject_kind = str(obj.get("subject_kind") or "").strip()
        appearance = production_appearance_anchor(
            (obj.get("appearance_canonical") or "").strip()
        )
        if len(appearance) > APPEARANCE_MAX:
            appearance = appearance[:APPEARANCE_MAX]
        role = (obj.get("role") or "重要配角").strip() or "重要配角"
        # 完整度只看长度 + role 是否合法：新契约下"标志性特征齐全"不再是必要条件——
        # 通用形态本来就允许没有标志性特征，见 assess_new_character docstring。
        card_complete = (
            APPEARANCE_MIN <= len(appearance) <= APPEARANCE_MAX
            # role 是闭合枚举，不是自由文本。
            and role in CHARACTER_CARD_ROLES
        )
        # 模型原始的 important 信号（降级前）：card_complete=False 时，代码把
        # important 强制降级成 false，但下游不能把这次降级误报成"模型判了戏份
        # 不足"——那是格式问题，与 model_important 一起交给 ensure_character_card
        # 的 unimportant_verdict_result 区分 card_incomplete / skipped_minor。
        model_important = important
        incomplete_reason = ""
        if not card_complete:
            if not (APPEARANCE_MIN <= len(appearance) <= APPEARANCE_MAX):
                incomplete_reason = (
                    f"appearance_canonical 长度 {len(appearance)} 字，"
                    f"要求 {APPEARANCE_MIN}~{APPEARANCE_MAX} 字"
                )
            else:
                incomplete_reason = (
                    f"role「{role}」不是 {'/'.join(CHARACTER_CARD_ROLES)} 之一"
                )
        if important and not card_complete:
            important = False  # 外观太稀薄不足以稳定定妆 → 不建卡
        if subject_kind != CHARACTER_SUBJECT_PERSON:
            # 不是人就不进人物谱，且这一条不受 require_identity_card 影响：
            # 身份消歧确认了"这是一个稳定的专名"，并不等于确认了"这是一个人"。
            # 未声明 subject_kind 的旧响应同样落在这里，方向是保守的。
            important = False
        known_set = set(known_names)
        # 只保留指向【已知角色】且 relation 非空的关系；Relationship.to/relation 必填，漏 relation 会让校验崩。
        rels = [
            {"to": r["to"], "relation": str(r.get("relation") or "").strip()}
            for r in (obj.get("relationships") or [])
            if isinstance(r, dict) and r.get("to") in known_set and str(r.get("relation") or "").strip()
        ]
        # 标志性特征证据：只有非空、逐字核验通过的条目才登记；核验失败的条目单独
        # 记下失败原因，供 require_identity_card 路径的重试逻辑使用（未通过核验时
        # 不做文本手术裁剪 appearance_canonical 本身——那是子句边界识别问题，做不到
        # 可靠的自动化字符串处理，交给模型下一轮自己重写，见
        # logs/appearance_provenance_plan.md 第 8 节风险 7）。
        verified_evidence: list[dict] = []
        rejected_evidence: list[dict] = []
        for item in (obj.get("source_evidence") or []):
            if not isinstance(item, dict):
                continue
            quote = str(item.get("evidence_quote") or "").strip()
            if not quote:
                continue
            try:
                chapter_index = int(item.get("evidence_chapter_index"))
            except (TypeError, ValueError):
                rejected_evidence.append({
                    "evidence_chapter_index": item.get("evidence_chapter_index"),
                    "evidence_quote": quote,
                })
                continue
            if _appearance_evidence_verified(
                chapters_by_idx or {}, {name}, chapter_index, quote,
            ):
                verified_evidence.append({
                    "evidence_chapter_index": chapter_index,
                    "evidence_quote": quote,
                })
            else:
                rejected_evidence.append({
                    "evidence_chapter_index": chapter_index,
                    "evidence_quote": quote,
                })
        return {
            "important": important,
            "model_important": model_important,
            "card_complete": card_complete,
            "incomplete_reason": incomplete_reason,
            "subject_kind": subject_kind,
            "canonical_name": str(obj.get("canonical_name") or "").strip(),
            "is_person": subject_kind == CHARACTER_SUBJECT_PERSON,
            "reason": (obj.get("reason") or "").strip(),
            "role": role,
            "appearance_canonical": appearance,
            "personality": (obj.get("personality") or "").strip(),
            "speech_style": (obj.get("speech_style") or "").strip(),
            "relationships": rels,
            "source_evidence": verified_evidence,
            "rejected_evidence": rejected_evidence,
        }

    verdict = _build_verdict(await _assess_once(""))
    # 已确认真名却拿到过薄的人物卡、或标志性特征证据核验不通过时，做一次有界重试，
    # 而不是首轮不完整/不实就让整条剧本硬失败（这是与结构化输出同源的单点脆弱性）。
    # 重试提示按实际失败原因动态拼接，不是静态模板：证据核验不通过时明确给出"换一条
    # 真实证据"或"去掉这个特征只写通用形态"两条合法出路，不再逼模型必须保留一个
    # 标志性特征（那正是王有材事故的激励结构）。
    if require_identity_card and (not verdict["card_complete"] or verdict["rejected_evidence"]):
        reasons: list[str] = []
        if not verdict["card_complete"]:
            reasons.append(
                f"appearance_canonical 不完整（当前 {len(verdict['appearance_canonical'])} 字，"
                f"要求 {APPEARANCE_MIN}~{APPEARANCE_MAX} 字；或 role 不是 主角/重要配角/反派 "
                "之一）。请重写为完整外观锚点，只写通用形态（性别年龄感/发型发色/服装款式与"
                "颜色）即可满足长度要求，不必强行加标志性特征。"
            )
        if verdict["rejected_evidence"]:
            detail = "；".join(
                f"第 {item.get('evidence_chapter_index')} 章引句「{item.get('evidence_quote')}」"
                for item in verdict["rejected_evidence"]
            )
            reasons.append(
                "以下标志性特征引用的证据未通过核验（可能不是该章逐字原文、超过 40 字、或角色"
                f"本人姓名没有出现在这句引文本身里）：{detail}。请换一条真实、40 字以内、且角色"
                "本人姓名与所写特征同句出现的原文逐字短句作为新证据；找不到就直接去掉这个"
                "标志性特征，appearance_canonical 只保留通用形态，这同样是合法结果，不要为了"
                "保住这个特征而勉强凑一条证据。"
            )
        retry_instruction = (
            "\n\n上一轮 appearance_canonical 不完整，需要修正：\n"
            + "\n".join(reasons)
            + "\n并固定 important=true。"
        )
        verdict = _build_verdict(await _assess_once(retry_instruction))
    return verdict


async def ensure_character_card(
    project_id: str,
    name: str,
    from_episode_no: int,
    *,
    generate_portrait: bool = True,
    require_identity_card: bool = False,
    write_guard: Callable[[], None] | None = None,
    identity_source_labels: list[str] | None = None,
) -> dict:
    """检查新角色的原文份量，并自动完成建卡与定妆包。

    默认由 AI 判断是否需要跨镜头保持一致；若上游身份模型已确认稳定真名，
    ``require_identity_card`` 会要求模型完成最小人物卡，不能再以戏份少降为路人。
    一次性功能角色仍跳过。建卡先落库，定妆包生成失败时保留卡片并由分镜前
    的自愈步骤重试，不再暴露人工待审队列。带 (project,name) 锁，可幂等并发。

    ``identity_source_labels``：本次身份决议里同一 identity_group 下、除 ``name``
    本身外的其它 source_label（换称呼但指向同一个人）。新建卡时会尝试把它们登记
    为 ``Character.aliases``（见 ``.card_merge.resolve_card_build_or_merge`` 内部
    调用的 ``.card_aliases.new_card_aliases``），让下次同一个人换个称呼出现时
    ``card_owner`` 的别名匹配查得到。``cards_ensure.py`` 的 ``unknown_by_name``
    分组、``identity_adjudication.py`` 的 ``identity.source_names`` 都已接线传入；
    未传（或传空）时按原样跳过，不写任何 aliases，与历史行为一致。真正建卡之前，
    ``resolve_card_build_or_merge`` 还会先问一遍 ``name`` 是不是人物谱里已有某个
    角色的另一种叫法，是则登记别名、复用既有卡，不建新卡（见该函数 docstring）。
    """
    name = (name or "").strip()
    if not name:
        return {"status": "skipped", "reason": "empty"}
    if write_guard:
        write_guard()
    conn = get_conn()
    if (owner_result := _card_owner_lookup(conn, project_id, name)) is not None:
        return owner_result
    async with await _card_lock(project_id, name):
        if write_guard:
            write_guard()
        if (owner_result := _card_owner_lookup(conn, project_id, name)) is not None:  # 拿到锁后复查（并发兜底）
            return owner_result
        if not _has_column(conn, "projects", "bible_auto_changes_json"):
            conn.execute("ALTER TABLE projects ADD COLUMN bible_auto_changes_json TEXT")
        pending_row = conn.execute("SELECT bible_auto_changes_json FROM projects WHERE id=?", (project_id,)).fetchone()
        try:
            change_items = json.loads(pending_row["bible_auto_changes_json"] or "[]") if pending_row else []
        except (TypeError, ValueError, json.JSONDecodeError):
            change_items = []
        existing_change = next((
            item for item in change_items if item.get("kind") in {"new_character", "character_discovery", "new_bible_character"}
            and item.get("character") == name and item.get("status") in {"pending", "processing", "auto_applied_asset_failed"}
        ), None)
        # 负缓存：判过"戏份不足"的名字先不重判——判据挂在这次检索到的原文片段
        # 内容上，不是挂在"过了多少集"（片段没变就仍是同一次判断的延续；片段变了
        # 就必须重判，见 _fragment_signature/discovery_fragments.py）。
        fragments, ep_label, forward_chapters_by_idx = _forward_fragments(conn, project_id, name, from_episode_no)
        # 字/改名先归位（「长生」→「关羽」，见 courtesy_name_redirect）：有卡则登记别名复用，无卡则改在全名下建卡。
        redirect = await courtesy_name_redirect(conn, project_id, name, forward_chapters_by_idx, write_guard)
        if isinstance(redirect, dict):
            return redirect
        if redirect:
            labels = [*(identity_source_labels or []), name]
            return await ensure_character_card(project_id, redirect, from_episode_no, generate_portrait=generate_portrait,
                                               require_identity_card=require_identity_card, identity_source_labels=labels, write_guard=write_guard)
        fragment_signature = _fragment_signature(fragments)
        skip_raw = get_setting(_discovery_skip_key(project_id, name))
        if skip_raw and existing_change is None and not require_identity_card:
            if skip_raw == fragment_signature:
                return {"status": "skipped_minor", "name": name, "reason": "recently judged minor"}
        bible_artifact_supported = _has_column(conn, "projects", "bible_artifact_id")
        select_cols = "bible_json, bible_version"
        if bible_artifact_supported:
            select_cols += ", bible_artifact_id"
        project = conn.execute(
            f"SELECT {select_cols} FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        if not project or not project["bible_json"]:
            return {"status": "skipped", "name": name, "reason": "no bible"}
        bible = Bible.model_validate(json.loads(project["bible_json"]))
        style = bible.world.visual_style_canonical
        known = sorted(bible_known_labels(bible))
        if existing_change is not None:
            change_payload = (
                existing_change.get("payload")
                if isinstance(existing_change.get("payload"), dict) else {}
            )
            try:
                char_obj = Character.model_validate(change_payload.get("character_card"))
            except ValidationError as exc:
                return {"status": "error", "name": name, "reason": f"pending card invalid {exc}"[:240]}
            verdict = {
                "reason": existing_change.get("reason") or "AI 已判定为需要跨镜头保持的新角色",
            }
        else:
            if not fragments:
                # 原文里根本检索不到这个名字（多半是剧本臆造/称谓）。
                if require_identity_card:
                    return {
                        "status": "error", "name": name,
                        "reason": "真名已确认，但人物卡缺少可核验的原文片段",
                    }
                set_setting(_discovery_skip_key(project_id, name), fragment_signature)
                return {"status": "skipped_minor", "name": name, "reason": "no fragments in novel"}
            try:
                assessment_options = {"style": style, "known_names": known, "ep_label": ep_label, "chapters_by_idx": forward_chapters_by_idx}
                if require_identity_card:
                    assessment_options["require_identity_card"] = True
                verdict = await assess_new_character(
                    name, fragments, **assessment_options,
                )
            except Exception as exc:  # noqa: BLE001
                return {"status": "error", "name": name,
                        "reason": "新角色评估失败" + code_ref(exc, action="assess_new_character",
                                                              context={"project_id": project_id, "name": name})}
            if write_guard:
                write_guard()
            card_complete = bool(verdict.get("card_complete")) or (
                bool(str(verdict.get("role") or "").strip())
                and APPEARANCE_MIN
                <= len(str(verdict.get("appearance_canonical") or "").strip())
                <= APPEARANCE_MAX
            )
            # 以 subject_kind 为准（_build_verdict 一定会给出它），缺失同样拦截：
            # 无法断言"这是一个人"时，保守的方向就是不进人物谱。
            if str(
                verdict.get("subject_kind") or ""
            ).strip() != CHARACTER_SUBJECT_PERSON:
                # 人格是独立的硬闸门，不能被 require_identity_card 绕过：身份消歧
                # 确认的是"这是一个稳定的专名"，不是"这是一个人"。宗门、器物、
                # 地点即使专名稳定、戏份很重，也只能留在场景库/reference 身份里。
                set_setting(
                    _discovery_skip_key(project_id, name), fragment_signature
                )
                set_setting(_non_character_skip_key(project_id, name), "1")
                return {
                    "status": "skipped_not_person",
                    "name": name,
                    "subject_kind": verdict.get("subject_kind") or "",
                    "reason": verdict["reason"],
                }
            unimportant_result = unimportant_verdict_result(
                name, verdict, require_identity_card=require_identity_card,
                card_complete=card_complete, project_id=project_id,
                fragment_signature=fragment_signature,
            )
            if unimportant_result is not None:
                return unimportant_result
            named = await resolve_card_name(conn, project_id, name, verdict, fragments, forward_chapters_by_idx, write_guard)
            if isinstance(named, dict):
                return named
            label, name = name, named  # 此后 name 是卡名（可能是「守墓老人」），label 是触发建卡的称谓
            build_result = await resolve_card_build_or_merge(
                conn, project_id, name, bible, verdict, identity_source_labels, forward_chapters_by_idx,
                write_guard, descriptive_label=label if label != name else None,
            )
            if isinstance(build_result, dict):
                return build_result
            char_obj = build_result

        # 保留内部追溯记录，但不再把它当成用户待审任务。
        existing = existing_change
        if existing is None:
            evidence_fragments = [part.strip() for part in fragments.split("\n……\n") if part.strip()][:6]
            existing = {
                "id": new_id("change"), "kind": "new_character", "status": "processing", "character": name,
                "ep_start": from_episode_no, "reason": verdict["reason"], "created_at": now(),
                "payload": {"character_card": char_obj.model_dump(mode="json"), "source_episode": from_episode_no,
                            "source_episode_label": ep_label, "evidence_fragments": evidence_fragments},
            }
            change_items.append(existing)
        else:
            existing["status"] = "processing"
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(change_items, ensure_ascii=False), project_id),
        )
        conn.commit()

        card = char_obj.model_dump(mode="json")
        bible_lock = await _bible_lock(project_id)
        async with bible_lock:
            if write_guard:
                write_guard()
            appended = _append_character_to_bible(conn, project_id, card)
        if not appended and (owner_result := _card_owner_lookup(conn, project_id, name)) is not None:
            # 并发竞态：写锁内复查（_append_character_to_bible 自己也会用
            # card_owner 复核）发现名字/别名已经被另一路并发调用抢先落库，不是
            # 真的写入失败。必须原样返回归属者结果（跟函数开头两处早退分支同一个
            # 出口），不能落进下面"写入失败"的错误分支——那会在归属其实已经
            # 确定的情况下把返回状态误报成 error，也不能顺势往下走判成"added"，
            # 那会让调用方以为又建出了一张新卡（真实归属早已属于另一个名字）。
            return owner_result
        if not appended and not _name_in_bible(conn, project_id, name):
            existing["status"] = "auto_apply_failed"
            existing["decision_reason"] = "人物卡写入失败"
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {"status": "error", "name": name, "reason": "character card commit failed"}
        set_setting(_discovery_skip_key(project_id, name), "")

        if not generate_portrait:
            existing["status"] = "auto_applied_asset_pending"
            existing["decided_at"] = now()
            existing["decision_reason"] = "人物卡已加入；定妆包等待独立资产环节确认"
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {
                "status": "added",
                "name": name,
                "change_id": existing["id"],
                "has_portrait": False,
                "portrait_deferred": True,
                "reason": verdict["reason"],
                "character_card": card,
            }

        latest = conn.execute(
            "SELECT bible_version FROM projects WHERE id=?", (project_id,),
        ).fetchone()
        try:
            portrait = await _generate_discovered_character_portrait(
                project_id,
                name,
                style,
                char_obj.appearance_canonical,
                ep_start=from_episode_no,
                bible_version=int(latest["bible_version"] or 0) if latest else 0,
            )
        except Exception as exc:  # noqa: BLE001 -- 卡片仍可约束剧本，分镜前自动重试资产
            public = code_ref(
                exc,
                action="auto_generate_discovered_character_portrait",
                context={"project_id": project_id, "name": name, "episode_no": from_episode_no},
            )
            existing["status"] = "auto_applied_asset_failed"
            existing["decided_at"] = now()
            existing["decision_reason"] = public
            conn.execute(
                "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
                (json.dumps(change_items, ensure_ascii=False), project_id),
            )
            conn.commit()
            return {
                "status": "added", "name": name, "change_id": existing["id"],
                "has_portrait": False, "reason": verdict["reason"],
                "portrait_error": public, "character_card": card,
            }

        if write_guard:
            write_guard()
        existing["status"] = "auto_applied"
        existing["decided_at"] = now()
        existing["decision_reason"] = "AI 判定需要人物卡并已自动生成定妆包"
        existing.setdefault("payload", {})["portrait_id"] = portrait.get("portrait_id")
        conn.execute(
            "UPDATE projects SET bible_auto_changes_json=? WHERE id=?",
            (json.dumps(change_items, ensure_ascii=False), project_id),
        )
        conn.commit()
        return {
            "status": "added", "name": name, "change_id": existing["id"],
            "has_portrait": True, "reason": verdict["reason"],
            "character_card": card, **portrait,
        }


# bible_with_provisional_characters / bible_with_pending_characters_for_text 搬到
# .bible_compat（见该文件模块 docstring：与建卡逻辑正交，是安全的搬迁对象）。
# 这里重新导入进本模块命名空间，app/portraits/__init__.py 的
# `from .cards import bible_with_provisional_characters` 等既有导入不受影响。

