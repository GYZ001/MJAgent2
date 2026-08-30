"""角色候选发现的历史（legacy）主流程：整合证据目录、发起结构化调用、
记录 visual entity 合并，是 discover_character_candidates 的前置实现。
"""

from __future__ import annotations

import json
import sqlite3

from app.evidence import repository as evidence_repository
from app import hiagent
from app.character_policy import resolution_declares_functional_identity
from app.db import get_conn, new_id, now
from app.errors import ContentGenerationError
from app.harness import model_gateway
from app.identity_authority import identity_authority_registry, identity_resolution_is_authoritative
from app.schemas import Bible, extract_json

from .constants import (
    CAST_DISCOVERY_SOURCE_BUDGET,
    CURRENT_IDENTITY_DECISION_VERSION,
    CURRENT_IDENTITY_SYNTHETIC_PROVENANCE,
    IDENTITY_DISCOVERY_CONTRACT_VERSION,
    IDENTITY_NAME_FORM_RULE,
    IDENTITY_REQUEST_MAX_TOKENS,
)
from .discovery_fragments import (
    _aligned_identity_source_label,
    _future_identity_context,
)
from .discovery_resample import (
    _identity_operation_retry_epoch,
    _identity_structured_with_resample,
)
from .evidence_catalog import (
    _current_identity_evidence_batches,
    _current_identity_known_decision_catalog,
    _current_identity_prior_decision_catalog,
)
from .evidence_merge import (
    _current_identity_durable_signature,
    _current_identity_is_schema_violation,
    _merge_current_identity_occurrences,
    _normalize_current_identity_payload,
)
from .identity_response_projection import _project_current_identity_response
from .identity_schemas import (
    CurrentIdentityCandidateResponse,
    _current_identity_schema,
    _identity_strict_response_format,
)

def _current_identity_projection_errors(candidates: list[dict]) -> list[str]:
    """Reject cross-batch projection conflicts instead of last/first wins.

    真实第20轮 EP4 回归 ERR-20260824-407c9b：这是 _project_current_identity_
    response（单批内按 (source_label, scope_qualifier) 复合键判定唯一性，见
    该函数的 by_label 分组注释）**之外**、独立的一道**跨批**一致性检查——
    长章节按证据切成多批分别调用模型，同一 source_label 若在不同批次里被
    判成不同 identity_group，说明模型自相矛盾，必须拒绝而不是"后者覆盖前者"
    静默吞掉。ERR-20260824-b16bb4（"师弟"关系称谓消歧）修复时只升级了
    单批内的判定键，这道跨批检查仍按裸 source_label 分组——上游单批内
    已经合法放行的"两个外宗弟子"（不同 scope_qualifier、不同 identity_
    group）跨批一比对，照样被这里当成"同一 source_label 冲突"重新拦下。
    键同样升级为 (source_label, scope_qualifier) 复合键，跟单批内判定用
    同一把尺子；没有 scope_qualifier（空串）的称谓不受影响，继续按裸
    label 生效。
    """
    by_label: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for item in candidates:
        label = str(item.get("source_label") or "").strip()
        qualifier = str(item.get("scope_qualifier") or "").strip()
        by_label.setdefault((label, qualifier), set()).add((
            str(item.get("identity_group") or "").strip(),
            str(item.get("identity_kind") or "").strip(),
            str(item.get("name") or "").strip(),
        ))
    return [
        f"current 投影后同一 source_label 冲突：{label}"
        for (label, _qualifier), signatures in by_label.items()
        if not label or len(signatures) != 1
    ]


