"""未解析/自校验失败的场景资产就地降级（WS6 追加条目）。

映射台对场景条目的两类失败——resolve_assets 阶段"未解析到已有
scene_reference_id"、provenance 自校验阶段"anchor_phrase 验不过"——原本都会
整批 ``PrepPackGateError`` 判死整集（B 上真实实例：橘座在上 ×2、神墓 ×1，
每一次都是全集素材映射作废重来）。改法：不阻断，把失败原地降级成
``asset_manifest.scenes`` 里带 ``unresolved=True``/``asset_required=False``/
``reason``/``resolution_hint`` 的一条可见记录；``segment_indexes`` 清空（不
喂给分镜台当可用候选——分镜台/生成台已有的 SCENE_MANIFEST_GAP 信号继续用，
不需要这里再拦一次），原始段号挪进 ``attempted_segment_indexes`` 仅供观测/
映射台展示。角色/道具/群演的同类失败不在本次范围内，继续照原样阻断——判据
是错误消息的"场景「...」"前缀，跟既有各 ``_check(kind, ...)`` 调用点用的
``kind`` 标签同源（见 provenance.py::_prep_pack_verify_manifest_provenance）。

跟同一次 WS6 的 coverage_ledger 诚实性改动配合：这里降级掉的段号必须落进
``scene_coverage.scene_uncovered``，不能因为"模型确实报过一条场景提及"就被
算成 ``scene_delivered``——``resolved_scene_delivered_indexes`` 只统计未
降级的条目，供
``app.production.prep_pack.generate_once._prep_pack_finalize_scene_coverage``
在资产解析/自校验都跑完之后再诚实重算一遍（早于此时算出的 scene_coverage
只是"模型报过什么"的第一手观察，不知道后面会不会解析失败）。
"""
from __future__ import annotations

from typing import Any

_SCENE_ERROR_PREFIX = "场景「"


def resolution_hint(name: str, segment_indexes: list[int]) -> str:
    """出路文案：拦住用户时必须给出路（CLAUDE.md User-Facing Behavior）。"""
    return (
        f"请到映射台为第 {sorted(segment_indexes)} 段登记/绑定场景，"
        f"或在场景库为「{name}」补 anchor_phrase"
    )


def degrade_unresolved_scene(
    scenes: dict[str, dict[str, Any]], *, name: str, segment_indexes: list[int], reason: str,
) -> None:
    """场景 mention 从未解析到 scene_reference_id 时调用：原地在 ``scenes``
    字典（by scene_id 去重，与既有 ``scenes.setdefault`` 同一约定）里登记一条
    降级条目，替代原来的整批阻断。"""
    entry = scenes.setdefault(f"scene:unresolved:{name}", {
        "scene_id": f"scene:unresolved:{name}",
        "display_name": name,
        "scene_reference_id": None,
        "segment_indexes": [],
        "attempted_segment_indexes": [],
        "provenance": None,
        "unresolved": True,
        "asset_required": False,
        "reason": reason,
        "resolution_hint": resolution_hint(name, segment_indexes),
    })
    entry["attempted_segment_indexes"] = sorted(
        set(entry["attempted_segment_indexes"]) | set(segment_indexes)
    )


def split_scene_errors(errors: list[str]) -> tuple[list[str], list[str]]:
    """把错误消息按"场景「...」"前缀分组：场景类已经被就地降级、不再阻断；
    其它类别（角色/道具/群演）维持既有阻断行为，本次不变更范围。"""
    scene = [message for message in errors if message.startswith(_SCENE_ERROR_PREFIX)]
    other = [message for message in errors if not message.startswith(_SCENE_ERROR_PREFIX)]
    return scene, other


def degrade_scene_provenance_failures(
    asset_manifest: dict[str, Any], provenance_errors: list[str],
) -> list[str]:
    """场景 provenance 自校验失败（有 scene_reference_id，但 anchor_phrase
    验不过）时，原地降级发布前已经写入 ``asset_manifest["scenes"]`` 的那条
    已解析条目——跟 ``degrade_unresolved_scene`` 是同一种归宿，只是失败发生
    的阶段更晚、条目已经存在。返回值：仍然需要阻断发布的非场景错误（角色/
    道具/群演）。"""
    remaining: list[str] = []
    for message in provenance_errors:
        if not message.startswith(_SCENE_ERROR_PREFIX):
            remaining.append(message)
            continue
        name = message[len(_SCENE_ERROR_PREFIX):].split("」", 1)[0]
        for entry in asset_manifest.get("scenes") or []:
            if entry.get("unresolved") or entry.get("display_name") != name:
                continue
            attempted = sorted(entry.get("segment_indexes") or [])
            entry["attempted_segment_indexes"] = attempted
            entry["segment_indexes"] = []
            entry["unresolved"] = True
            entry["asset_required"] = False
            entry["reason"] = message
            entry["resolution_hint"] = resolution_hint(name, attempted)
            break
    return remaining


def resolved_scene_delivered_indexes(scenes_payload: list[dict[str, Any]]) -> set[int]:
    """发布前重新汇总"真正可用"的场景交付段号——排除掉降级条目，供映射台
    coverage_ledger.scene_coverage 诚实重算。"""
    indexes: set[int] = set()
    for entry in scenes_payload:
        if entry.get("unresolved"):
            continue
        indexes.update(entry.get("segment_indexes") or [])
    return indexes
