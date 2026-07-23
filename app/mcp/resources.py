"""manju:// Resources：把只读业务快照映射为 MCP Resources（PRD §9.2）。

不重复查询逻辑——直接复用现有 REST route 函数/evidence repository，
这样 REST、内嵌 Agent 和 MCP 读到的永远是同一份数据。大正文/大媒体只给
URL/摘要，不把整本书或 base64 视频塞进模型上下文。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from app.capabilities import ensure_catalog_loaded
from app.capabilities.registry import get_registry


class ResourceError(Exception):
    def __init__(self, code: str, message: str, status: int = 404) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_]+\}")


def _template_to_regex(template: str) -> re.Pattern[str]:
    parts = re.split(r"(\{[a-zA-Z_]+\})", template)
    pattern = "".join(
        f"(?P<{part[1:-1]}>[^/]+)" if part.startswith("{") else re.escape(part)
        for part in parts
    )
    return re.compile(f"^{pattern}$")


def _content_hash(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


READERS: dict[str, Callable[..., dict[str, Any]]] = {}


def _reader(name: str) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def deco(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        READERS[name] = fn
        return fn

    return deco


@_reader("projects")
def _read_projects(**_: str) -> dict[str, Any]:
    from app.api import list_projects

    return {"items": list_projects()}


@_reader("project")
def _read_project(project_id: str) -> dict[str, Any]:
    from app.api import project_detail

    return project_detail(project_id)


@_reader("chapter")
def _read_chapter(project_id: str, idx: str) -> dict[str, Any]:
    from app.api import read_chapter

    try:
        idx_int = int(idx)
    except ValueError as exc:
        raise HTTPException(422, f"章节序号不合法：{idx}") from exc
    return read_chapter(project_id, idx_int)


@_reader("bible")
def _read_bible(project_id: str) -> dict[str, Any]:
    from app.api import project_detail

    detail = project_detail(project_id)
    return {
        "project_id": project_id,
        "bible": detail.get("bible"),
        "bible_evidence": detail.get("bible_evidence"),
        "bible_version": detail.get("bible_version"),
        "bible_status": detail.get("bible_status"),
    }


@_reader("character_portraits")
def _read_character_portraits(project_id: str, name: str) -> dict[str, Any]:
    from app.api import project_detail

    detail = project_detail(project_id)
    bible = detail.get("bible") or {}
    character = next(
        (c for c in bible.get("characters", []) if c.get("name") == name), None
    )
    if character is None:
        raise HTTPException(404, f"角色不存在：{name}")
    return {
        "project_id": project_id,
        "character": character.get("name"),
        "appearance_canonical": character.get("appearance_canonical"),
        "current_ref_image_url": character.get("ref_image_url"),
        "portrait_prompt_effective": character.get("portrait_prompt_effective"),
        "portraits": character.get("portraits", []),
    }


@_reader("scenes")
def _read_scenes(project_id: str) -> dict[str, Any]:
    from app.api import project_detail

    detail = project_detail(project_id)
    bible = detail.get("bible") or {}
    return {
        "project_id": project_id,
        "scenes": bible.get("scenes", []),
        "scene_refs_status": detail.get("scene_refs_status"),
        "scene_refs_error": detail.get("scene_refs_error"),
    }


@_reader("episodes")
def _read_episodes(project_id: str) -> dict[str, Any]:
    from app.api import project_detail

    detail = project_detail(project_id)
    return {"project_id": project_id, "episodes": detail.get("episodes", [])}


@_reader("episode")
def _read_episode(episode_id: str) -> dict[str, Any]:
    from app.api import episode_detail

    detail = dict(episode_detail(episode_id))
    shots = detail.pop("shots", [])
    detail["shot_count"] = len(shots)
    detail["adopted_shot_count"] = sum(1 for s in shots if s.get("adopted_version_id"))
    return detail


@_reader("screenplay")
def _read_screenplay(episode_id: str) -> dict[str, Any]:
    from app.api import episode_detail

    detail = episode_detail(episode_id)
    return {
        "episode_id": episode_id,
        "screenplay": detail.get("screenplay"),
        "screenplay_mode": detail.get("screenplay_mode"),
        "screenplay_evidence": detail.get("screenplay_evidence"),
    }


@_reader("storyboard")
def _read_storyboard(episode_id: str) -> dict[str, Any]:
    from app.api import episode_detail

    detail = episode_detail(episode_id)
    return {
        "episode_id": episode_id,
        "storyboard_outline": detail.get("storyboard_outline"),
        "storyboard_planned_shots": detail.get("storyboard_planned_shots"),
        "storyboard_evidence": detail.get("storyboard_evidence"),
        "shots": detail.get("shots", []),
    }


@_reader("shot")
def _read_shot(shot_id: str) -> dict[str, Any]:
    from app.api import episode_detail
    from app.db import get_conn

    row = get_conn().execute("SELECT episode_id FROM shots WHERE id=?", (shot_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"镜头不存在：{shot_id}")
    detail = episode_detail(row["episode_id"])
    shot = next((s for s in detail.get("shots", []) if s.get("id") == shot_id), None)
    if shot is None:
        raise HTTPException(404, f"镜头不存在：{shot_id}")
    return {"episode_id": row["episode_id"], "shot": shot}


@_reader("run")
def _read_run(run_id: str) -> dict[str, Any]:
    from app.orchestration.api import get_run

    return get_run(run_id)


@_reader("run_events")
def _read_run_events(run_id: str) -> dict[str, Any]:
    from app.orchestration.api import get_events

    return {"run_id": run_id, "events": get_events(run_id, after=None, limit=500)}


@_reader("artifact")
def _read_artifact(artifact_id: str) -> dict[str, Any]:
    from app.orchestration.api import get_artifact

    return get_artifact(artifact_id)


@_reader("artifact_lineage")
def _read_artifact_lineage(artifact_id: str) -> dict[str, Any]:
    from app.orchestration.api import get_artifact_lineage

    return {"artifact_id": artifact_id, "lineage": get_artifact_lineage(artifact_id)}


@_reader("delivery")
def _read_delivery(episode_id: str) -> dict[str, Any]:
    from app.api import mix_status
    from app.orchestration.api import get_delivery_readiness, list_delivery_packages

    return {
        "episode_id": episode_id,
        "readiness": get_delivery_readiness(episode_id),
        "packages": list_delivery_packages(episode_id),
        "mix_status": mix_status(episode_id),
    }


@_reader("system_health")
def _read_system_health(**_: str) -> dict[str, Any]:
    from app.system_api import health

    return health()


@_reader("gates")
def _read_gates(**_: str) -> dict[str, Any]:
    from app.orchestration.api import list_pending_gates

    return {"items": list_pending_gates(project_id=None, limit=100)}


# 章节正文/模型输出等来自素材内容，不能被当作系统指令；标记为 untrusted_content
# 供调用方（内嵌 Agent / 外部 MCP Client）区分事实来源（PRD §7.2 / §12.1）。
_UNTRUSTED_CONTENT_RESOURCES = frozenset({"chapter", "screenplay", "storyboard", "shot"})


def _trust_level(spec_name: str) -> str:
    return "untrusted_content" if spec_name in _UNTRUSTED_CONTENT_RESOURCES else "trusted"


def _extract_version(data: dict[str, Any]) -> Any:
    for key in ("version", "bible_version", "screenplay_version"):
        if key in data:
            return data[key]
    return None


def list_resources() -> list[dict[str, Any]]:
    ensure_catalog_loaded()
    registry = get_registry()
    return [
        {
            "uri": spec.uri_template,
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "mimeType": "application/json",
        }
        for spec in registry.resources.values()
        if spec.mcp_exposed and not _PLACEHOLDER_RE.search(spec.uri_template)
    ]


def list_resource_templates() -> list[dict[str, Any]]:
    ensure_catalog_loaded()
    registry = get_registry()
    return [
        {
            "uriTemplate": spec.uri_template,
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "mimeType": "application/json",
        }
        for spec in registry.resources.values()
        if spec.mcp_exposed and _PLACEHOLDER_RE.search(spec.uri_template)
    ]


def read_resource(uri: str) -> dict[str, Any]:
    ensure_catalog_loaded()
    registry = get_registry()
    for spec in registry.resources.values():
        if not spec.mcp_exposed:
            continue
        match = _template_to_regex(spec.uri_template).match(uri)
        if not match:
            continue
        reader_fn = READERS.get(spec.name)
        if reader_fn is None:
            raise ResourceError("not_implemented", f"resource reader not implemented: {spec.name}", 501)
        try:
            data = reader_fn(**match.groupdict())
        except HTTPException as exc:
            code = "not_found" if exc.status_code == 404 else "resource_error"
            raise ResourceError(code, str(exc.detail), exc.status_code) from exc
        return {
            "uri": uri,
            "name": spec.name,
            "title": spec.title,
            "mimeType": "application/json",
            "version": _extract_version(data),
            "content_hash": _content_hash(data),
            "trust_level": _trust_level(spec.name),
            "content": data,
        }
    raise ResourceError("unknown_resource", f"no resource matches uri: {uri}", 404)
