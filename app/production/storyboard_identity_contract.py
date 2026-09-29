"""分镜台身份合同：同一规范化结果驱动台词、展示人物和实际选图。"""
from copy import deepcopy
import hashlib
import json
from typing import Any

from app.production.storyboard_pack_identity import resolve_persisted_character_ids
from app.schemas.segment_identity import IDENTITY_CONTRACT_VERSION


#: ``_backfill_portrait_binding`` 告警的固定前缀（含人物），用于幂等去重——
#: 见该函数 2026-09-28 幂等改版注记。
_PORTRAIT_MISSING_MARKER = "[STORYBOARD_PACK_PORTRAIT_MISSING][未拦截] 人物「{identity}」"


def _backfill_portrait_binding(entry: dict, manifest_entry: dict, result: dict, identity: str) -> None:
    """本段草稿没有 portrait_id 时，从全量人物谱补上这个 bible 角色当前生效的定妆照。

    2026-09-28 真实回归（《顾念长安（第二版）》EP1 第 9/15/18 段）：``entries``
    （``payload["asset_manifest"]["characters"]``）是全集范围的人物谱，同一个
    bible 角色全集只有一条记录、一个 portrait_id；本段 ``relevant_assets.
    characters`` 会按映射阶段登记的原文段号收窄过滤（``storyboard_pack.
    _segment_relevant_assets`` 的 ``_hits``），过滤覆盖有缺口时模型看不到这个
    角色的 portrait_id，只能诚实留空——但全量人物谱里这条记录明明存在，不
    该让本段的过滤缺口污染最终产物。这里只读全量人物谱这一份已验证数据，
    不是编造：identity 已经过 ``resolve_persisted_character_ids`` 确认。
    人物谱本身也没有 portrait_id（角色确实还没出图）时，记一条准确的告警，
    不假装补上了一个不存在的值。

    2026-09-28 幂等改版：``canonical_segment_identities`` 在真实流水线里对同一
    segment 至少被调用两到三次（生成期 ``finalize_generated_identity``、落库期
    ``persist_storyboard_pack``、身份工作台 ``prepare_identity_candidate``），
    每次都从上一次的产物（已带着上一轮告警）继续处理；portrait_id 回填分支
    本就有「已经有值就直接返回」的幂等保护，但告警分支此前没有——人物谱本身
    确实没图时，每多算一轮就多一条逐字重复的告警，随编辑次数无上限增长。
    改法与 ``storyboard_cast_lock``/``storyboard_travel_direction`` 里
    「先剥离旧痕迹再写唯一一条」同一形状：写入前先按这个人物的固定前缀剥掉
    已有的同类告警，再写回唯一一条。
    """
    if entry.get("portrait_id"):
        return
    manifest_portrait_id = manifest_entry.get("portrait_id")
    if manifest_portrait_id:
        entry["portrait_id"] = manifest_portrait_id
        return
    if entry.get("visibility") != "visible":
        return
    marker = _PORTRAIT_MISSING_MARKER.format(identity=identity)
    existing = [n for n in result.get("degraded_capabilities") or [] if not str(n).startswith(marker)]
    existing.append(
        f"{marker}本段在场，但本集人物谱里这个角色目前没有已生效的定妆照，正文里这个人物的"
        "长相没有参考图来源，请人工核对是否需要先出图"
    )
    result["degraded_capabilities"] = existing


