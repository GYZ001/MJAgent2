"""分镜台/生成台判据统一化（WS8-A）。

背景：分镜台展示的 ``[未拦截]`` 资源类警告（``STORYBOARD_PACK_RESOURCE_
CHARACTER_UNKNOWN``/``STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP`` 等，见
``app.production.storyboard_pack._segment_content_advisories``）与生成台真正
决定"这一镜怎么出片"的判据（``app.multiview.manifest_production_blockers`` /
``app.media_exec.reference_pool_gate``）此前各算各的：前者只说"这个身份不是
映射台已知人物"，从不说清楚这句话对生成台意味着什么。真实事故：三国 ep1 第 1
镜挂着 4 条 ``[未拦截]`` 警告，用户读成"不影响"，送到生成台后连续 5 次全部落
``waiting_human``（候选池判空回退到纯文本的修复是同一晚才提交的）。

本模块把两边判据接到同一个源头：``forecast_shot_production`` 直接调用
``app.multiview.manifest_production_blockers``（不复制它的规则），只负责把
"有没有 blockers" 之外的第二个维度——"就算没有 blockers，这一镜到底有没有真实
参考图可用"——也算清楚，输出三态供两边的文案层消费：

- ``WILL_BLOCK``：有真实缺口（人物/场景本该有定妆照/场景图却没有，或包状态未
  ready）。生成台两次生成尝试后会判定为真实故障，落 ``waiting_human``。
- ``TEXT_ONLY_FALLBACK``：没有缺口，但这一镜也没有任何"本该有资产"的人物/场景
  ——候选池天生是空的（群演、一次性人物、这段原文没有登记场景等）。生成台会
  在两次尝试后自动回退成纯文本出片，外观完全交给 prompt_text 的文字描述。
- ``OK``：至少有一个人物或场景本该有资产，且 blockers 为空——生成台预期能拿到
  真实参考图。

``resource_advisories_for_segment`` 在此基础上产出面向用户的告警文案，是
``storyboard_core.storyboard_pack_resource_advisories`` 与
``app.media_exec.reference_pool_gate._reference_repair_guidance`` 共用的唯一
文案来源（CLAUDE.md「模型契约两侧必须对齐」的姊妹要求：两处提示语也不许各说
各话）。

层号：本模块随 ``app.validators`` 包前缀归 L4（app/LAYERS.toml），只依赖同层的
``app.multiview``/``app.portraits``（均显式声明为 L4），不依赖任何 L5 编排/
领域模块——``app.media_exec.reference_pool_gate``（L5）向下依赖本模块是合法方向。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


WILL_BLOCK = "will_block"
TEXT_ONLY_FALLBACK = "text_only_fallback"
OK = "ok"


@dataclass(frozen=True, slots=True)
class ShotProductionForecast:
    """一镜的生成台预测：verdict 三选一，blockers 只在 WILL_BLOCK 时非空。"""

    verdict: str
    blockers: list[str] = field(default_factory=list)


def _manifest_scene_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    scenes = [manifest.get("scene"), *(manifest.get("additional_scenes") or [])]
    return [s for s in scenes if isinstance(s, dict) and s.get("name")]


def forecast_shot_production(manifest: dict[str, Any] | None) -> ShotProductionForecast:
    """输入：``app.multiview.resolve_shot_asset_dependencies`` 已经解析好的
    单镜资产依赖 manifest（调用方现查，本函数不做任何 I/O，纯函数）。

    判据只有两层，都不重新发明：第一层直接是 ``manifest_production_blockers``
    本身——生成台唯一权威判据；第二层只回答"如果没有 blockers，是不是因为这一
    镜压根没有需要资产的人物/场景"，用来区分 OK 与 TEXT_ONLY_FALLBACK，对应
    ``app.media_exec.reference_pool_gate`` 里"候选池两次尝试后为空即回退纯
    文本"的真实运行时行为（该模块判定"候选池是否本该有东西"用的也是
    ``manifest_production_blockers``，见其 ``_reference_pool_blockers``）。
    """
    # 延迟导入，不能放模块级：app.multiview 模块级 `from app.validators import match_scene_name`
    # 会触发 validators 门面 __init__ → 本模块 → 再 import app.multiview（此时它只初始化了一半），
    # 任何先 import app.multiview 的进程（单跑 tests/test_storyboard_gate_consistency.py、脚本）
    # 都会 ImportError；全量测试只是碰巧导入顺序不同才没红。tests/test_import_cycle_multiview_validators.py 守着。
    from app.multiview import manifest_production_blockers

    blockers = manifest_production_blockers(manifest)
    if blockers:
        return ShotProductionForecast(verdict=WILL_BLOCK, blockers=blockers)
    manifest = manifest if isinstance(manifest, dict) else {}
    has_required_character = any(
        ch.get("asset_required", True) for ch in (manifest.get("characters") or [])
    )
    has_required_scene = any(
        scene.get("asset_required", True) for scene in _manifest_scene_entries(manifest)
    )
    if has_required_character or has_required_scene:
        return ShotProductionForecast(verdict=OK)
    return ShotProductionForecast(verdict=TEXT_ONLY_FALLBACK)


def blocked_consequence_text(blockers: list[str]) -> str:
    """WILL_BLOCK 时的共用文案：拦住用户必须给出路（CLAUDE.md「User-Facing
    Behavior」），不许只说"缺口"不说去哪补。分镜台的资源警告与生成台的修复
    引导（``reference_pool_gate._reference_repair_guidance``）共用这一份，
    不许两套话。"""
    return (
        "生成台会拦下本镜：" + "；".join(blockers)
        + "。请到「人物谱」或「场景库」补齐后重试。"
    )


def text_only_consequence_text(display_name: str, *, kind: str = "人物") -> str:
    """TEXT_ONLY_FALLBACK 时的共用文案：明确说清后果（外观只由文字负责）与
    去哪补救，不让用户误以为"没拦截=没事"。"""
    register_hint = (
        f"把「{display_name}」登记为角色" if kind == "人物" else f"为「{display_name}」建场景卡"
    )
    library = "人物谱" if kind == "人物" else "场景库"
    return (
        f"本镜无可绑定的{kind}参考图：生成台将按纯文本出片"
        f"（外观只由分镜文字决定），若想用定妆照/场景图，请到"
        f"「{library}」{register_hint}"
    )


def _display_name(identity_or_scene_id: str) -> str:
    return str(identity_or_scene_id).split(":", 1)[-1] if identity_or_scene_id else str(identity_or_scene_id)


def _manifest_character_has_asset(manifest: dict[str, Any], display_name: str) -> bool:
    for entry in manifest.get("characters") or []:
        if entry.get("name") == display_name:
            return bool(entry.get("selected_view_ids") or entry.get("selected_views"))
    return False


def _manifest_scene_has_asset(manifest: dict[str, Any], display_name: str) -> bool:
    for entry in _manifest_scene_entries(manifest):
        if entry.get("name") == display_name:
            return bool(entry.get("selected_view_ids") or entry.get("selected_views"))
    return False


def _blocker_for(blockers: list[str], display_name: str) -> str | None:
    return next((b for b in blockers if f"「{display_name}」" in b), None)


def _advisory_line(
    *, code_suffix: str, kind: str, name: str, forecast: ShotProductionForecast,
) -> str:
    if forecast.verdict == WILL_BLOCK:
        blocker = _blocker_for(forecast.blockers, name)
        return (
            f"[STORYBOARD_PACK_RESOURCE_{code_suffix}_BLOCKED][拦截] "
            + (
                f"本镜会被生成台拦下：{blocker}"
                if blocker
                else blocked_consequence_text(forecast.blockers)
            )
        )
    return (
        f"[STORYBOARD_PACK_RESOURCE_{code_suffix}_UNKNOWN][未拦截] "
        + text_only_consequence_text(name, kind=kind)
    )


def resource_advisories_for_segment(
    *, resources: dict[str, Any], manifest: dict[str, Any] | None,
) -> list[str]:
    """本镜的资源类 ``[未拦截]``/``[拦截]`` 告警，标签与文案都取自
    ``forecast_shot_production`` 的判定——不允许分镜台自说自话。

    ``resources`` 是分镜台 2.0.0 段落自己声明的 ``resources.characters``/
    ``resources.scenes``（``identity_id``/``scene_id`` + 展示名），只用来
    枚举"这一镜提到了哪些人物/场景"；``manifest`` 是调用方已经用
    ``app.multiview.resolve_shot_asset_dependencies`` 解析好的生产台真实依赖，
    本函数不在这里现查库。``manifest`` 为空时仍按"没有任何资产"处理（走
    forecast 的 TEXT_ONLY_FALLBACK/WILL_BLOCK 分支，取决于 blockers 是否为
    空），不静默跳过——空信息不等于无需检查。
    """
    forecast = forecast_shot_production(manifest)
    manifest = manifest if isinstance(manifest, dict) else {}
    advisories: list[str] = []
    for entry in resources.get("characters") or []:
        name = _display_name(str(entry.get("identity_id") or ""))
        if _manifest_character_has_asset(manifest, name):
            continue
        advisories.append(_advisory_line(
            code_suffix="CHARACTER", kind="人物", name=name, forecast=forecast,
        ))
    for entry in resources.get("scenes") or []:
        name = _display_name(str(entry.get("scene_id") or ""))
        if _manifest_scene_has_asset(manifest, name):
            continue
        advisories.append(_advisory_line(
            code_suffix="SCENE", kind="场景", name=name, forecast=forecast,
        ))
    if not resources.get("scenes") and forecast.verdict == TEXT_ONLY_FALLBACK:
        # 本段压根没有声明任何场景（旧文案 STORYBOARD_PACK_RESOURCE_SCENE_
        # MANIFEST_GAP 的场景），且整镜确实落到纯文本回退——补一条镜级告警，
        # 不然"没有场景资源"这件事在 per-entity 循环里永远不会被提起。只在
        # TEXT_ONLY_FALLBACK 时补：verdict=OK 时说明别的人物/场景已经能拿到
        # 真实参考图，没有场景不构成额外后果；verdict=WILL_BLOCK 时缺口另有
        # 所指（某个人物/场景本该有资产却没有），不该被这条无场景的噪音带偏。
        advisories.append(
            "[STORYBOARD_PACK_RESOURCE_SCENE_MANIFEST_GAP][未拦截] "
            "本镜没有声明任何场景资源，生成台将按纯文本出片（画面完全由分镜文字"
            "决定）；如需为本镜挂场景参考图，请先在映射台为这段原文补齐场景发现"
        )
    return advisories


def _time_anchor_lookup_targets(resources: dict[str, Any]) -> list[str]:
    return [
        _display_name(str(entry.get("identity_id") or ""))
        for entry in resources.get("characters") or []
        if entry.get("identity_id")
    ]


def _best_time_anchor(anchors: list[dict[str, Any]]) -> dict[str, Any] | None:
    """本镜锚点里可用于按锚点查造型的那条——``anchor_key`` 为 None（era/
    relative，或数字解析失败）的锚点天然不构成可查询键，不参与挑选。"""
    keyed = [a for a in anchors if a.get("anchor_key")]
    if not keyed:
        return None
    priority = {"age": 0, "year": 1}
    return min(keyed, key=lambda a: priority.get(a.get("kind"), 9))


def _time_anchor_mismatch_line(name: str, anchor: dict[str, Any]) -> str:
    label = anchor.get("label") or anchor.get("value") or ""
    evidence = anchor.get("evidence") or ""
    key = anchor.get("anchor_key") or ""
    return (
        "[STORYBOARD_PACK_PORTRAIT_TIME_ANCHOR_MISMATCH][未拦截] "
        f"人物「{name}」本镜为{label}（原文『{evidence}』），当前用的是按集段选用的默认造型，"
        f"非「{key}」专属；到人物谱为该角色添加对应造型后重新生成"
    )


def character_time_anchor_advisories(
    *, shot: Any, project_id: str, episode_no: int | None, conn: Any,
) -> list[str]:
    """WS9：本镜命中的时间线锚点若与当前实际选用造型不符，给一条 ``[未拦截]``
    告警——不挡生成，只提醒"这一镜的人物形象可能不是原文这个时间点该有的
    样子"。与本模块其余函数不同，本函数真的做 I/O（须查
    ``character_portraits``），``conn`` 必须由调用方显式传入（CLAUDE.md
    「Ownership Must Be Explicit」）。只在确有回退（``look_mismatch.used ==
    "episode_segment"``）时才报；完全没有任何定妆照的情形已由
    ``resource_advisories_for_segment`` 的既有告警覆盖，不在这里重复。
    """
    segment = getattr(shot, "storyboard_pack_segment", None)
    if segment is None:
        return []
    anchor = _best_time_anchor(segment.get("timeline_anchors") or [])
    if anchor is None:
        return []
    from app.portraits.portrait_lookup import portrait_lookup_for_episode

    advisories: list[str] = []
    for name in _time_anchor_lookup_targets(segment.get("resources") or {}):
        result = portrait_lookup_for_episode(
            project_id, name, episode_no, time_anchor=anchor["anchor_key"], conn=conn,
        )
        mismatch = result.get("look_mismatch")
        if mismatch and mismatch.get("used") == "episode_segment":
            advisories.append(_time_anchor_mismatch_line(name, anchor))
    return advisories


def shot_resource_advisories(shot: Any, manifest: dict[str, Any] | None) -> list[str]:
    """按 ``Shot`` 对象取用的便捷入口——``storyboard_core.py`` 已顶格撞在
    ``app/FILE_CONVENTIONS.toml`` 的行数棘轮基线（675 行，零余量，只能减不能
    增），新逻辑因此全部放在本模块，不加进那个文件。旧架构行
    （``storyboard_pack_segment`` 为空）返回空列表，其余委托
    ``resource_advisories_for_segment``。``manifest`` 由调用方现查
    （``app.multiview.resolve_shot_asset_dependencies``，需要 conn/bible），
    本函数不做 I/O。
    """
    segment = getattr(shot, "storyboard_pack_segment", None)
    if segment is None:
        return []
    return resource_advisories_for_segment(
        resources=segment.get("resources") or {}, manifest=manifest,
    )


__all__ = [name for name in globals() if not name.startswith("__")]
