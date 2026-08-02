"""System, settings, model-catalog, credentials, and filesystem API routes."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import string
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse

from app import config
from app.db import get_conn, get_setting, new_id, rows_to_dicts, set_setting
from app.local_session import require_local_session

router = APIRouter(prefix="/api")
# 公开探活路由：不挂本机会话依赖（由 main 单独 include）。
public_router = APIRouter(prefix="/api")

_MONITOR_EVENTS = {
    "block_load", "drilldown", "deep_link", "job_action", "gate_action",
    "settings_preview", "settings_submit", "call_detail", "query_result",
    "bible_payment_precheck", "bible_payment_confirm", "bible_conflict",
    "portrait_qa_review", "bible_navigation_guard",
}


@router.post("/system/monitor/events")
def record_monitor_event(body: dict):
    """监制房最小化埋点；只收白名单维度，拒绝 prompt/正文/密钥。"""
    import hashlib
    from app.monitoring import audit

    name = str(body.get("name") or "")
    if name not in _MONITOR_EVENTS:
        raise HTTPException(422, "未知监制房事件")
    dimensions = body.get("dimensions") if isinstance(body.get("dimensions"), dict) else {}
    allowed = {
        "block", "result", "error_category", "source", "target_type", "filter_count",
        "action", "object_status", "permission", "conflict", "apply_mode", "size_bucket",
        "query_type", "total", "page_size", "query_ms",
    }
    safe = {str(key): value for key, value in dimensions.items() if key in allowed and isinstance(value, (str, int, float, bool))}
    object_id = str(body.get("object_id") or "")
    object_hash = hashlib.sha256(object_id.encode()).hexdigest()[:16] if object_id else "none"
    audit(name, "monitor_event", object_hash, "recorded", safe)
    return {"ok": True}

# ---------- 文件系统目录浏览（本机部署，供导出目录选择器使用） ----------

_BLOCKED_BROWSE_PREFIXES = (
    "/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/var/log",
    "/private/etc", "/private/var",
)


def _is_blocked_fs_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    text = str(resolved)
    lowered = text.lower()
    if any(text == prefix or text.startswith(prefix + os.sep) for prefix in _BLOCKED_BROWSE_PREFIXES):
        return True
    # 隐藏敏感家目录内容
    parts = {p.lower() for p in resolved.parts}
    if parts & {".ssh", ".gnupg", ".aws", ".kube", ".docker"}:
        return True
    if lowered.endswith(".env") or "/.env/" in lowered.replace("\\", "/"):
        return True
    return False


def _list_directory_grants() -> list[str]:
    try:
        raw = json.loads(get_setting("directory_grants") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _builtin_directory_roots() -> list[Path]:
    """默认可浏览/建目录根：仅项目与数据目录，不开放家目录枚举（Todolist T5）。"""
    return [config.PROJECTS_DIR.resolve(), config.DATA_DIR.resolve()]


def allowed_directory_roots() -> list[Path]:
    roots = list(_builtin_directory_roots())
    for grant in _list_directory_grants():
        try:
            path = Path(grant).expanduser().resolve()
        except OSError:
            continue
        if _is_blocked_fs_path(path):
            continue
        if path not in roots:
            roots.append(path)
    return roots


def assert_path_under_directory_grant(path: Path) -> Path:
    """路径必须落在 builtin 根或已授权 directory_grant 之下。"""
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise HTTPException(400, f"路径不可解析：{path}") from exc
    # 应用内建数据根由部署配置明确指定；macOS 的临时目录会解析到
    # /private/var，不能因此把应用自己的 projects/data 目录误判为敏感路径。
    for root in _builtin_directory_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    if _is_blocked_fs_path(resolved):
        raise HTTPException(403, f"不允许访问系统敏感目录：{resolved}")
    for root in allowed_directory_roots():
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise HTTPException(403, f"路径不在已授权 directory_grant 白名单内：{resolved}")


def _list_drives() -> list[str]:
    if os.name != "nt":
        return []
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


@router.get("/system/browse")
def browse_dir(path: str = ""):
    """列出已授权根或其子目录；空 path 返回白名单根，不枚举家目录。"""
    drives = _list_drives()
    p = (path or "").strip()
    if not p:
        roots = allowed_directory_roots()
        return {
            "path": "",
            "parent": None,
            "drives": drives,
            "dirs": [{"name": root.name or str(root), "path": str(root)} for root in roots],
            "grants": [str(root) for root in roots],
        }
    base = Path(p)
    assert_path_under_directory_grant(base)
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, f"目录不存在：{p}")
    dirs = []
    try:
        for child in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir() and not _is_blocked_fs_path(child):
                    # 子目录仍须落在 grant 内（通常自然满足）
                    try:
                        assert_path_under_directory_grant(child)
                    except HTTPException:
                        continue
                    dirs.append({"name": child.name, "path": str(child.resolve())})
            except OSError:
                continue  # 个别子项无权访问/不可达，跳过
    except PermissionError:
        raise HTTPException(403, f"无权访问：{p}")
    parent = str(base.parent) if base.parent != base else None
    if parent:
        try:
            assert_path_under_directory_grant(Path(parent))
        except HTTPException:
            parent = None
    return {"path": str(base.resolve()), "parent": parent, "drives": drives, "dirs": dirs}


@router.post("/system/directory-grants")
def grant_directory(body: dict):
    """人工授权一个可浏览/建子目录的根路径（Todolist T5）。"""
    raw = str(body.get("path") or "").strip()
    if not raw:
        raise HTTPException(422, "缺少 path")
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise HTTPException(400, f"路径不可解析：{raw}") from exc
    if _is_blocked_fs_path(resolved):
        raise HTTPException(403, f"不允许授权系统敏感目录：{resolved}")
    if not resolved.exists() or not resolved.is_dir():
        raise HTTPException(404, f"目录不存在：{resolved}")
    grants = _list_directory_grants()
    text = str(resolved)
    if text not in grants:
        grants.append(text)
        set_setting("directory_grants", json.dumps(grants, ensure_ascii=False))
    return {"ok": True, "path": text, "grants": [str(r) for r in allowed_directory_roots()]}


@router.get("/system/directory-grants")
def list_directory_grants_route():
    return {"grants": [str(r) for r in allowed_directory_roots()]}


@router.post("/system/mkdir")
async def make_dir_route(body: dict):
    """在指定父目录下新建文件夹，供选择器「新建文件夹」使用。"""
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "system.mkdir",
        {"parent_grant": (body.get("path") or "").strip(), "name": (body.get("name") or "").strip()},
    )
    if routed is not None:
        return routed
    return make_dir(body)


def make_dir(body: dict):
    """领域实现：仅允许在已授权 directory_grant 下创建子目录。"""
    parent = (body.get("path") or "").strip()
    name = (body.get("name") or "").strip()
    if not parent or not name:
        raise HTTPException(422, "缺少父目录或文件夹名")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(422, '文件夹名含非法字符（不能包含 \\ / : * ? " < > |）')
    if name in {".", ".."}:
        raise HTTPException(422, "文件夹名非法")
    parent_path = assert_path_under_directory_grant(Path(parent))
    if not parent_path.exists() or not parent_path.is_dir():
        raise HTTPException(404, f"父目录不存在：{parent}")
    dest = parent_path / name
    # 新建目标也必须仍在同一 grant 树下
    assert_path_under_directory_grant(dest)
    try:
        dest.mkdir(parents=False, exist_ok=True)
    except OSError as e:
        raise HTTPException(400, f"创建失败：{e}")
    return {"path": str(dest)}


# ---------- 系统 ----------

MODEL_KINDS = {"text", "vlm", "video", "image"}
MODEL_PROVIDER_KINDS = {
    "hiagent": MODEL_KINDS,
    "openrouter": {"text", "vlm"},
    "bailian": {"text", "vlm"},
    "deepseek": {"text"},
    "zhipu": {"text"},
}
BUILTIN_MODELS = (
    ("hiagent", config.DEFAULT_HIAGENT_MODEL_TEXT, "文本推理模型", ("text",)),
    ("hiagent", "d71l5c8nfdb167kligqg", "Text 模型", ("text",)),
    ("hiagent", config.DEFAULT_HIAGENT_MODEL_VLM, "视觉质检模型", ("vlm",)),
    ("hiagent", config.DEFAULT_HIAGENT_MODEL_VIDEO, "Seedance 视频生成", ("video",)),
    ("hiagent", config.DEFAULT_HIAGENT_MODEL_IMAGE, "Seedream 图像生成", ("image",)),
    ("openrouter", "z-ai/glm-5.2", "GLM 5.2", ("text",)),
    ("openrouter", "anthropic/claude-opus-4.8", "Claude Opus 4.8", ("text",)),
    ("openrouter", "google/gemini-3.5-flash", "Gemini 3.5 Flash", ("vlm",)),
    ("bailian", "qwen3.7-max-2026-06-08", "Qwen3.7-Max 2026-06-08", ("text",)),
    ("bailian", "qwen3.7-max-2026-05-20", "Qwen3.7-Max 2026-05-20", ("text",)),
    ("bailian", "qwen3.7-max-2026-05-17", "Qwen3.7-Max 2026-05-17", ("text",)),
    ("bailian", "qwen3.7-max-preview", "Qwen3.7-Max Preview", ("text",)),
    ("bailian", "qwen3.7-plus-2026-05-26", "Qwen3.7-Plus 2026-05-26", ("text", "vlm")),
    ("bailian", "qwen3.7-max", "Qwen3.7-Max", ("text",)),
    ("bailian", "qwen3.7-plus", "Qwen3.7-Plus", ("text", "vlm")),
    ("deepseek", "deepseek-v4-pro", "DeepSeek V4 Pro", ("text",)),
    ("zhipu", "glm-5.2", "GLM 5.2", ("text",)),
)


def _custom_models() -> list[dict]:
    try:
        value = json.loads(get_setting("custom_models") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _model_catalog() -> list[dict]:
    builtins = [
        {"id": f"builtin:{provider}:{model}", "provider": provider, "model": model,
         "label": label, "kinds": list(kinds), "builtin": True}
        for provider, model, label, kinds in BUILTIN_MODELS
    ]
    return [*builtins, *_custom_models()]


def _public_model(item: dict) -> dict:
    public = {key: value for key, value in item.items() if key != "api_key"}
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, json.JSONDecodeError):
        credentials = {}
    provider_key = {
        "hiagent": config.HIAGENT_API_KEY, "openrouter": config.OPENROUTER_API_KEY,
        "bailian": config.BAILIAN_API_KEY, "deepseek": config.DEEPSEEK_API_KEY,
        "zhipu": config.ZHIPU_API_KEY,
    }.get(str(item.get("provider") or ""), "")
    public["key_configured"] = bool(
        item.get("api_key") or credentials.get(item.get("id"), {}).get("api_key") or provider_key
    )
    return public


@router.get("/models")
def get_models():
    return {"items": [_public_model(item) for item in _model_catalog()]}


@router.post("/models")
async def add_model_route(body: dict):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("system.model_create", {"model": body})
    if routed is not None:
        return routed
    return add_model(body)


def add_model(body: dict):
    provider = str(body.get("provider") or "").strip().lower()
    model = str(body.get("model") or "").strip()
    label = str(body.get("label") or "").strip()
    kinds = list(dict.fromkeys(str(k).strip().lower() for k in (body.get("kinds") or [])))
    custom_provider = provider == "custom"
    if provider not in MODEL_PROVIDER_KINDS and not custom_provider:
        raise HTTPException(422, "不支持该模型服务商")
    if not model or len(model) > 180 or any(ch.isspace() for ch in model):
        raise HTTPException(422, "模型 ID 必填，且不能包含空格")
    if not label or len(label) > 80:
        raise HTTPException(422, "模型名称必填，且不能超过 80 个字符")
    allowed_kinds = {"text", "vlm"} if custom_provider else MODEL_PROVIDER_KINDS[provider]
    if not kinds or any(k not in allowed_kinds for k in kinds):
        raise HTTPException(422, "所选服务商不支持该模型能力")
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    api_key = str(body.get("api_key") or "").strip()
    provider_label = str(body.get("provider_label") or "").strip()
    if custom_provider:
        if not provider_label or len(provider_label) > 60:
            raise HTTPException(422, "请填写自定义服务名称")
        if not re.fullmatch(r"https?://[^\s]+", base_url):
            raise HTTPException(422, "Base URL 必须是有效的 http(s) 地址")
        if not api_key:
            raise HTTPException(422, "请填写自定义服务 API Key")
    if not custom_provider and any(item["provider"] == provider and item["model"] == model for item in _model_catalog()):
        raise HTTPException(409, "这个模型已经在模型库中")
    item_id = new_id("model")
    if custom_provider:
        provider = f"custom:{item_id}"
    item = {
        "id": item_id, "provider": provider, "model": model,
        "label": label, "kinds": kinds, "builtin": False,
    }
    if custom_provider:
        item.update({"provider_label": provider_label, "base_url": base_url, "api_key": api_key})
    custom = _custom_models()
    custom.append(item)
    set_setting("custom_models", json.dumps(custom, ensure_ascii=False))
    return _public_model(item)


def _assert_public_http_url(base_url: str) -> None:
    """拒绝指向本机/内网/链路本地的探测 URL，降低 SSRF 风险。"""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(422, "Base URL 必须是 http(s)")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise HTTPException(422, "Base URL 缺少主机名")
    if host in {"localhost", "metadata", "metadata.google.internal"} or host.endswith(".local"):
        raise HTTPException(422, "不允许探测本机或链路本地地址")
    if host == "169.254.169.254":
        raise HTTPException(422, "不允许探测云元数据地址")
    candidates: list[str] = []
    try:
        ipaddress.ip_address(host)
        candidates = [host]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise HTTPException(422, f"无法解析主机名：{host}") from exc
        for info in infos:
            addr = info[4][0]
            if addr:
                candidates.append(addr)
    if not candidates:
        raise HTTPException(422, f"无法解析主机名：{host}")
    for addr in candidates:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise HTTPException(422, "不允许探测内网或保留地址")


async def _probe_openai_model(base_url: str, api_key: str, model: str, kind: str = "text") -> dict:
    base_url = base_url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s]+", base_url):
        raise HTTPException(422, "Base URL 必须是有效的 http(s) 地址")
    if not api_key.strip() or not model.strip():
        raise HTTPException(422, "模型 ID 和 API Key 不能为空")
    _assert_public_http_url(base_url)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"},
                json={"model": model.strip(), "messages": [{"role": "user", "content": "Reply with OK only."}], "max_tokens": 8, "temperature": 0},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(422, f"连接失败：{type(exc).__name__}，请检查 Base URL 和网络") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    if not response.is_success:
        raw_lower = response.text.lower()
        if kind in {"video", "image"} and response.status_code == 400 and (
                "not supported for this endpoint" in raw_lower or "expects model type" in raw_lower):
            return {
                "ok": True, "latency_ms": latency_ms, "probe": "model_recognition",
                "preview": "凭证与模型识别通过；为避免产生费用，未执行媒体生成",
            }
        if response.status_code in {401, 403}:
            message = "当前 API Key 无效，或没有访问该模型的权限"
        elif response.status_code == 404:
            message = "接口或模型不存在，请检查 Base URL 与模型 ID"
        elif response.status_code == 429:
            message = "服务当前限流或额度不足，请稍后重试"
        else:
            detail = response.text[:180].replace(api_key, "***")
            message = f"上游返回：{detail}"
        raise HTTPException(422, f"模型测试失败（HTTP {response.status_code}）：{message}")
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(422, "服务已响应，但返回格式不是 OpenAI chat/completions 兼容格式") from exc
    return {"ok": True, "latency_ms": latency_ms, "preview": str(content or "")[:80]}


@router.post("/models/test")
async def test_model_connection(body: dict):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("system.model_test", {"draft": body})
    if routed is not None:
        return routed
    return await _probe_openai_model(
        str(body.get("base_url") or ""), str(body.get("api_key") or ""),
        str(body.get("model") or ""), str(body.get("kind") or "text"))


@router.put("/models/{model_id}")
async def update_model_route(model_id: str, body: dict):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("system.model_update", {"model_id": model_id, "patch": body})
    if routed is not None:
        return routed
    return update_model(model_id, body)


def update_model(model_id: str, body: dict):
    custom = _custom_models()
    item = next((m for m in custom if m.get("id") == model_id), None)
    if not item:
        raise HTTPException(404, "模型不存在或为内置模型")
    label = str(body.get("label") or item.get("label") or "").strip()
    model = str(body.get("model") or item.get("model") or "").strip()
    kinds = list(dict.fromkeys(str(k).strip().lower() for k in (body.get("kinds") or item.get("kinds") or [])))
    if not label or not model or any(ch.isspace() for ch in model):
        raise HTTPException(422, "模型名称和模型 ID 必填，模型 ID 不能包含空格")
    if not kinds or any(k not in {"text", "vlm"} for k in kinds):
        raise HTTPException(422, "自定义 OpenAI 兼容模型仅支持 Text/VLM")
    item.update({"label": label, "model": model, "kinds": kinds})
    if str(item.get("provider", "")).startswith("custom:"):
        provider_label = str(body.get("provider_label") or item.get("provider_label") or "").strip()
        base_url = str(body.get("base_url") or item.get("base_url") or "").strip().rstrip("/")
        if not provider_label or not re.fullmatch(r"https?://[^\s]+", base_url):
            raise HTTPException(422, "自定义服务名称或 Base URL 无效")
        item.update({"provider_label": provider_label, "base_url": base_url})
        if str(body.get("api_key") or "").strip():
            item["api_key"] = str(body["api_key"]).strip()
    set_setting("custom_models", json.dumps(custom, ensure_ascii=False))
    return _public_model(item)


@router.post("/models/{model_id}/test")
async def test_saved_model(model_id: str, body: dict | None = None):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route(
        "system.model_test", {"model_id": model_id, "draft": body or {}},
    )
    if routed is not None:
        return routed
    item = next((m for m in _model_catalog() if m.get("id") == model_id), None)
    if not item:
        raise HTTPException(404, "模型不存在")
    override = body or {}
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, json.JSONDecodeError):
        credentials = {}
    saved = credentials.get(model_id, {}) if isinstance(credentials, dict) else {}
    base_url = str(override.get("base_url") or saved.get("base_url") or item.get("base_url") or "")
    api_key = str(override.get("api_key") or saved.get("api_key") or item.get("api_key") or "")
    model = str(override.get("model") or item.get("model") or "")
    kinds = item.get("kinds") or ["text"]
    kind = "video" if "video" in kinds else "image" if "image" in kinds else "vlm" if "vlm" in kinds and "text" not in kinds else "text"
    return await _probe_openai_model(base_url, api_key, model, kind)


@router.delete("/models/{model_id}")
async def delete_model_route(model_id: str):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("system.model_delete", {"model_id": model_id})
    if routed is not None:
        return routed
    return delete_model(model_id)


def delete_model(model_id: str):
    custom = _custom_models()
    item = next((m for m in custom if m.get("id") == model_id), None)
    if not item:
        raise HTTPException(404, "模型不存在或为内置模型")
    from app import hiagent
    for kind in item.get("kinds", []):
        if hiagent.active_provider(kind) == item.get("provider") and hiagent.active_model(kind) == item.get("model"):
            raise HTTPException(409, f"该模型正在用于 {kind}，请先切换后再删除")
    set_setting("custom_models", json.dumps([m for m in custom if m.get("id") != model_id], ensure_ascii=False))
    return {"ok": True}


@router.put("/models/{model_id}/credentials")
def put_model_credentials(model_id: str, body: dict):
    item = next((m for m in _model_catalog() if m.get("id") == model_id), None)
    if not item:
        raise HTTPException(404, "模型不存在")
    if body.get("confirm") is not True:
        raise HTTPException(422, "写入模型凭证需 confirm=true 二次确认")
    base_url = str(body.get("base_url") or "").strip().rstrip("/")
    api_key = str(body.get("api_key") or "").strip()
    if not re.fullmatch(r"https?://[^\s]+", base_url):
        raise HTTPException(422, "Base URL 必须是有效的 http(s) 地址")
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, json.JSONDecodeError):
        credentials = {}
    current_key = str(credentials.get(model_id, {}).get("api_key") or item.get("api_key") or "")
    if not api_key and not current_key:
        raise HTTPException(422, "API Key 不能为空")
    credentials[model_id] = {"base_url": base_url, "api_key": api_key or current_key}
    set_setting("model_credentials", json.dumps(credentials, ensure_ascii=False))
    return {"ok": True, "key_configured": True}

@public_router.get("/system/health")
def health():
    from app import config, hiagent

    def option(provider: str, model: str, available: bool = True) -> dict:
        return {"provider": provider, "model": model, "available": available}

    def selected(kind: str, label: str, options: list[dict]) -> dict:
        provider = hiagent.active_provider(kind)
        active = next((o for o in options if o["provider"] == provider), options[0])
        return {
            "key": kind,
            "label": label,
            "provider": active["provider"],
            "model": active["model"],
            "options": options,
        }

    def custom_options(kind: str) -> list[dict]:
        return [option(item["provider"], item["model"], bool(_public_model(item).get("key_configured")))
                for item in _custom_models()
                if kind in item.get("kinds", []) and str(item.get("provider", "")).startswith("custom:")]

    def model_available(provider: str, model: str, legacy_available: bool) -> bool:
        item = next((m for m in _model_catalog() if m.get("provider") == provider and m.get("model") == model), None)
        return bool(legacy_available or (item and _public_model(item).get("key_configured")))

    models = {
        "text": selected("text", "Text 模型", [
            option("hiagent", hiagent.active_model("text", "hiagent"), model_available("hiagent", hiagent.active_model("text", "hiagent"), bool(config.HIAGENT_API_KEY))),
            option("openrouter", hiagent.active_model("text", "openrouter"), model_available("openrouter", hiagent.active_model("text", "openrouter"), bool(config.OPENROUTER_API_KEY))),
            option("bailian", hiagent.active_model("text", "bailian"), model_available("bailian", hiagent.active_model("text", "bailian"), bool(config.BAILIAN_API_KEY))),
            option("deepseek", hiagent.active_model("text", "deepseek"), model_available("deepseek", hiagent.active_model("text", "deepseek"), bool(config.DEEPSEEK_API_KEY))),
            option("zhipu", hiagent.active_model("text", "zhipu"), model_available("zhipu", hiagent.active_model("text", "zhipu"), bool(config.ZHIPU_API_KEY))),
            *custom_options("text"),
        ]),
        "vlm": selected("vlm", "VLM 模型", [
            option("hiagent", hiagent.active_model("vlm", "hiagent"), model_available("hiagent", hiagent.active_model("vlm", "hiagent"), bool(config.HIAGENT_API_KEY))),
            option("openrouter", hiagent.active_model("vlm", "openrouter"), model_available("openrouter", hiagent.active_model("vlm", "openrouter"), bool(config.OPENROUTER_API_KEY))),
            option("bailian", hiagent.active_model("vlm", "bailian"), model_available("bailian", hiagent.active_model("vlm", "bailian"), bool(config.BAILIAN_API_KEY))),
            *custom_options("vlm"),
        ]),
        "video": selected("video", "视频模型", [
            option("hiagent", hiagent.active_model("video", "hiagent"), model_available("hiagent", hiagent.active_model("video", "hiagent"), bool(config.HIAGENT_API_KEY))),
            option("openrouter", "", False),
        ]),
        "image": selected("image", "图像模型", [
            option("hiagent", hiagent.active_model("image", "hiagent"), model_available("hiagent", hiagent.active_model("image", "hiagent"), bool(config.HIAGENT_API_KEY))),
            option("openrouter", "", False),
        ]),
    }
    return {
        "ok": True,
        "gateway": config.HIAGENT_BASE_URL,
        "key_configured": bool(config.HIAGENT_API_KEY),
        "model_route": get_setting("model_route") or "hiagent",
        "openrouter_key_configured": bool(config.OPENROUTER_API_KEY),
        "bailian_key_configured": bool(config.BAILIAN_API_KEY),
        "deepseek_key_configured": bool(config.DEEPSEEK_API_KEY),
        "zhipu_key_configured": bool(config.ZHIPU_API_KEY),
        "hiagent_model_text": hiagent.active_model("text", "hiagent"),
        "hiagent_model_vlm": hiagent.active_model("vlm", "hiagent"),
        "hiagent_model_video": hiagent.active_model("video", "hiagent"),
        "hiagent_model_image": hiagent.active_model("image", "hiagent"),
        "openrouter_model_text": hiagent.active_model("text", "openrouter"),
        "openrouter_model_vlm": hiagent.active_model("vlm", "openrouter"),
        "bailian_model_text": hiagent.active_model("text", "bailian"),
        "bailian_model_vlm": hiagent.active_model("vlm", "bailian"),
        "deepseek_model_text": hiagent.active_model("text", "deepseek"),
        "zhipu_model_text": hiagent.active_model("text", "zhipu"),
        "models": models,
    }


def _effective_call_status(row: dict) -> str:
    effective = row["status"]
    if row["status"] == "INTERRUPTED":
        if row.get("recovery_disposition") == "RETRIED_SUCCESSFULLY":
            effective = "RECOVERED"
        elif row.get("recovery_disposition") in {"RETRY_STARTED", "RETRYING_INTERRUPTED"}:
            effective = "RETRYING"
    return effective


@router.get("/system/calls")
def recent_calls(limit: int = 30):
    """最近调用概览：禁止回传 request/response 原文（Todolist T6）。"""
    rows = rows_to_dicts(get_conn().execute(
        """SELECT id,ts,kind,model,status,http_status,latency_ms,error,run_id,step_run_id,
                  trace_id,operation_id,attempt_no,supersedes_call_id,superseded_by_call_id,
                  recovery_disposition,meta
           FROM provider_calls ORDER BY id DESC LIMIT ?""",
        (min(limit, 200),),
    ).fetchall())
    for row in rows:
        row["effective_status"] = _effective_call_status(row)
        row["context"] = _call_meta_summary(row.pop("meta", None))
    return rows


_BUSINESS_CALL_KINDS = {
    "chat", "vlm", "vlm_qa", "video_create", "video_poll", "image", "image_generate",
    "image_edit", "scene_image", "screenplay_prompt", "plan_prompt", "bible_prompt",
    "references_prompt", "storyboard_prompt", "storyboard_shot_prompt", "storyboard_outline_prompt",
}
_WORKFLOW_CALL_MARKERS = ("prompt", "storyboard", "reference", "handoff", "repair")
_FAILED_CALL_STATUSES = {"FAILED", "TIMEOUT", "NETWORK_ERROR", "TASK_FAILED", "QA_ERROR", "REPAIR_STALLED"}


def _call_category(kind: str) -> str:
    if kind in _BUSINESS_CALL_KINDS:
        return "business"
    if any(marker in kind for marker in _WORKFLOW_CALL_MARKERS):
        return "workflow"
    return "internal"


def _call_meta_summary(raw: str | None) -> dict:
    try:
        meta = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(meta, dict):
        return {}
    allowed = {
        "project_id", "project_name", "episode_id", "episode_no", "episode_title",
        "shot_id", "shot_no", "stage", "asset_kind", "frame_kind", "reference_type",
        "character_name", "scene_name", "call_role", "call_role_label", "caller_module",
        "caller_function", "contract_version", "error_stage", "purpose",
    }
    return {key: value for key, value in meta.items() if key in allowed}


def _scope_project_id(scope_type: str | None, scope_id: str | None) -> str | None:
    """Resolve an evidence scope without trusting denormalized UI metadata."""
    if not scope_type or not scope_id:
        return None
    conn = get_conn()
    if scope_type == "project":
        row = conn.execute("SELECT id FROM projects WHERE id=?", (scope_id,)).fetchone()
    elif scope_type == "episode":
        row = conn.execute("SELECT project_id AS id FROM episodes WHERE id=?", (scope_id,)).fetchone()
    elif scope_type == "shot":
        row = conn.execute(
            "SELECT e.project_id AS id FROM shots s JOIN episodes e ON e.id=s.episode_id WHERE s.id=?",
            (scope_id,),
        ).fetchone()
    else:
        row = None
    return str(row["id"]) if row else None


def _run_project_id(run_id: str | None) -> str | None:
    if not run_id:
        return None
    row = get_conn().execute(
        "SELECT scope_type,scope_id FROM workflow_runs WHERE id=?", (run_id,),
    ).fetchone()
    return _scope_project_id(row["scope_type"], row["scope_id"]) if row else None


def _resolved_project_id(candidates: list[object]) -> str | None:
    values = {str(value) for value in candidates if value}
    return next(iter(values)) if len(values) == 1 else None


def _project_scope_maps() -> dict[str, dict[str, str]]:
    """Load immutable scope relations once for high-volume observability queries."""
    conn = get_conn()
    episodes = {str(row["id"]): str(row["project_id"]) for row in conn.execute(
        "SELECT id,project_id FROM episodes"
    ).fetchall()}
    shots = {str(row["id"]): str(row["project_id"]) for row in conn.execute(
        """SELECT s.id,e.project_id FROM shots s JOIN episodes e ON e.id=s.episode_id"""
    ).fetchall()}
    runs: dict[str, str] = {}
    for row in conn.execute("SELECT id,scope_type,scope_id FROM workflow_runs").fetchall():
        if row["scope_type"] == "project":
            project_id = str(row["scope_id"])
        elif row["scope_type"] == "episode":
            project_id = episodes.get(str(row["scope_id"]))
        elif row["scope_type"] == "shot":
            project_id = shots.get(str(row["scope_id"]))
        else:
            project_id = None
        if project_id:
            runs[str(row["id"])] = project_id
    steps = {str(row["id"]): str(row["run_id"]) for row in conn.execute(
        "SELECT id,run_id FROM step_runs"
    ).fetchall()}
    return {"episodes": episodes, "shots": shots, "runs": runs, "steps": steps}


def _call_project_id(
    row: dict,
    context: dict | None = None,
    scope_maps: dict[str, dict[str, str]] | None = None,
) -> str | None:
    context = context or {}
    run_project = (
        scope_maps["runs"].get(str(row.get("run_id")))
        if scope_maps and row.get("run_id") else _run_project_id(row.get("run_id"))
    )
    candidates: list[object] = [context.get("project_id"), run_project]
    step_id = row.get("step_run_id")
    if step_id:
        if scope_maps:
            step_run = scope_maps["steps"].get(str(step_id))
            candidates.append(scope_maps["runs"].get(step_run or ""))
        else:
            step = get_conn().execute("SELECT run_id FROM step_runs WHERE id=?", (step_id,)).fetchone()
            if step:
                candidates.append(_run_project_id(step["run_id"]))
    if context.get("episode_id"):
        candidates.append(
            scope_maps["episodes"].get(str(context["episode_id"])) if scope_maps
            else _scope_project_id("episode", str(context["episode_id"]))
        )
    if context.get("shot_id"):
        candidates.append(
            scope_maps["shots"].get(str(context["shot_id"])) if scope_maps
            else _scope_project_id("shot", str(context["shot_id"]))
        )
    return _resolved_project_id(candidates)


def _job_project_id(
    row: dict,
    scope_maps: dict[str, dict[str, str]] | None = None,
) -> str | None:
    candidates: list[object] = [row.get("project_id")]
    run_id = row.get("run_id") or (row.get("id") if row.get("source") == "run" else None)
    if run_id:
        candidates.append(
            scope_maps["runs"].get(str(run_id)) if scope_maps
            else _run_project_id(str(run_id))
        )
    if row.get("episode_id"):
        candidates.append(
            scope_maps["episodes"].get(str(row["episode_id"])) if scope_maps
            else _scope_project_id("episode", str(row["episode_id"]))
        )
    if row.get("shot_id"):
        candidates.append(
            scope_maps["shots"].get(str(row["shot_id"])) if scope_maps
            else _scope_project_id("shot", str(row["shot_id"]))
        )
    return _resolved_project_id(candidates)


@router.get("/system/calls/query")
def query_calls(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str = "",
    status: str | None = None,
    category: str | None = None,
    project_id: str | None = None,
    function: str | None = None,
    model: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    sort: str = "desc",
    ids: str | None = None,
):
    query_started = time.perf_counter()
    clauses: list[str] = []
    params: list[object] = []
    selected_ids = [int(value) for value in (ids or "").split(",") if value.strip().isdigit()][:500]
    if selected_ids:
        clauses.append(f"id IN ({','.join('?' for _ in selected_ids)})")
        params.extend(selected_ids)
    if search.strip():
        needle = f"%{search.strip()}%"
        clauses.append("(kind LIKE ? OR model LIKE ? OR status LIKE ? OR error LIKE ? OR CAST(id AS TEXT) LIKE ? OR meta LIKE ?)")
        params.extend([needle] * 6)
    if function:
        clauses.append("kind=?")
        params.append(function)
    if model:
        clauses.append("model=?")
        params.append(model)
    if from_ts is not None:
        clauses.append("ts>=?")
        params.append(from_ts)
    if to_ts is not None:
        clauses.append("ts<=?")
        params.append(to_ts)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order = "ASC" if sort.lower() == "asc" else "DESC"
    rows = rows_to_dicts(get_conn().execute(
        f"""SELECT id,ts,kind,model,status,http_status,latency_ms,error,run_id,step_run_id,
                    trace_id,operation_id,attempt_no,supersedes_call_id,superseded_by_call_id,
                    recovery_disposition,meta
             FROM provider_calls {where} ORDER BY id {order}""",
        params,
    ).fetchall())
    scope_maps = _project_scope_maps() if project_id else None
    catalog = {(item.get("provider"), item.get("model")): item.get("label") for item in _model_catalog()}
    filtered: list[dict] = []
    for row in rows:
        row["effective_status"] = _effective_call_status(row)
        if status and row["effective_status"] != status:
            continue
        row["category"] = _call_category(str(row.get("kind") or ""))
        if category and row["category"] != category:
            continue
        meta_summary = _call_meta_summary(row.pop("meta", None))
        if project_id and _call_project_id(row, meta_summary, scope_maps) != project_id:
            continue
        row["context"] = meta_summary
        row["model_label"] = next(
            (label for (_provider, model_id), label in catalog.items() if model_id == row.get("model")),
            row.get("model") or "未记录模型",
        )
        filtered.append(row)
    total = len(filtered)
    start = (page - 1) * page_size
    items = filtered[start:start + page_size]
    aggregate_rows = [row for row in filtered if row["effective_status"] in _FAILED_CALL_STATUSES or "FAILED" in row["effective_status"] or "ERROR" in row["effective_status"]]
    grouped: dict[tuple[str, str, str], dict] = {}
    for row in aggregate_rows:
        ctx = row.get("context") or {}
        root = re.sub(r"\b[0-9a-f]{8,}\b", "#", str(row.get("error") or row["effective_status"]))[:160]
        key = (str(ctx.get("project_id") or ""), str(row.get("kind") or ""), root)
        group = grouped.setdefault(key, {
            "key": "|".join(key), "project_id": ctx.get("project_id"),
            "project_name": ctx.get("project_name") or "上下文未关联",
            "episode_no": ctx.get("episode_no"), "shot_no": ctx.get("shot_no"),
            "kind": row.get("kind"), "root_cause": root, "count": 0,
            "first_ts": row["ts"], "last_ts": row["ts"], "call_ids": [],
            "run_id": row.get("run_id"),
        })
        group["count"] += 1
        group["first_ts"] = min(group["first_ts"], row["ts"])
        group["last_ts"] = max(group["last_ts"], row["ts"])
        group["call_ids"].append(row["id"])
    return {
        "items": items, "total": total, "page": page, "page_size": page_size,
        "page_count": max(1, (total + page_size - 1) // page_size),
        "aggregates": sorted(grouped.values(), key=lambda item: item["last_ts"], reverse=True)[:20],
        "failed_total": len(aggregate_rows),
        "query_ms": round((time.perf_counter() - query_started) * 1000, 2),
        "server_time": time.time(),
    }


def _call_detail_payload(call_id: int) -> dict:
    from app.monitoring import redact_json_text

    row = get_conn().execute("SELECT * FROM provider_calls WHERE id=?", (call_id,)).fetchone()
    if not row:
        raise HTTPException(404, "调用记录不存在")
    item = dict(row)
    item["effective_status"] = _effective_call_status(item)
    item["category"] = _call_category(str(item.get("kind") or ""))
    item["context"] = _call_meta_summary(item.get("meta"))
    item["model_label"] = next(
        (entry.get("label") for entry in _model_catalog() if entry.get("model") == item.get("model")),
        item.get("model") or "未记录模型",
    )
    for field in ("request_json", "response_json", "meta"):
        raw = item.get(field)
        item[f"{field}_size"] = len(raw.encode("utf-8")) if isinstance(raw, str) else 0
        item[field] = redact_json_text(raw, mask_sensitive_content=field == "request_json")
    item["raw_access"] = False
    return item


@router.get("/system/calls/{call_id}")
def call_detail(call_id: int, _session: str = Depends(require_local_session)):
    from app.monitoring import audit, monitor_features

    if not monitor_features()["call_detail_v2"]:
        raise HTTPException(503, "调用详情已由发布开关安全停用")

    item = _call_detail_payload(call_id)
    audit("call_detail_view", "provider_call", str(call_id), "masked")
    return item


@router.get("/system/calls/{call_id}/download", response_class=PlainTextResponse)
def download_call_detail(
    call_id: int,
    _session: str = Depends(require_local_session),
):
    from app.monitoring import audit, monitor_features

    if not monitor_features()["call_detail_v2"]:
        raise HTTPException(503, "调用详情下载已由发布开关安全停用")

    item = _call_detail_payload(call_id)
    audit("call_detail_download", "provider_call", str(call_id), "masked")
    return PlainTextResponse(json.dumps(item, ensure_ascii=False, indent=2), headers={
        "Content-Disposition": f'attachment; filename="provider-call-{call_id}-masked.json"',
    })


@router.get("/system/val422-metrics")
def val422_metrics(limit: int = 500):
    """聚合 VAL-422 指标（provider_calls.kind=val422_metric）。"""
    rows = rows_to_dicts(get_conn().execute(
        """SELECT id, ts, response_preview, meta_json FROM provider_calls
           WHERE kind='val422_metric' ORDER BY id DESC LIMIT ?""",
        (min(limit, 2000),),
    ).fetchall())
    totals: dict[str, int] = {}
    for row in rows:
        meta = {}
        try:
            meta = json.loads(row.get("meta_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            meta = {}
        name = str(meta.get("metric") or "")
        if not name:
            continue
        totals[name] = totals.get(name, 0) + int(meta.get("value") or 1)
    return {"totals": totals, "samples": len(rows)}


@router.get("/system/errors")
def recent_errors(limit: int = 50):
    """最近报错码列表（不含原文/堆栈，只给概览）。凭 id 调下方详情接口查根因。"""
    rows = rows_to_dicts(get_conn().execute(
        """SELECT id, ts, category, category_label, code, is_technical, http_status, action, exc_type
           FROM error_logs ORDER BY ts DESC LIMIT ?""", (min(limit, 200),)).fetchall())
    return rows


@router.get("/system/errors/{error_id}")
def error_detail(error_id: str, _session: str = Depends(require_local_session)):
    """凭错误ID查全文：请求动作上下文 + 原始报错 + 堆栈（需本机会话，Todolist T6）。"""
    from app.monitoring import redact_monitor_value

    row = get_conn().execute("SELECT * FROM error_logs WHERE id=?", (error_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"错误ID不存在：{error_id}")
    item = dict(row)
    if "context_json" in item:
        try:
            ctx = json.loads(item["context_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            ctx = {}
        item["context_json"] = json.dumps(
            redact_monitor_value(ctx, mask_sensitive_content=True), ensure_ascii=False,
        )
    return item


@router.get("/system/jobs")
def jobs_overview(include_all: bool = False):
    conn = get_conn()
    run_statuses = {
        "CREATED": "queued",
        "RUNNING": "running",
        "WAITING_RETRY": "waiting_retry",
        "WAITING_HUMAN": "waiting_human",
        "PAUSED_BUDGET": "paused_budget",
        "PAUSED_EXTERNAL": "paused_external",
        "SUCCEEDED": "succeeded",
        "PARTIAL": "partial",
        "FAILED": "failed",
        "CANCELLED": "cancelled",
    }
    counts: dict[str, int] = {}

    def add_count(status: str, amount: int = 1) -> None:
        counts[status] = counts.get(status, 0) + amount

    def effective_run_status(row: dict) -> str:
        if row.get("recovered_by_run_id"):
            return "recovered"
        linked = row.get("linked_job_status")
        if row.get("status") == "PAUSED_EXTERNAL" and linked == "queued":
            return "recovering"
        if row.get("status") == "PAUSED_EXTERNAL" and linked == "running":
            return "running"
        if (row.get("status") == "WAITING_RETRY"
                and row.get("failure_code") == "SERVICE_RESTART"):
            return "recovering"
        return run_statuses.get(row.get("status"), str(row.get("status") or "").lower())

    # workflow_runs is the authoritative business-task ledger.  Resolve its scope
    # back to project/episode/shot labels so the legacy queue UI can present every
    # Harness workflow, including bible/reference generation that never creates a
    # row in the low-level media jobs table.
    run_recent = rows_to_dicts(conn.execute(
        """SELECT wr.*,
                  CASE
                    WHEN wr.status IN ('PAUSED_EXTERNAL', 'WAITING_RETRY') THEN (
                      SELECT j.status FROM jobs j WHERE j.run_id=wr.id
                      ORDER BY j.updated_at DESC LIMIT 1
                    )
                  END AS linked_job_status,
                  CASE wr.scope_type
                    WHEN 'project' THEN wr.scope_id
                    WHEN 'episode' THEN scope_episode.project_id
                    WHEN 'shot' THEN shot_episode.project_id
                  END AS project_id,
                  COALESCE(project_scope.name, episode_project.name, shot_project.name) AS project_name,
                  COALESCE(scope_episode.id, shot_episode.id) AS episode_id,
                  COALESCE(scope_episode.episode_no, shot_episode.episode_no) AS episode_no,
                  COALESCE(scope_episode.title, shot_episode.title) AS episode_title,
                  scope_shot.id AS shot_id,
                  scope_shot.shot_no AS shot_no
           FROM workflow_runs wr
           LEFT JOIN projects project_scope
             ON wr.scope_type='project' AND project_scope.id=wr.scope_id
           LEFT JOIN episodes scope_episode
             ON wr.scope_type='episode' AND scope_episode.id=wr.scope_id
           LEFT JOIN projects episode_project ON episode_project.id=scope_episode.project_id
           LEFT JOIN shots scope_shot
             ON wr.scope_type='shot' AND scope_shot.id=wr.scope_id
           LEFT JOIN episodes shot_episode ON shot_episode.id=scope_shot.episode_id
           LEFT JOIN projects shot_project ON shot_project.id=shot_episode.project_id
           ORDER BY wr.updated_at DESC""").fetchall())
    for row in run_recent:
        row["source"] = "run"
        row["run_id"] = row["id"]
        row["kind"] = row["workflow_type"]
        row["raw_status"] = row["status"]
        row["status"] = effective_run_status(row)
        row["error"] = (
            "服务重启后已自动重新排队，等待 worker 领取"
            if row["status"] == "recovering" else row.get("failure_message")
        )
    count_rows = rows_to_dicts(conn.execute(
        """SELECT wr.*,
                  CASE
                    WHEN wr.status IN ('PAUSED_EXTERNAL', 'WAITING_RETRY') THEN (
                      SELECT j.status FROM jobs j WHERE j.run_id=wr.id
                      ORDER BY j.updated_at DESC LIMIT 1
                    )
                  END AS linked_job_status
           FROM workflow_runs wr"""
    ).fetchall())
    for row in count_rows:
        add_count(effective_run_status(row))

    # Keep legacy/untraced media jobs visible, but omit jobs already represented
    # by a valid Run so one business task is never counted twice.
    legacy_jobs = rows_to_dicts(conn.execute(
        """SELECT j.*, 'job' AS source, s.shot_no, e.episode_no,
                  e.title AS episode_title, p.name AS project_name
           FROM jobs j LEFT JOIN shots s ON s.id=j.shot_id
           LEFT JOIN episodes e ON e.id=j.episode_id LEFT JOIN projects p ON p.id=j.project_id
           WHERE j.run_id IS NULL
              OR NOT EXISTS (SELECT 1 FROM workflow_runs wr WHERE wr.id=j.run_id)
           ORDER BY j.updated_at DESC""").fetchall())
    for row in conn.execute(
        """SELECT j.status, COUNT(*) c FROM jobs j
           WHERE j.run_id IS NULL
              OR NOT EXISTS (SELECT 1 FROM workflow_runs wr WHERE wr.id=j.run_id)
           GROUP BY j.status"""
    ):
        add_count(row["status"], row["c"])

    # Older screenplay rows predate WorkflowRecorder.  Preserve them only when
    # no screenplay Run exists for the episode.
    screenplay_recent = rows_to_dicts(conn.execute(
        """SELECT 'screenplay_' || e.id AS id, 'screenplay' AS kind, 'screenplay' AS source,
                  e.id AS episode_id, e.project_id, NULL AS shot_id,
                  CASE e.screenplay_status
                    WHEN 'running' THEN 'running'
                    WHEN 'ready' THEN 'succeeded'
                    WHEN 'failed' THEN 'failed'
                    ELSE e.screenplay_status
                  END AS status,
                  e.screenplay_error AS error, e.episode_no, e.title AS episode_title,
                  p.name AS project_name, NULL AS shot_no,
                  COALESCE(e.screenplay_updated_at, e.screenplay_started_at, e.created_at) AS updated_at
           FROM episodes e JOIN projects p ON p.id=e.project_id
           WHERE e.screenplay_started_at IS NOT NULL
             AND NOT EXISTS (
               SELECT 1 FROM workflow_runs wr
               WHERE wr.workflow_type='screenplay'
                 AND wr.scope_type='episode' AND wr.scope_id=e.id
             )
           ORDER BY updated_at DESC""").fetchall())
    for row in screenplay_recent:
        add_count(row["status"])
    all_rows = sorted(
        [*run_recent, *legacy_jobs, *screenplay_recent],
        key=lambda row: row.get("updated_at") or 0,
        reverse=True,
    )
    recent = all_rows if include_all else all_rows[:200]
    from app.recovery import last_report
    return {
        "counts": counts, "recent": recent, "total": len(all_rows),
        "startup_recovery": last_report(), "server_time": time.time(),
    }


@router.get("/system/jobs/query")
def query_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    search: str = "",
    status: str | None = None,
    project_id: str | None = None,
    workflow: str | None = None,
    from_ts: float | None = None,
    to_ts: float | None = None,
    sort: str = "desc",
):
    query_started = time.perf_counter()
    payload = jobs_overview(include_all=True)
    scope_maps = _project_scope_maps() if project_id else None
    # 项目观测台的汇总数字必须和列表使用完全相同的强作用域。旧实现虽然
    # 过滤了 items，却仍返回全局 counts，既误导用户也会泄露其他项目活跃度。
    scoped_rows = [
        row for row in payload["recent"]
        if not project_id or _job_project_id(row, scope_maps) == project_id
    ]
    scoped_counts: dict[str, int] = {}
    for row in scoped_rows:
        row_status = str(row.get("status") or "unknown")
        scoped_counts[row_status] = scoped_counts.get(row_status, 0) + 1
    keyword = search.strip().lower()
    wanted_statuses = {
        item.strip() for item in (status or "").split(",") if item.strip()
    }
    items = []
    for row in scoped_rows:
        if wanted_statuses and row.get("status") not in wanted_statuses:
            continue
        if workflow and (row.get("workflow_type") or row.get("kind")) != workflow:
            continue
        updated_at = float(row.get("updated_at") or 0)
        if from_ts is not None and updated_at < from_ts:
            continue
        if to_ts is not None and updated_at > to_ts:
            continue
        if keyword:
            haystack = " ".join(str(row.get(key) or "") for key in (
                "id", "run_id", "kind", "workflow_type", "scope_type", "scope_id",
                "status", "project_name", "episode_title", "error", "episode_no", "shot_no",
            )).lower()
            if keyword not in haystack:
                continue
        items.append(row)
    items.sort(key=lambda row: float(row.get("updated_at") or 0), reverse=sort.lower() != "asc")
    total = len(items)
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size], "total": total, "page": page,
        "page_size": page_size, "page_count": max(1, (total + page_size - 1) // page_size),
        "counts": scoped_counts, "startup_recovery": payload["startup_recovery"],
        "server_time": payload["server_time"],
        "query_ms": round((time.perf_counter() - query_started) * 1000, 2),
    }


@router.get("/system/jobs/{job_id}")
def job_detail(job_id: str, source: str = "job"):
    summary: dict = {}
    if source == "auto":
        summary = next((row for row in jobs_overview(include_all=True)["recent"] if row.get("id") == job_id), {})
        if not summary:
            raise HTTPException(404, "任务不存在")
        source = str(summary.get("source") or "job")
    if source == "run":
        from app.evidence import repository
        run = repository.get_run(job_id)
        if not run:
            raise HTTPException(404, "任务对应的 Run 不存在")
        return {
            **run, **summary, "source": "run", "run_id": job_id,
            "raw_status": run.get("status"),
            "status": summary.get("status") or {
                "CREATED": "queued", "RUNNING": "running", "WAITING_RETRY": "waiting_retry",
                "WAITING_HUMAN": "waiting_human", "PAUSED_BUDGET": "paused_budget",
                "PAUSED_EXTERNAL": "paused_external", "SUCCEEDED": "succeeded",
                "PARTIAL": "partial", "FAILED": "failed", "CANCELLED": "cancelled",
            }.get(str(run.get("status") or ""), str(run.get("status") or "").lower()),
            "steps": repository.get_steps(job_id), "events": repository.get_events(job_id, limit=100),
        }
    if source == "screenplay" or job_id.startswith("screenplay_"):
        episode_id = job_id.removeprefix("screenplay_")
        row = get_conn().execute(
            """SELECT e.*,p.name AS project_name FROM episodes e JOIN projects p ON p.id=e.project_id
               WHERE e.id=?""", (episode_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "剧本任务不存在")
        return {**dict(row), "source": "screenplay", "id": job_id}
    row = get_conn().execute(
        """SELECT j.*,s.shot_no,e.episode_no,e.title AS episode_title,p.name AS project_name
           FROM jobs j LEFT JOIN shots s ON s.id=j.shot_id
           LEFT JOIN episodes e ON e.id=j.episode_id LEFT JOIN projects p ON p.id=j.project_id
           WHERE j.id=?""", (job_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "媒体任务不存在")
    return {**dict(row), "source": "job"}


@router.post("/system/jobs/{job_id}/retry")
def retry_job(job_id: str, body: dict | None = None):
    """低层媒体 Job 的显式重试/恢复；Run 任务继续使用统一 Run 控制接口。"""
    from app import worker
    from app.monitoring import audit

    request = body or {}
    conn = get_conn()
    row = conn.execute(
        """SELECT j.*, v.provider_task_id, s.duration_s
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
             LEFT JOIN shots s ON s.id=j.shot_id
            WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "媒体任务不存在")
    item = dict(row)
    if item["status"] not in {
        "failed", "cancelled", "paused", "paused_external", "paused_budget", "waiting_retry",
    }:
        raise HTTPException(409, detail={
            "code": "JOB_STATE_CONFLICT", "message": f"当前状态 {item['status']} 不支持重试",
            "current_status": item["status"],
            "retryability": {
                "retryable": False,
                "action": "refresh",
                "message": "任务状态已变化，请刷新后查看最新状态",
            },
        })
    expected = request.get("expected_version")
    if expected is not None and int(expected) != int(item.get("state_revision") or 0):
        raise HTTPException(409, detail={
            "code": "JOB_VERSION_CONFLICT", "message": "任务状态已变化，请刷新后重试",
            "current_version": item.get("state_revision") or 0,
        })
    if item["status"] == "paused_budget":
        if not item.get("episode_id"):
            raise HTTPException(409, "预算暂停任务缺少分集上下文，不能安全恢复")
        resumed = worker.retry_paused(item["episode_id"], job_id=job_id)
        if not resumed:
            raise HTTPException(409, "预算仍不足，请先提高单集成本上限")
        retryability = {
            "retryable": True,
            "action": "resume_budget_paused",
            "paid_risk": "uses_reserved_budget",
            "will_submit_new_provider_task": not bool(item.get("provider_task_id")),
            "will_continue_existing_provider_task": bool(item.get("provider_task_id")),
            "message": "预算已重新校验，任务将从已保存断点继续",
        }
    else:
        has_provider_task = bool(item.get("provider_task_id"))
        provider_recovery = bool(
            item.get("provider_non_cancellable")
            or item.get("provider_create_state") in {"accepted", "submitting", "unknown"}
        )
        provider_recovery_unconfirmed = False
        if not has_provider_task and provider_recovery:
            recovered = worker._recover_paid_video_task(
                conn,
                item.get("provider_operation_id"),
            )
            if recovered and item.get("version_id"):
                provider_task_id, submitted_at = recovered
                conn.execute(
                    "UPDATE shot_versions SET provider_task_id=? WHERE id=?",
                    (provider_task_id, item["version_id"]),
                )
                conn.execute(
                    """UPDATE jobs
                       SET provider_create_state='accepted', provider_non_cancellable=1,
                           provider_submitted_at=?, updated_at=?
                       WHERE id=?""",
                    (submitted_at, time.time(), job_id),
                )
                conn.commit()
                item["provider_task_id"] = provider_task_id
                has_provider_task = True
            elif not request.get("allow_new_submission"):
                raise HTTPException(409, detail={
                    "code": "PROVIDER_HANDLE_UNCONFIRMED",
                    "message": "供应商可能已接单，但暂未找到可继续查询的任务编号",
                    "retryability": {
                        "retryable": True,
                        "action": "confirm_new_submission",
                        "paid_risk": "previous_charge_unknown",
                        "will_submit_new_provider_task": True,
                        "will_continue_existing_provider_task": False,
                        "message": "请先核对供应商后台；确认继续后会重新校验预算，并可能产生新费用",
                    },
                })
            else:
                provider_recovery_unconfirmed = True
        error_text = str(item.get("error") or "").lower()
        provider_terminal_failure = bool(
            has_provider_task
            and item["status"] == "failed"
            and (
                "seedance 任务失败" in error_text
                or ("供应商任务" in error_text and "失败" in error_text)
            )
        )
        if provider_terminal_failure:
            raise HTTPException(409, detail={
                "code": "PROVIDER_TASK_TERMINAL_FAILED",
                "message": "供应商任务已明确失败，不能继续轮询同一任务，请创建新视频版本",
                "retryability": {
                    "retryable": False,
                    "action": "create_new_version",
                    "paid_risk": "requires_new_charge",
                    "message": "新版本会创建新的供应商任务，并可能产生新费用",
                },
            })
        if has_provider_task:
            target_status = "waiting_provider"
            retryability = {
                "retryable": True,
                "action": "continue_poll",
                "paid_risk": "no_new_charge",
                "will_submit_new_provider_task": False,
                "will_continue_existing_provider_task": True,
                "message": "将继续查询同一个供应商任务，不会重复提交或产生新任务",
            }
        elif provider_recovery_unconfirmed:
            target_status = "queued"
            if not item.get("episode_id"):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_CONTEXT_MISSING",
                    "message": "任务缺少分集上下文，不能安全重新提交",
                })
            from app.compiler import shot_cost_cny
            estimate = float(item.get("reserved_cost_cny") or 0)
            if estimate <= 0:
                estimate = shot_cost_cny(float(item.get("duration_s") or 5))
            if not worker.media_scheduler.reserve_budget(
                job_id,
                item["episode_id"],
                estimate,
                float(get_setting("episode_cost_limit_cny") or 100),
                conn=conn,
            ):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_BUDGET_BLOCKED",
                    "message": "单集预算不足，任务已保持暂停；提高成本上限后可继续",
                    "retryability": {
                        "retryable": True,
                        "action": "increase_budget",
                        "paid_risk": "blocked_before_charge",
                    },
                })
            retryability = {
                "retryable": True,
                "action": "new_submission_after_unconfirmed_provider",
                "paid_risk": "may_create_new_charge",
                "will_submit_new_provider_task": True,
                "will_continue_existing_provider_task": False,
                "message": "已确认继续并重新校验预算；将复用原幂等标识，但仍可能产生新费用",
            }
        else:
            target_status = "queued"
            if not item.get("episode_id"):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_CONTEXT_MISSING",
                    "message": "任务缺少分集上下文，不能安全重新提交",
                })
            from app.compiler import shot_cost_cny
            estimate = float(item.get("reserved_cost_cny") or 0)
            if estimate <= 0:
                estimate = shot_cost_cny(float(item.get("duration_s") or 5))
            if not worker.media_scheduler.reserve_budget(
                job_id,
                item["episode_id"],
                estimate,
                float(get_setting("episode_cost_limit_cny") or 100),
                conn=conn,
            ):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_BUDGET_BLOCKED",
                    "message": "单集预算不足，任务已保持暂停；提高成本上限后可继续",
                    "retryability": {
                        "retryable": True,
                        "action": "increase_budget",
                        "paid_risk": "blocked_before_charge",
                    },
                })
            retryability = {
                "retryable": True,
                "action": "new_submission",
                "paid_risk": "may_create_new_charge",
                "will_submit_new_provider_task": True,
                "will_continue_existing_provider_task": False,
                "message": "该任务尚无供应商断点，将重新提交并可能产生新费用",
            }
        cursor = conn.execute(
            """UPDATE jobs SET status=?,error=?,next_retry_at=NULL,
                       cancellation_requested=0,lease_owner=NULL,lease_expires_at=NULL,
                       state_revision=COALESCE(state_revision,0)+1,updated_at=?
               WHERE id=? AND status=?""",
            (
                target_status,
                retryability["message"],
                time.time(),
                job_id,
                item["status"],
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise HTTPException(409, "任务已被其他操作更新")
        conn.commit()
        dispatch_deferred = False
        try:
            worker._enqueue_for_current_status(job_id)
        except Exception as exc:
            from app import errors as app_errors
            app_errors.record_and_format(
                exc,
                action="manual_job_retry_dispatch",
                context={"job_id": job_id, "target_status": target_status},
            )
            dispatch_deferred = True
    latest = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    audit(
        "job_retry", "job", job_id, "accepted",
        {"previous_status": item["status"], "retry_action": retryability["action"]},
    )
    return {
        "ok": True,
        "accepted": True,
        "asynchronous": True,
        "retryability": retryability,
        "job": latest,
        "dispatch_deferred": locals().get("dispatch_deferred", False),
    }


def _redact_settings_values(values: dict[str, Any]) -> dict[str, Any]:
    """永远剥离 api_key / model_credentials 明文（Todolist T2）。"""
    public: dict[str, Any] = {}
    for key, raw in values.items():
        if key == "model_credentials":
            public[key] = "{}"
            continue
        if key == "custom_models":
            try:
                items = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                public[key] = "[]"
                continue
            if isinstance(items, list):
                public[key] = json.dumps(
                    [{k: v for k, v in item.items() if k != "api_key"}
                     if isinstance(item, dict) else item for item in items],
                    ensure_ascii=False,
                )
            else:
                public[key] = "[]"
            continue
        lowered = key.lower()
        if "api_key" in lowered or "secret" in lowered or lowered.endswith("_token") or lowered == "token":
            public[key] = "***" if raw else ""
            continue
        public[key] = raw
    return public


@router.get("/settings")
def get_settings(include_schema: bool = False):
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    values = {r["key"]: r["value"] for r in rows}
    if not include_schema:
        return _redact_settings_values(values)
    from app.monitoring import (
        SETTINGS_SCHEMA,
        monitor_features,
        normalize_setting,
        public_settings_schema,
    )

    issues = []
    for key, spec in SETTINGS_SCHEMA.items():
        if key not in values:
            continue
        try:
            normalize_setting(key, values[key])
        except HTTPException as exc:
            issues.append({"field": key, "message": exc.detail})
    version = int(values.get("_monitor_config_version") or 0)
    public_values = {key: values.get(key, str(spec.get("default", ""))) for key, spec in SETTINGS_SCHEMA.items()}
    public_values = _redact_settings_values(public_values)
    effective_values = {
        key: (
            values.get(f"_monitor_effective_{key}", public_values.get(key, ""))
            if not spec.get("immediate", True) else public_values.get(key, "")
        )
        for key, spec in SETTINGS_SCHEMA.items()
    }
    effective_values = _redact_settings_values(effective_values)
    return {
        "values": public_values, "effective": effective_values,
        "schema": public_settings_schema(), "version": version,
        "health": "invalid" if issues else "ok", "issues": issues,
        "server_time": time.time(), "features": monitor_features(),
    }


@router.put("/settings")
async def put_settings_route(body: dict):
    from app.capabilities.dispatch import dispatch, respond_ui
    from app.local_session import get_request_session_id

    session_id = get_request_session_id()
    result = await dispatch(
        "system.update_settings", {"patch": body}, initiator="ui", session_id=session_id,
    )
    return respond_ui(result, session_id=session_id)


def put_settings(body: dict):
    from app.monitoring import (
        SETTINGS_SCHEMA,
        audit,
        monitor_features,
        validate_settings_patch,
    )

    if not monitor_features()["settings_edit_v2"]:
        raise HTTPException(503, "设置编辑已由发布开关切为只读")

    request = body if isinstance(body, dict) else {}
    patch = request.get("patch") if isinstance(request.get("patch"), dict) else request
    expected_version = request.get("version") if patch is not request else request.pop("_version", None)
    conn = get_conn()
    current = {row["key"]: row["value"] for row in conn.execute("SELECT key,value FROM settings")}
    normalized = validate_settings_patch(dict(patch), current)
    # model_route is a legacy shorthand; preserve its historical coupled update in the same transaction.
    if "model_route" in normalized:
        normalized.setdefault("model_text_provider", normalized["model_route"])
        normalized.setdefault("model_vlm_provider", normalized["model_route"])
    if any(key.startswith("model_") or "_model_" in key for key in normalized):
        try:
            saved_credentials = json.loads(current.get("model_credentials") or "{}")
        except (TypeError, json.JSONDecodeError):
            saved_credentials = {}
        provider_keys = {
            "hiagent": bool(config.HIAGENT_API_KEY), "openrouter": bool(config.OPENROUTER_API_KEY),
            "bailian": bool(config.BAILIAN_API_KEY), "deepseek": bool(config.DEEPSEEK_API_KEY),
            "zhipu": bool(config.ZHIPU_API_KEY),
        }
        for kind in ("text", "vlm", "video", "image"):
            provider_field = f"model_{kind}_provider"
            if provider_field not in normalized and not any(key.endswith(f"_model_{kind}") for key in normalized):
                continue
            provider = normalized.get(provider_field, current.get(provider_field, "hiagent"))
            if provider.startswith("custom:"):
                target = next((item for item in _model_catalog()
                               if item.get("provider") == provider and kind in item.get("kinds", [])), None)
            else:
                model_field = f"{provider}_model_{kind}"
                model_id = normalized.get(model_field, current.get(model_field, ""))
                target = next((item for item in _model_catalog()
                               if item.get("provider") == provider and item.get("model") == model_id
                               and kind in item.get("kinds", [])), None)
            if not target:
                raise HTTPException(422, detail={"field": provider_field, "message": "目标模型不存在或不支持该能力"})
            configured = bool(
                target.get("api_key")
                or (saved_credentials.get(target.get("id"), {}) if isinstance(saved_credentials, dict) else {}).get("api_key")
                or provider_keys.get(provider)
            )
            if not configured:
                raise HTTPException(422, detail={"field": provider_field, "message": "目标模型连接尚未配置并通过测试"})
    current_version = int(current.get("_monitor_config_version") or 0)
    if expected_version is not None and int(expected_version) != current_version:
        raise HTTPException(409, detail={
            "code": "SETTINGS_VERSION_CONFLICT", "message": "设置已被其他会话更新，请重新核对差异",
            "expected_version": expected_version, "current_version": current_version,
        })
    changed = {key: value for key, value in normalized.items() if current.get(key, str(SETTINGS_SCHEMA[key].get("default", ""))) != value}
    if not changed:
        return {"ok": True, "version": current_version, "items": [], "runtime_reload_ok": True}
    conn.execute("BEGIN IMMEDIATE")
    try:
        authoritative_version = int((conn.execute(
            "SELECT value FROM settings WHERE key='_monitor_config_version'"
        ).fetchone() or {"value": 0})["value"])
        if authoritative_version != current_version:
            raise HTTPException(409, detail={
                "code": "SETTINGS_VERSION_CONFLICT", "message": "设置版本已变化，请重新加载",
                "current_version": authoritative_version,
            })
        # Restart-only fields retain a separate runtime-effective snapshot.  init_db
        # advances that snapshot on the next process start; a successful save does not
        # pretend the new persisted value is already active.
        for key in changed:
            if not SETTINGS_SCHEMA[key].get("immediate", True):
                conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                    (f"_monitor_effective_{key}", current.get(key, str(SETTINGS_SCHEMA[key].get("default", "")))),
                )
        conn.executemany(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(changed.items()),
        )
        from app.media_pipeline.concurrency import (
            SETTING_KEYS, reload_limits_from_settings,
        )
        from app import worker
        immediate_changed = [key for key in changed if SETTINGS_SCHEMA[key].get("immediate", True)]
        if immediate_changed:
            reload_limits_from_settings()
        # 三通道 worker 分别跟随各自并发配置热更新
        if any(k in changed for k in (*SETTING_KEYS.values(), "video_concurrency", "auto_concurrency",
                                   "media_scheduler_policy", "video_ready_low_watermark",
                                   "video_ready_high_watermark", "reference_shot_cohort_limit")):
            worker.ensure_workers()
        new_version = current_version + 1
        conn.execute(
            "INSERT INTO settings(key,value) VALUES('_monitor_config_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(new_version),),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 必须回滚持久值，并把运行时恢复到旧配置
        conn.rollback()
        try:
            from app.media_pipeline.concurrency import reload_limits_from_settings
            reload_limits_from_settings()
        except Exception:
            pass
        audit("settings_update", "settings", str(current_version), "rolled_back", {"error_type": type(exc).__name__})
        raise HTTPException(503, detail={
            "code": "SETTINGS_RUNTIME_APPLY_FAILED",
            "message": "运行时应用失败，全部设置已回滚；草稿可修正后重试",
        }) from exc
    items = [{
        "key": key, "requested": value,
        "effective": value if SETTINGS_SCHEMA[key].get("immediate", True) else current.get(key, SETTINGS_SCHEMA[key].get("default", "")),
        "apply_mode": "immediate" if SETTINGS_SCHEMA[key].get("immediate", True) else "restart",
    } for key, value in changed.items()]
    audit("settings_update", "settings", str(new_version), "succeeded", {"fields": sorted(changed)})
    return {
        "ok": True, "runtime_reload_ok": True, "version": new_version, "items": items,
        "effect_scope": {
            "new_tasks": True, "queued_not_started": True, "running_tasks": False,
            "source": "server_policy_v1",
        } if any(key.startswith("model_") or "_model_" in key for key in changed) else None,
    }


# ---------- API Key 管理：前端填写 → 持久化 .env ----------

@router.get("/keys")
def get_keys():
    """获取各 provider 的 key 状态（不返回完整 key 值）。"""
    return config.get_key_status()


@router.put("/keys")
def put_keys(body: dict):
    """保存 API Key 到 .env 并热更新运行时变量。

    body 格式：{"confirm": true, "hiagent": "sk-xxx", ...}
    前端传 provider 名（小写），后端映射到对应的环境变量名。
    写入需 ``confirm=true`` 二次确认（Todolist T2）。
    """
    if body.get("confirm") is not True:
        raise HTTPException(422, "写入 API Key 需 confirm=true 二次确认")
    provider_to_key = {
        "hiagent": "HIAGENT_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "bailian": "BAILIAN_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "zhipu": "ZHIPU_API_KEY",
    }
    env_keys: dict[str, str] = {}
    for provider, value in body.items():
        p = str(provider).strip().lower()
        if p == "confirm":
            continue
        if p not in provider_to_key:
            raise HTTPException(422, f"不支持的 provider：{p}，可选：{', '.join(provider_to_key)}")
        env_keys[provider_to_key[p]] = str(value).strip()

    updated = config.save_keys_to_env(env_keys)
    if not updated:
        raise HTTPException(422, "没有提供有效的 Key")
    updated_providers = [k.replace("_API_KEY", "").lower() for k in updated]
    return {"ok": True, "updated": updated_providers}


# ---------- MCP 对外接入：Token 管理（PRD AGENT_MCP_CAPABILITY §9.6） ----------
#
# 有意不通过 Capability Registry / Command Bus：这是签发/撤销 MCP 访问凭证本身的
# 运维端点，绝不能被 Agent/外部 MCP 客户端自己调用去给自己签发更高权限的 token。
# 覆盖扫描豁免见 app/capabilities/catalog.py 的 rest_exemptions。

@router.post("/system/mcp-tokens")
def create_mcp_token(body: dict):
    """创建一枚新 MCP token；明文只在这次响应里返回一次。"""
    from app.mcp import auth as mcp_auth

    scopes = body.get("scopes")
    if scopes is not None:
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise HTTPException(422, "scopes 必须是字符串数组")
    ttl_s = body.get("ttl_s")
    if ttl_s is not None:
        try:
            ttl_s = int(ttl_s)
        except (TypeError, ValueError):
            raise HTTPException(422, "ttl_s 必须是整数秒数") from None
    name = str(body.get("name") or "").strip()[:80] or None
    try:
        token, record = mcp_auth.create_token(scopes=scopes, ttl_s=ttl_s, name=name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"token": token, **record}


@router.get("/system/mcp-tokens")
def list_mcp_tokens():
    """列出已创建的 MCP token（脱敏：不返回明文/hash）。"""
    from app.mcp import auth as mcp_auth

    return {"items": mcp_auth.list_tokens()}


@router.delete("/system/mcp-tokens/{token_id}")
def delete_mcp_token(token_id: str):
    from app.mcp import auth as mcp_auth

    if not mcp_auth.revoke_token(token_id):
        raise HTTPException(404, "token 不存在")
    return {"ok": True}