def _record_visual_entity_merge(
    conn,
    project_id: str,
    *,
    from_visual_entity_id: str,
    to_visual_entity_id: str,
    canonical_name: str,
    evidence_episode_no: int,
    merge_rule: str = "same_batch_k_absorption",
) -> bool:
    """落一条视觉实体折叠记账（``visual_entity_merges``，设计文档 §4.2）。

    ``visual_entity_merges`` 表由并行改动在 ``app/db.py`` 落地迁移（本文件
    不实现该迁移）；表尚未存在时静默跳过——折叠本身（K 决议的命名侧结果）
    已经生效，记账是可补齐的审计侧信息，不构成折叠是否成立的前置条件，
    避免让本文件对尚未合并的迁移产生硬依赖。选图沿用设计文档 §4.2 的规则：
    优先复用 ``to`` 侧（规范权威）已有的 ready 定妆照。
    """
    if not project_id or not from_visual_entity_id or not to_visual_entity_id:
        return False
    if from_visual_entity_id == to_visual_entity_id:
        return False
    selected_portrait_id = None
    if canonical_name:
        try:
            row = conn.execute(
                "SELECT id FROM character_portraits WHERE project_id=? "
                "AND character_name=? AND pack_status='ready' "
                "ORDER BY ep_start DESC LIMIT 1",
                (project_id, canonical_name),
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            selected_portrait_id = row["id"]
    try:
        conn.execute(
            "INSERT INTO visual_entity_merges (id, project_id, "
            "from_visual_entity_id, to_visual_entity_id, canonical_name, "
            "merge_rule, selected_portrait_id, evidence_episode_no, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                new_id("visual_merge"),
                project_id,
                from_visual_entity_id,
                to_visual_entity_id,
                canonical_name,
                merge_rule,
                selected_portrait_id,
                evidence_episode_no,
                now(),
            ),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return False
    return True


def _record_current_identity_absorbed_visual_merges(
    project_id: str | None,
    episode_no: int,
    candidates: list[dict],
) -> None:
    """遍历一批已通过校验的 current-identity 候选，落地同批折叠的视觉记账。

    折叠的核验与纯计算已经在 ``_project_current_identity_response`` 内完成
    （见该函数 k 循环里 ``_current_identity_absorbed_visual_merges`` 的写入）
    ——这里只做落库这一步的副作用，且只在整批（含跨批一致性检查）都通过后
    才调用（见 ``_discover_character_candidates_legacy`` 的调用点），避免为
    一个最终会被拒绝重试的响应写入记账。``project_id`` 缺失时（历史调用点
    尚未传入，见 ``discover_character_candidates`` 的可选形参）静默跳过——
    折叠的命名侧结果不受影响，只是暂不记账。
    """
    if not project_id:
        return
    conn = get_conn()
    for item in candidates:
        merges = item.get("_current_identity_absorbed_visual_merges") or []
        for merge in merges:
            _record_visual_entity_merge(
                conn,
                project_id,
                from_visual_entity_id=str(
                    merge.get("from_visual_entity_id") or ""
                ),
                to_visual_entity_id=str(
                    merge.get("to_visual_entity_id") or ""
                ),
                canonical_name=str(merge.get("canonical_name") or ""),
                evidence_episode_no=episode_no,
                merge_rule=str(
                    merge.get("merge_rule") or "same_batch_k_absorption"
                ),
            )


async def _discover_character_candidates_legacy(
    source_text: str,
    bible: Bible,
    episode_no: int,
    *,
    draft_text: str = "",
    future_text: str = "",
    future_label: str = "",
    existing_resolutions: list[dict] | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Resolve the current episode's cast before/after screenplay generation.

    后续章节只能用来把当前章节的“大汉/老者/黑衣人”解析成稳定真名，
    不得把尚未出场的人物或剧情带回本集。身份模型确认的稳定真名必须完成
    最小人物卡；未确认真名的一次性人物保留来源称谓并签发 typed functional identity。
    """
    known_names = [c.name for c in bible.characters if c.name]
    known = "、".join(known_names) or "（无）"
    existing_functional_routes = {
        str(item.get("canonical_name") or "").strip()
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("canonical_name") or "").strip()
        )
    }
    existing_resolution_projection = [
        {
            "source_label": str(item.get("source_label") or "").strip(),
            "canonical_name": str(item.get("canonical_name") or "").strip(),
        }
        for item in (existing_resolutions or [])
        if (
            isinstance(item, dict)
            and identity_resolution_is_authoritative(item)
            and resolution_declares_functional_identity(item)
            and str(item.get("source_label") or "").strip()
            and str(item.get("canonical_name") or "").strip()
        )
    ]
    # 同批折叠通道（absorbed_functional_keys）第三类可吸收来源——"本集已有
    # 功能身份决议"——的 canonical_name -> source_label 反查表：
    # existing_functional_routes（下方）只是扁平 canonical_name 集合，供
    # 成员关系核验；这里额外保留 source_label，供折叠命中后计算被吸收组的
    # visual_entity_id（见 _project_current_identity_response 的 k 循环）。
    existing_functional_route_labels = {
        item["canonical_name"]: item["source_label"]
        for item in existing_resolution_projection
        if item["canonical_name"] and item["source_label"]
    }
    current_authorities = identity_authority_registry(
        bible,
        existing_resolutions or [],
    )
    reserved_authority_labels = {
        str(label).strip()
        for authority in current_authorities
        if str(authority.get("identity_kind") or "").strip() != "functional"
        for label in (
            authority.get("canonical_name"),
            *(authority.get("source_labels") or []),
        )
        if str(label or "").strip()
    }
    current_haystack = f"{source_text or ''}\n{draft_text or ''}"
    current_evidence_batches = _current_identity_evidence_batches(
        source_text,
        draft_text=draft_text,
    )
    seen: set[tuple[str, str, str]] = set()
    candidates: list[dict] = []
    current_provider, current_model, current_effective_max = (
        hiagent.text_request_token_limits(
            requested_max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
        )
    )
    current_semantic_settings = hiagent.text_request_semantic_settings(
        current_provider
    )

    def collect(
        raw: str,
        *,
        identity_haystack: str,
        group_scope: str,
    ) -> None:
        try:
            obj = extract_json(raw, repair_unescaped_inner_quotes=True)
        except ValueError as exc:
            raise ContentGenerationError(
                "人物身份模型返回了不可验证的非结构化结果，当前阶段已停止"
            ) from exc
        for item in obj.get("characters") or []:
            if not isinstance(item, dict):
                continue
            # 兼容旧模型形状 {name, kind, evidence}。新协议的身份判断完全以模型输出为准。
            legacy_name = str(item.get("name") or "").strip()
            model_source_label = str(
                item.get("source_label") or legacy_name
            ).strip()
            source_label = _aligned_identity_source_label(
                model_source_label,
                current_haystack,
            )
            identity_kind = str(item.get("identity_kind") or "named").strip().lower()
            canonical_name = str(item.get("canonical_name") or legacy_name).strip()
            if identity_kind not in {"named", "functional"}:
                continue
            future_evidence = str(
                item.get("future_evidence") or ""
            ).strip()
            if (
                group_scope in {"future", "coverage"}
                and canonical_name
                and future_evidence
                and canonical_name in known_names
            ):
                # Only an existing canonical Bible identity can repair provider
                # enum drift. Repeating a relation/description in source text
                # proves label presence, not a stable named identity.
                identity_kind = "named"
            elif identity_kind == "functional":
                canonical_name = ""
            dedupe_key = (source_label, canonical_name, identity_kind)
            if (
                not source_label
                or len(source_label) > 16
                or dedupe_key in seen
                or (
                    identity_kind == "named"
                    and (
                        not canonical_name
                        or len(canonical_name) > 16
                        or (
                            canonical_name not in identity_haystack
                            and canonical_name not in known_names
                        )
                    )
                )
            ):
                continue
            seen.add(dedupe_key)
            functional_identity_key = str(
                item.get("functional_identity_key") or ""
            ).strip()[:64]
            existing_route_name = (
                functional_identity_key
                if functional_identity_key in existing_functional_routes
                else ""
            )
            prior_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if (
                    str(candidate.get("source_label") or "").strip()
                    == source_label
                    and str(candidate.get("identity_group") or "").strip()
                )
            }
            declared_group = str(
                item.get("identity_group")
                or functional_identity_key
                or ""
            ).strip()
            existing_groups = {
                str(candidate.get("identity_group") or "").strip()
                for candidate in candidates
                if str(candidate.get("identity_group") or "").strip()
            }
            declared_matches = {
                group
                for group in existing_groups
                if (
                    declared_group
                    and (
                        group == declared_group
                        or group.endswith(f":{declared_group}")
                    )
                )
            }
            identity_group = (
                next(iter(prior_groups))
                if group_scope in {"future", "coverage"} and len(prior_groups) == 1
                else (
                    next(iter(declared_matches))
                    if (
                        group_scope in {"future", "coverage"}
                        and len(declared_matches) == 1
                    )
                    else (
                        f"existing:{existing_route_name}"
                        if existing_route_name
                        else f"{group_scope}:{declared_group or source_label}"
                    )
                )
            )
            candidates.append({
                "name": canonical_name or source_label,
                "source_label": source_label,
                "identity_kind": identity_kind,
                "identity_group": identity_group,
                "existing_route_name": existing_route_name,
                "kind": "mentioned" if item.get("kind") == "mentioned" else "onscreen",
                "evidence": str(item.get("evidence") or "").strip()[:80],
                "future_evidence": future_evidence[:120],
                "source_segment_id": str(
                    item.get("source_segment_id") or ""
                ).strip(),
                "source_quote": str(item.get("source_quote") or "").strip()[:240],
                "model_source_label": (
                    model_source_label
                    if model_source_label != source_label else ""
                ),
            })

    for current_batch, evidence_records in enumerate(
        current_evidence_batches, start=1
    ):
        evidence_by_ref = {
            f"E{index:03d}": record
            for index, record in enumerate(evidence_records, start=1)
        }
        evidence_catalog = [
            {
                "evidence_ref": evidence_ref,
                "origin": str(record.get("origin") or ""),
                "source_segment_id": str(
                    record.get("source_segment_id") or ""
                ),
                "path": str(record.get("path") or ""),
                "text": str(record.get("text") or ""),
            }
            for evidence_ref, record in evidence_by_ref.items()
        ]
        registered_decisions = _current_identity_known_decision_catalog(
            evidence_by_ref,
            authorities=current_authorities,
        )
        prior_named_decisions, prior_functional_groups = (
            _current_identity_prior_decision_catalog(
                evidence_by_ref,
                prior_candidates=candidates,
            )
        )
        known_decisions = {
            **registered_decisions,
            **prior_named_decisions,
        }
        known_decision_projection = [
            {
                "decision_id": decision_id,
                "decision_type": str(item.get("decision_type") or ""),
                "evidence_ref": str(item.get("evidence_ref") or ""),
                "source_label": str(item.get("source_label") or ""),
                "canonical_name": str(item.get("canonical_name") or ""),
                "allowed_kinds": (
                    ["onscreen", "mentioned"]
                    if item.get("materialization_compatible")
                    else ["mentioned"]
                ),
            }
            for decision_id, item in sorted(known_decisions.items())
        ]
        prior_functional_projection = [
            {
                "decision_id": decision_id,
                "source_labels": list(item.get("source_labels") or []),
                "existing_route_name": str(
                    item.get("existing_route_name") or ""
                ),
            }
            for decision_id, item in sorted(prior_functional_groups.items())
        ]
        prior_decision_catalog_hash = evidence_repository.content_hash({
            "named": [
                item for item in known_decision_projection
                if str(item.get("decision_type") or "").startswith("prior_")
            ],
            "functional": prior_functional_projection,
        })
        evidence_catalog_hash = evidence_repository.content_hash({
            "contract_version": CURRENT_IDENTITY_DECISION_VERSION,
            "evidence": evidence_records,
            "known_decisions": known_decision_projection,
            "prior_functional_groups": prior_functional_projection,
        })
        current_schema = _current_identity_schema(
            list(evidence_by_ref),
            known_decision_ids=list(known_decisions),
        )
        current_response_format = _identity_strict_response_format(
            current_schema,
            name="screenplay_current_identity_discovery_v11",
        )
        prompt = f"""任务：为第 {episode_no} 集做人物身份增量预检。请用语义和上下文判断，
不要依赖服饰、性别、年龄或称谓后缀的固定词表。

当前人物谱已有角色：
{known}

前批已确定的 functional 分组 P 决议（后批判断为同一人时，functional_identity_key 必须精确复用 decision_id）：
{json.dumps(prior_functional_projection, ensure_ascii=False, separators=(',', ':'))}

本批 backend-owned 当前身份证据目录。E ref 已绑定完整证据 receipt，禁止跨 E 搬运人物：
{json.dumps(evidence_catalog, ensure_ascii=False, separators=(',', ':'))}

本批已登记身份 K 决议目录（只有这些 decision_id 可进入 k；目录为空则所有 k=[]）：
{json.dumps(known_decision_projection, ensure_ascii=False, separators=(',', ':'))}

本集已有功能身份决议（可为空；canonical_name 是已分配的本集稳定 ID）：
{json.dumps(existing_resolution_projection, ensure_ascii=False, separators=(',', ':'))}

规则：
1. root 只输出一次 k/n/f 三个全局数组，不得输出 decisions，也无需覆盖没有人物的 E。
   每个身份/称谓只输出一次，从它的 owned 证据中选最清晰的 E；同一 E 可支持多人。
2. 已登记身份只可选 k：decision_id 精确复制 K 目录，该 token 已绑定 E；
   kind 必须属于该 K 的 allowed_kinds；
   不得把 K 目录中的 source_label 写进 n/f。只允许 mentioned 的 K 没有可安全物化的最终人物卡
   authority；若人物实际出镜则必须停止而不能谎报 mentioned 或另造身份。
   本批 K 目录没有为「当前人物谱已有角色」中的某人签发 decision_id，说明本批证据没有
   逐字锚定他：此时既不得把他的真名写进 n，也不得据上下文推断，只能把你实际读到的
   逐字称谓按第 4 条放入 f，交给后续带 authority_id 的权威绑定去认领。人物谱名单只用于
   识别，不是可以直接书写的名字。
3. 当前阶段的新 named 只用于逐字自称谓：n 每项写 evidence_ref、identity_label、name_kind 与 kind，
   identity_label 必须是所选 E text 的连续逐字子串；后端会令 canonical_name=source_label。
   {IDENTITY_NAME_FORM_RULE}
   name_kind 只描述 identity_label 这个字符串本身的形态，与你是否认得这个人无关；
   尊称或代称请照实写 honorific/referential，后端会自动把它落为功能身份。
   任何“称谓 A 其实是名字 B”的别名判断，即使 A、B 同时出现在当前输入，也必须先判为
   functional，交由后续带 authority_id 的权威绑定；不得用同场共现代替同一性证据。
4. 若是一次性角色，别名待后续确认，或无法确认稳定真名，放入 f；每项填写
   evidence_ref、source_label、functional_identity_key、kind，不得携带 canonical/authority/evidence。
   source_label 尽量逐字复用所选 E text；若为区分同段多个无名实体而
   必须使用非逐字的稳定描述，只能保留为 functional，后端会隔离为 synthetic identity，
   不得将它当作别名或真名。
   若同一实体在证据里有多个逐字可用的称呼（如「凶兽」与「一只约莫一人大小，
   样子如猴般的凶兽」），必须选其中最短的那个稳定称谓：source_label 只是一个
   可复用的身份标签，不是用来证明你读到了完整描述。
   source_label 不得包含 、，,／/；;｜|＆&＋+ 等分隔符标点或空白：后端会按这些
   字符切分身份列表（如台词发言人、场次角色表），混入分隔符会让一个人被错误
   切成多段身份。
5. 若身份投影中的 source_label 混入动作或表演提示，必须结合对应 line_context 判断真正说话人；
   source_label 保留原始完整字符串，canonical_name/functional_identity_key 绑定到真正说话人。
   禁止按“说、喊、点头”等固定词表或后缀规则猜测。
6. 每个 f 项必须填写 functional_identity_key：
   - 若它与“前批已确定的 functional 分组”是同一人，必须精确复制该 P decision_id；
     不得重新使用前批原始分组字符串。
   - 若它与“本集已有功能身份决议”中的某人是同一人，精确填写该人的 canonical_name。
   - 否则填写本次响应内的不透明分组 ID（如 F1、F2）；不同 source_label 若明确是同一人必须共用同一 ID。
   - 无法确认是否同一人时必须使用不同 ID，禁止根据称谓字面相似猜测。
7. 每个人只输出一次；不得因多次出现重复输出，不得因共用证据合并人物，
   也不得把同一 source_label 放入多个分支。后端只会聚合语义签名完全相同的合法重复，
   任何跨分支、跨分组或非逐字 synthetic 重复都会硬失败。
8. f 每项还需要填写 scope_qualifier（默认可留空字符串）：如果同一个 source_label
   在本批不止一次出现、且这几次实际指的不是同一个人——比如「师弟」「师兄」「道友」
   「前辈」这类相对说话人或语境而定的关系称谓/身份指代，本就可能在同一批里对应
   不同人——必须给每一次单独填一句简短、能从对应证据里直接读出依据的限定语，说明
   这次具体是哪一个人（如取自证据的动作、对话对象或所在场景），确保同一 source_label
   下不同的人各自的 scope_qualifier 互不相同。如果这几次确实是同一个人反复出现，
   或这个称谓本就唯一指向一个人，scope_qualifier 留空即可。判断依据是"这次读到的
   是不是同一个人"，不是称谓字面是什么词；拿不准时倾向于填写限定语而不是留空，
   避免把两个不同的人误合并成一个人。同一 source_label 下用不同
   functional_identity_key 申报了多个人时，必须各自填写能互相区分的
   scope_qualifier——不要依赖后端的确定性降级补足（后端会用甲/乙/丙...
   兜底填一个可用但没有语义信息量的限定语，只是防止拒绝重来，不是让你
   可以不填）。
9. absorbed_functional_keys 的合法取值域只有三类，逐项必须精确复制其中
   之一——本批 f 项自己声明过的 functional_identity_key、前批 P token
   （prior functional 分组的 decision_id）、或本集已有功能身份决议的
   canonical_name；不是任意你认为"指代同一人"的称谓原文。后端只核验每个
   token 是否确实来自这三类来源，不做文本语义判断，越界或臆造的 token
   （包括任何未按上述三类之一先行声明过的称谓原文）都会导致本次响应被
   拒绝重试。

   这三类来源有一个共同前提：token 背后的实体在被吸收前必须处于"稳定真名
   尚未确认"的功能性占位状态——这正是规则4"若…无法确认稳定真名，放入 f"
   的适用范围。一个人只要已经有确定真名（不论是这条 k 决议刚揭晓的，还是
   人物谱/更早证据里早已确认的），TA 的其它称谓从一开始就不满足"功能性
   占位"这个前提，永远不构成合法的 f 项，也就永远不会出现在上述三类合法
   来源里——不得为了让某个称谓能被吸收，倒着现造一条 f 项把它包装成功能性
   占位；f 项存在的理由是"真名未定"，不是"我想吸收它"。这类已有确定真名
   之人的其它称谓，走称谓解析的正常渠道（n 的逐字自称谓声明、或人物谱别名
   登记），不进 absorbed_functional_keys。（真实事故：「孟才子」「孟兄」是
   孟浩的称谓、「王伯的儿子」是王有材的称谓、「许师姐」是许清的称谓——这
   四人都已有确定真名，从一开始就不是合法的 f 项，任何 k 决议都不得把这类
   称谓原文填入 absorbed_functional_keys。）

   合法用例：如果某个 k 决议揭晓的真名，其实就是一个仍处于 functional 状态
   的称谓组一路指代的同一个人——例如某绰号从更早的证据起就被追踪为
   functional，直到这条 k 决议对应的证据才第一次读到该人物的真名——不要
   把真名重复写进 n（那是这条 k 决议已经覆盖的重复声明，会被拒绝）：改为
   在这条 k 决议里填写 absorbed_functional_keys，逐项精确复制被吸收的
   functional_identity_key/P token/canonical_name。只有在你确实判断这些
   token 指代的是同一个人时才填写；拿不准是否为同一人时留空，不要吸收。

   absorbed_functional_keys 里禁止填入这条 k 决议自己的 source_label（即
   本批 K 决议目录里这个 decision_id 条目自带的 source_label 原文）：选中
   decision_id 本身已经表达了这个称谓属于该决议，重复列出会被判定为越界
   token 而拒绝，不是多填了一道保险。absorbed_functional_keys 只能用来
   吸收这个自身称谓之外的、真正处于功能性占位状态的其它称谓组（不是任何
   已有确定真名之人的称谓，见本条前半段）——如果某个这样的称谓只是你在
   证据里零散认出、还没有单独作为一条 f 项列出（source_label 与 functional_
   identity_key 均已确定），它就还不是合法的可吸收 token：必须先在本响应
   的 f 数组里为它单独声明一条 f 项（source_label 填该称谓本身，
   functional_identity_key 可以直接使用你打算吸收的同一个 key），再在
   absorbed_functional_keys 里精确复制那个 key。第7条"每个人只输出一次"
   约束的是同一个人不得被同时判给两个互相冲突的最终身份归属，不禁止你为
   将被吸收的称谓单独声明它自己的 f 项——被吸收的 f 项与吸收它的 k 决议
   共存，就是这条通道设计的正常形态。
只输出 response_format 约束的 JSON，不要复述证据、Schema 或规则。"""

        def validate_current_response(
            value: CurrentIdentityCandidateResponse,
        ) -> list[str]:
            _projected, errors = _project_current_identity_response(
                value,
                evidence_by_ref=evidence_by_ref,
                known_decisions=known_decisions,
                prior_functional_groups=prior_functional_groups,
                reserved_authority_labels=reserved_authority_labels,
                group_scope=f"current-{current_batch}",
                existing_functional_routes=existing_functional_routes,
                existing_functional_route_labels=existing_functional_route_labels,
            )
            # chat_structured has no format/semantic split on a validate()
            # callback's return value (strict_identity_substage forbids this
            # call from using format_retry_limit/semantic_retry_limit anyway,
            # see app.harness.model_gateway) -- any non-empty return here
            # raises StructuredSemanticError immediately. A response whose
            # *only* faults are wire-schema-declared (enum out of range) must
            # not trip that: filtering them out here lets chat_structured
            # accept the response, and the explicit re-check right after
            # `_identity_structured_with_resample` below raises
            # StructuredFormatError for them instead -- see that call site's
            # comment for the full reasoning. Any genuine semantic fault
            # (source_label 重复 等业务判断分歧) still returns non-empty here
            # and still halts on the first attempt, unchanged.
            return [
                error for error in errors
                if not _current_identity_is_schema_violation(error)
            ]

        response = await _identity_structured_with_resample(
            [{"role": "user", "content": prompt}],
            model_type=CurrentIdentityCandidateResponse,
            validate=validate_current_response,
            normalize_payload=_normalize_current_identity_payload,
            operation_id_for_attempt=lambda resample_attempt: (
                f"screenplay.identity.current.v6:{episode_no}:{current_batch}:"
                + evidence_repository.content_hash({
                    "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                    "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                    "current_evidence_catalog_hash": evidence_catalog_hash,
                    "prior_decision_catalog_hash": prior_decision_catalog_hash,
                    "provider": current_provider,
                    "model": current_model,
                    "requested_max_tokens": 8192,
                    "effective_max_tokens": current_effective_max,
                    "temperature": 0.1,
                    "provider_semantic_settings": current_semantic_settings,
                    "retry_epoch": _identity_operation_retry_epoch(),
                    "resample_attempt": resample_attempt,
                    "prompt": prompt,
                    "schema": current_schema,
                    "response_format": current_response_format,
                })
            ),
            temperature=0.1,
            max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
            format_retry_limit=0,
            semantic_retry_limit=0,
            call_meta={
                "stage": "discover_character_candidates",
                "stage_key": "screenplay_character_discovery",
                "substage": "current_identity",
                "episode_no": episode_no,
                "discovery_phase": "current",
                "source_batch": current_batch,
                "source_batches": len(current_evidence_batches),
                "reuse_successful_operation": False,
                "disable_provider_retries": True,
                "disable_provider_candidate_fallback": True,
                "disable_reasoning_fallback": True,
                "contract_version": IDENTITY_DISCOVERY_CONTRACT_VERSION,
                "current_identity_version": CURRENT_IDENTITY_DECISION_VERSION,
                "current_evidence_catalog_hash": evidence_catalog_hash,
                "current_decision_catalog_hash": evidence_repository.content_hash(
                    known_decision_projection
                ),
                "prior_decision_catalog_hash": prior_decision_catalog_hash,
                "schema_hash": evidence_repository.content_hash(current_schema),
                "provider": current_provider,
                "model": current_model,
                "effective_max_tokens": current_effective_max,
                "provider_semantic_settings": current_semantic_settings,
                "retry_epoch": _identity_operation_retry_epoch(),
            },
            output_schema=current_schema,
            response_format=current_response_format,
            require_response_format=True,
        )
        batch_candidates, projection_errors = (
            _project_current_identity_response(
                response,
                evidence_by_ref=evidence_by_ref,
                known_decisions=known_decisions,
                prior_functional_groups=prior_functional_groups,
                reserved_authority_labels=reserved_authority_labels,
                group_scope=f"current-{current_batch}",
                existing_functional_routes=existing_functional_routes,
                existing_functional_route_labels=existing_functional_route_labels,
            )
        )
        if projection_errors:
            schema_violations = [
                error for error in projection_errors
                if _current_identity_is_schema_violation(error)
            ]
            semantic_violations = [
                error for error in projection_errors
                if not _current_identity_is_schema_violation(error)
            ]
            if semantic_violations:
                # 真正的业务判断分歧（如 source_label 重复），维持既有语义
                # 失败/即停：即便同批还夹带了 wire-schema 越界，也不得靠重
                # 采样蒙混过真信号，两类原文一并报出方便排障。
                raise ContentGenerationError(
                    "；".join(semantic_violations + schema_violations)
                )
            # 走到这里说明本批 projection_errors 只剩 wire-schema 已声明的
            # 越界——validate_current_response 已经把这些从 chat_structured
            # 的 validate() 反馈里过滤掉，所以 chat_structured 把 response 当
            # 成验证通过返回；这是它们第一次真正被拦下。供应商对深层数组
            # enum 的严格模式偶发失效（RCA ERR-20260824-e3628f，约 0.5%
            # 采样缺陷），改判为格式失败：不在本次调用内重采样
            # （strict_identity_substage 强制 format_retry_limit=0），而是把
            # StructuredFormatError 交给 scripts/yyft_serial10.py 的瞬时族
            # 分诊（exc_type=='StructuredFormatError'），本集 60s 后整体
            # 重发一次，不需要人工 RCA。
            raise model_gateway.StructuredFormatError(
                "；".join(schema_violations)
            )
        candidates.extend(batch_candidates)

    if projection_errors := _current_identity_projection_errors(candidates):
        raise ContentGenerationError("；".join(projection_errors))

    _record_current_identity_absorbed_visual_merges(
        project_id, episode_no, candidates
    )

    unresolved_onscreen_groups = {
        str(item.get("identity_group") or "").strip()
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and item.get("kind") == "onscreen"
            and str(item.get("identity_group") or "").strip()
        )
    }
    future_candidates = [
        item
        for item in candidates
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and (
                item.get("kind") == "onscreen"
                or str(item.get("identity_group") or "").strip()
                in unresolved_onscreen_groups
                or str(item.get("source_label") or "").strip()
                in future_text
            )
        )
    ]
    future_context = _future_identity_context(
        future_text,
        [item["source_label"] for item in future_candidates],
        known_names=known_names,
        current_text=source_text,
    )
    if future_context:
        current_identity_audit_source = str(source_text or "").strip()
        if len(current_identity_audit_source) > CAST_DISCOVERY_SOURCE_BUDGET:
            half = CAST_DISCOVERY_SOURCE_BUDGET // 2
            current_identity_audit_source = (
                current_identity_audit_source[:half]
                + "\n……（身份覆盖复核中段省略）……\n"
                + current_identity_audit_source[-half:]
            )
        future_prompt = f"""任务：只为当前集已经发现的人物称谓做后续姓名消歧。

当前人物谱已有角色：
{known}

当前集尚未确认真名的候选：
{json.dumps(future_candidates, ensure_ascii=False, separators=(',', ':'))}

当前集原文（同时复核第一遍是否漏掉独立出场/开口的实体）：
{current_identity_audit_source}

后续章节中命中这些称谓的局部窗口（{future_label or '后续章节'}）：
{future_context}

规则：
1. 优先输出当前集候选中 source_label 完全相同的项目；若第一遍遗漏了当前原文中可区分、
   独立出场或开口的实体，也必须补充输出，source_label 使用当前原文逐字称谓。
   称谓可以在章节边界发生变化；
   若当前集离场状态、后续开场承接和人物谱真名窗口共同形成唯一同一性证据，可据此确认，
   不要求旧称谓与真名必须出现在同一句。
2. canonical_name 必须出现在后续窗口或当前人物谱中；稳定唯一的法号、尊号、专属称号
   也属于 named identity，不要求必须是户籍式真名；有歧义就不输出。
3. 不得新增只在后续章节出场的人，不得复述与身份无关的剧情。

只输出 JSON：
{{"characters": [{{"source_label": "当前称谓", "canonical_name": "稳定真名", "identity_kind": "named", "kind": "onscreen|mentioned", "evidence": "本集身份依据", "future_evidence": "同一性依据"}}]}}"""
        future_raw = await model_gateway.chat(
            [{"role": "user", "content": future_prompt}],
            temperature=0.1,
            max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
            call_meta={
                "stage": "discover_character_candidates",
                "episode_no": episode_no,
                "discovery_phase": "future_identity",
                "reuse_successful_operation": True,
            },
        )
        collect(
            future_raw,
            identity_haystack=f"{current_haystack}\n{future_context}",
            group_scope="future",
        )
        if len(current_identity_audit_source) >= 1000:
            coverage_prompt = f"""任务：独立审计当前集人物身份覆盖，只找第一遍遗漏或错误降级的实体。

当前人物谱已有角色：
{known}

当前集原文：
{current_identity_audit_source}

前两遍候选：
{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}

后续姓名证据：
{future_context}

规则：
1. 逐段核对每个独立行动、开口或具有可区分外观的实体；集合称谓不能替代其中的独立人物。
2. 只输出遗漏实体，或已有候选中能由后续证据唯一升级为 named identity 的实体。
3. source_label 必须逐字来自当前集原文。canonical_name 必须有当前原文、人物谱或后续窗口证据。
4. 不得按职业、年龄、服饰、称号词表判断人物是否重要；无法唯一确认就不输出。

只输出 JSON：
{{"characters": [{{"source_label": "当前原文逐字称谓", "canonical_name": "稳定真名或专属称号", "identity_kind": "named|functional", "identity_group": "若与已有候选同一实体则精确复用其 identity_group，否则空串", "functional_identity_key": "同一实体分组或空串", "kind": "onscreen|mentioned", "evidence": "当前依据", "future_evidence": "后续同一性依据"}}]}}"""
            coverage_raw = await model_gateway.chat(
                [{"role": "user", "content": coverage_prompt}],
                temperature=0.05,
                max_tokens=IDENTITY_REQUEST_MAX_TOKENS,
                call_meta={
                    "stage": "discover_character_candidates",
                    "episode_no": episode_no,
                    "discovery_phase": "coverage_audit",
                    "reuse_successful_operation": True,
                },
            )
            collect(
                coverage_raw,
                identity_haystack=f"{current_haystack}\n{future_context}",
                group_scope="coverage",
            )

    # 同一称谓在不同后文批次中可能先被保守判为 functional，后被真名证据命中。
    # 具名证据唯一时优先；出现两个不同真名时不猜，降级为一次性角色。
    def strongest_occurrence(options: list[dict]) -> dict:
        """Preserve an onscreen owned receipt independent of batch order."""
        signatures = {
            _current_identity_durable_signature(item) for item in options
        }
        if len(signatures) == 1 and not any(
            item.get("source_label_provenance")
            == CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            for item in options
        ) and all(
            isinstance(item.get("source_evidence_receipt"), dict)
            and isinstance(item.get("source_evidence_receipts"), list)
            and bool(item.get("source_evidence_receipts"))
            for item in options
        ):
            return _merge_current_identity_occurrences(options)
        return next(
            (item for item in options if item.get("kind") == "onscreen"),
            options[0],
        )

    resolved: list[dict] = []
    # 第31轮 ERR-20260824-614276 RCA 追加发现：这一步是跨 collect() 批次
    # （current/future/coverage）的最终折叠，一直只按裸 source_label 分组
    # ——round-20/round-31 在 _project_current_identity_response /
    # _current_identity_projection_errors 两处都已经升级成 (source_label,
    # scope_qualifier) 复合键，唯独这里从未跟进：两个通过了前两道复合键
    # 校验的不同人（比如本轮"老者"F3/F4 补足后各自拿到的 甲/乙 限定语），
    # 走到这里又会被裸 source_label 重新拍扁成一条（strongest_occurrence
    # 内部按 durable signature 判等，签名不等就直接"挑一个 onscreen 的"
    # 静默丢弃另一个）——同一个漏洞类型的第三处变体，一并按同一把复合键
    # 尺子修正。
    for group_key in dict.fromkeys(
        (item["source_label"], str(item.get("scope_qualifier") or "").strip())
        for item in candidates
    ):
        source_label, qualifier = group_key
        options = [
            item for item in candidates
            if item["source_label"] == source_label
            and str(item.get("scope_qualifier") or "").strip() == qualifier
        ]
        named_options_by_name: dict[str, list[dict]] = {}
        for item in options:
            if item["identity_kind"] == "named":
                named_options_by_name.setdefault(item["name"], []).append(item)
        named_by_name = {
            name: strongest_occurrence(named_options)
            for name, named_options in named_options_by_name.items()
        }
        if len(named_by_name) == 1:
            resolved.append(next(iter(named_by_name.values())))
        elif len(named_by_name) > 1:
            functional_options = [
                item for item in options
                if item["identity_kind"] == "functional"
            ]
            functional = (
                strongest_occurrence(functional_options)
                if functional_options else None
            )
            resolved.append(functional or {
                "name": source_label,
                "source_label": source_label,
                "identity_kind": "functional",
                "identity_group": f"conflict:{source_label}",
                "existing_route_name": "",
                "kind": "onscreen",
                "evidence": "多批次身份线索冲突，不猜真名",
                "future_evidence": "",
            })
        else:
            resolved.append(strongest_occurrence(options))

    named_by_group: dict[str, set[str]] = {}
    named_evidence: dict[tuple[str, str], dict] = {}
    for item in resolved:
        if item.get("identity_kind") != "named":
            continue
        group = str(item.get("identity_group") or "").strip()
        name = str(item.get("name") or "").strip()
        if group and name:
            named_by_group.setdefault(group, set()).add(name)
            named_evidence[(group, name)] = item
    upgraded: list[dict] = []
    for item in resolved:
        group = str(item.get("identity_group") or "").strip()
        names = named_by_group.get(group, set())
        if (
            item.get("identity_kind") == "functional"
            and item.get("source_label_provenance")
            != CURRENT_IDENTITY_SYNTHETIC_PROVENANCE
            and len(names) == 1
        ):
            canonical_name = next(iter(names))
            evidence = named_evidence[(group, canonical_name)]
            upgraded.append({
                **item,
                "name": canonical_name,
                "identity_kind": "named",
                "future_evidence": str(
                    evidence.get("future_evidence") or ""
                ),
            })
        else:
            upgraded.append(item)
    resolved = upgraded

    # 按本集第一次出现排序，保证后续“路人甲/乙/丙/丁”分配不受模型输出顺序影响。
    return sorted(
        resolved,
        key=lambda item: current_haystack.find(item["source_label"]),
    )