#: ``_backfill_scene_reference_binding`` 与 ``_backfill_portrait_binding`` 同一防线
#: 的场景侧版本，但绑定关系本身不同：场景绑定纯是确定性 DB 查找结果，没有
#: 需要保留的"作者意图"（不像 portrait_id 那样"模型已经填了就不动"），manifest
#: 这份场景条目在 ``_enrich_asset_manifest_canonical_visuals``（见
#: app.production.storyboard_scene_binding）里已经按当前集号重新解析到「此刻
#: 生效」的行，是唯一权威来源，因此这里无条件覆盖，不是只在缺失时才补。
def _backfill_scene_reference_binding(entry: dict, manifest_entry: dict) -> None:
    """本段草稿这条场景的 ``scene_reference_id`` 统一改写为 manifest 当前值。

    真实回归（proj_ca86b15ab7d7 EP1）：模型这段看到的 ``relevant_assets.scenes``
    是按本段 ``source_segment_indexes`` 过滤过的子集（``storyboard_pack.
    _segment_relevant_assets`` 的 ``_hits``），过滤有缺口时模型看不到这个场景，
    草稿字段可能留空或带着上一轮生成时的旧值；manifest 是全量、已重新解析过
    的权威数据，这里把它写回段落持久化。幂等：manifest 值不变时重复调用结果
    不变。
    """
    entry["scene_reference_id"] = manifest_entry.get("scene_reference_id")


def canonical_segment_identities(segment: dict, payload: dict) -> dict:
    """只按已确认映射正名；不改写语义，不猜未知人物，不产生事务副作用。"""
    result = deepcopy(segment)
    manifest = payload.get("asset_manifest") or {}
    entries = {str(c["identity_id"]): c for c in manifest.get("characters") or []}
    extras = {str(e["visual_entity_id"]): e for e in manifest.get("functional_extras") or [] if e.get("visual_entity_id")}
    scenes = {str(s.get("scene_id") or ""): s for s in manifest.get("scenes") or []}
    for scene_entry in (result.get("resources") or {}).get("scenes") or []:
        manifest_scene = scenes.get(str(scene_entry.get("scene_id") or ""))
        if manifest_scene is not None:
            _backfill_scene_reference_binding(scene_entry, manifest_scene)
    normalized = []
    aliases = {}
    for entry in (result.get("resources") or {}).get("characters") or []:
        raw = str(entry.get("identity_id") or "")
        resolved, notes = resolve_persisted_character_ids(payload, [raw], segment_source_indexes=result.get("source_segment_indexes") or [])
        result.setdefault("degraded_capabilities", []).extend(notes)
        if not resolved:
            continue
        identity = resolved[0]
        aliases[raw] = identity
        entry["identity_id"] = identity
        if identity in entries:
            entry["display_name"] = entries[identity].get("display_name") or identity
            if entry.get("subject_kind") not in {"extra", "crowd"}:
                entry["subject_kind"] = "character"
            _backfill_portrait_binding(entry, entries[identity], result, identity)
        elif identity in extras:
            entry["display_name"] = extras[identity].get("label") or identity
            entry["subject_kind"] = "crowd" if (extras[identity].get("provenance") or {}).get("collective") else "extra"
            # 显式错误的角色卡引用保留给校验器拒绝，不能规范化后悄悄洗掉。
        normalized.append(entry)
    result.setdefault("resources", {})["characters"] = normalized
    for line in result.get("dialogue") or []:
        raw = str(line.get("speaker_identity_id") or "")
        if raw in aliases:
            line["speaker_identity_id"] = aliases[raw]
    return result


def visible_character_ids(segment: dict) -> list[str]:
    """旧记录未知可见性保持旧语义；显式声音专用记录从视觉投影排除。"""
    return list(dict.fromkeys(
        str(c.get("identity_id") or "") for c in (segment.get("resources") or {}).get("characters") or []
        if c.get("visibility") != "voice_only" and c.get("identity_id") != "旁白"
    ))


def registered_subject_errors(segment: dict, payload: dict) -> list[str]:
    """正式角色资格来自当前素材身份清单，模型自报 character 不能借用同名角色卡。"""
    known = {str(c.get("identity_id") or "") for c in (payload.get("asset_manifest") or {}).get("characters") or []}
    errors = []
    for character in (segment.get("resources") or {}).get("characters") or []:
        identity = str(character.get("identity_id") or "")
        kind = character.get("subject_kind")
        if kind == "character" and identity not in known:
            errors.append(f"人物「{identity}」不在已确认角色身份清单中，请核对本段映射或声明为独立群演")
        elif kind in {"extra", "crowd"} and identity in known:
            errors.append(f"群演「{identity}」使用了正式角色身份；若原文指向该角色本人，subject_kind 应为 character；独立群演须使用独立原文身份")
    return errors


def effective_delivery_kind(line: dict) -> str:
    if line.get("delivery_kind"):
        return str(line["delivery_kind"])
    if line.get("speaker_identity_id") == "旁白":
        return "narration"
    return "offscreen_dialogue" if line.get("delivery") == "offscreen_voice" else "spoken_dialogue"


def identity_contract_fingerprint(segment: dict) -> str:
    """最小片段范围指纹：身份/声道/来源变化使旧视频幂等复用失效。"""
    data = {k: segment.get(k) for k in ("resources", "dialogue", "required_dialogue", "prompt_text", "identity_contract_version")}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def identity_contract_errors(segment: dict, *, require_explicit: bool = False) -> list[str]:
    """仅确定性合同冲突阻断提交，旧记录不因缺少新字段被全量作废。"""
    errors = []
    characters = (segment.get("resources") or {}).get("characters") or []
    by_id = {str(c.get("identity_id") or ""): c for c in characters}
    for c in characters:
        identity = str(c.get("identity_id") or "")
        if identity == "旁白":
            errors.append("旁白属于声音清单，请从画面人物资源中移除")
        if require_explicit and c.get("visibility", "unknown") == "unknown":
            errors.append(f"人物「{identity}」需要明确 visibility=visible 或 voice_only")
        if require_explicit and c.get("subject_kind", "unknown") == "unknown":
            errors.append(f"人物「{identity}」需要明确为已确认角色、独立群演或人群")
        if c.get("subject_kind") in {"extra", "crowd"} and (c.get("portrait_id") or identity.startswith("bible:")):
            errors.append(f"独立群演「{identity}」不能绑定正式角色卡，请核对其原文身份")
    for line in segment.get("dialogue") or []:
        errors.extend(_utterance_errors(line, by_id, require_explicit))
    if len(by_id) != len(characters):
        errors.append("同一主体身份在 resources.characters 重复，请合并为一条主体记录")
    errors.extend(str(n) for n in segment.get("degraded_capabilities") or [] if "[STORYBOARD_IDENTITY_AMBIGUOUS]" in n)
    return [f"[STORYBOARD_IDENTITY_CONFLICT] {e}" for e in errors]


def _utterance_errors(line: dict, by_id: dict, require_explicit: bool) -> list[str]:
    speaker = str(line.get("speaker_identity_id") or "")
    kind = effective_delivery_kind(line)
    subject = by_id.get(speaker)
    errors = []
    if not speaker:
        errors.append("台词说话人尚未确定，请保留原文称谓并补充归属证据")
    if (speaker == "旁白") != (kind == "narration"):
        errors.append(f"「{speaker}」的发声身份与 delivery_kind={kind} 不一致")
    if line.get("delivery_kind") and (kind == "spoken_dialogue") != (line.get("delivery", "spoken_dialogue") == "spoken_dialogue"):
        errors.append(f"「{speaker}」的 delivery 与 delivery_kind 不一致")
    if kind == "spoken_dialogue" and subject and subject.get("visibility") == "voice_only":
        errors.append(f"「{speaker}」声明为画内对白，但主体仅有声音")
    if require_explicit and speaker != "旁白" and subject is None:
        errors.append(f"说话人「{speaker}」须声明独立主体及其可见性")
    if require_explicit and not line.get("delivery_kind"):
        errors.append(f"「{speaker}」需要区分画内对白、人物画外对白、内心独白和旁白")
    return errors


def stamp_identity_contract(segment: dict[str, Any]) -> dict[str, Any]:
    """审计可复核的同源投影；不额外生成第二份人名权威。"""
    segment["identity_contract_version"] = IDENTITY_CONTRACT_VERSION
    segment["identity_contract_fingerprint"] = identity_contract_fingerprint(segment)
    return segment
