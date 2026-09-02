"""System, settings, model-catalog, credentials, and filesystem API routes."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sqlite3
import string
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from app.auth.deps import require_system_admin
from fastapi.responses import PlainTextResponse

from app import config
from app.db import get_conn, get_setting, new_id, rows_to_dicts, set_setting
from app.local_session import require_local_session
from app.model_capabilities import (
    apply_token_limit_defaults,
    extract_provider_token_limits,
    merge_token_capability_override,
    normalize_token_limits,
)

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
# 自建服务商可以声明的能力。视频/图像要额外声明 protocol：它们没有统一协议，
# 只能复用已实现的那几套。
CUSTOM_PROVIDER_KINDS = {"text", "vlm", "video", "image"}
MODEL_PROVIDER_KINDS = {
    "hiagent": MODEL_KINDS,
    "minimax_h3": {"video"},
    "openrouter": {"text", "vlm"},
    "bailian": {"text", "vlm"},
    "deepseek": {"text"},
    "zhipu": {"text"},
}
# 代码里不再内嵌任何模型：模型库（custom_models）是唯一来源，页面上的每一条都是
# 通过「添加模型」进来的。历史内嵌模型由 app/model_migration.py 一次性搬入。


def probe_kind(kinds: list[str] | set[str] | None) -> str:
    """把「模型能力」翻译成连接测试要走的探测方式。

    媒体模型只查目录（不出片、不产生费用），文本/视觉理解才真发一次补全。
    两条测试路径（草稿态 /models/test 与已保存的 /models/{id}/test）共用这里，
    避免再次出现"页面上勾了图像、后端却按文本去探测"的错配。
    """
    selected = list(kinds or ["text"])
    if "video" in selected:
        return "video"
    if "image" in selected:
        return "image"
    if "vlm" in selected and "text" not in selected:
        return "vlm"
    return "text"


def media_protocol_options(kinds: list[str] | set[str]) -> set[str]:
    """某组能力可选的接入协议。

    协议清单来自各自的注册表，而不是这里再抄一份：新实现一套协议只要在注册表
    里登记，页面上就能选到。
    """
    from app import image_providers, text_providers, video_providers

    options: set[str] = set()
    if "video" in kinds:
        options |= set(video_providers.VIDEO_PROTOCOLS)
    if "image" in kinds:
        options |= set(image_providers.IMAGE_PROTOCOLS)
    if "text" in kinds or "vlm" in kinds:
        options |= set(text_providers.TEXT_PROTOCOLS)
    return options


def _custom_models() -> list[dict]:
    try:
        value = json.loads(get_setting("custom_models") or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _token_capability_overrides() -> dict[str, dict]:
    try:
        value = json.loads(get_setting("model_token_capabilities") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _model_catalog() -> list[dict]:
    overrides = _token_capability_overrides()
    return [
        apply_token_limit_defaults(
            merge_token_capability_override(
                item, overrides.get(str(item.get("id") or ""), {})
            )
        )
        for item in _custom_models() if isinstance(item, dict)
    ]


def _public_model(item: dict) -> dict:
    from app.system_health import env_key_for_item

    public = {key: value for key, value in item.items() if key != "api_key"}
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, json.JSONDecodeError):
        credentials = {}
    # 按网关归族兜底查环境变量密钥（app.system_health.env_key_for_item，与
    # health() 同一份 family 映射），不再用按 provider 字面量的独立字典。
    provider_key = env_key_for_item(item)
    public["key_configured"] = (
        bool(item.get("base_url"))
        if item.get("requires_api_key") is False
        else bool(
            item.get("api_key")
            or credentials.get(item.get("id"), {}).get("api_key")
            or provider_key
        )
    )
    return public


@router.get("/models")
def get_models():
    from app import image_providers, text_providers, video_providers

    return {
        "items": [_public_model(item) for item in _model_catalog()],
        # 页面据此渲染"接入协议"下拉；清单只有一份，避免前后端各抄一遍后漂移。
        "media_protocols": {
            "video": sorted(video_providers.VIDEO_PROTOCOLS),
            "image": sorted(image_providers.IMAGE_PROTOCOLS),
            "text": sorted(text_providers.TEXT_PROTOCOLS),
            "vlm": sorted(text_providers.TEXT_PROTOCOLS),
        },
    }


@router.post("/models")
async def add_model_route(body: dict):
    from app.capabilities.dispatch import ui_route
    prepared = await prepare_model_token_capabilities(body)
    routed = await ui_route("system.model_create", {"model": prepared})
    if routed is not None:
        return routed
    return add_model(prepared)


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
    allowed_kinds = CUSTOM_PROVIDER_KINDS if custom_provider else MODEL_PROVIDER_KINDS[provider]
    if not kinds or any(k not in allowed_kinds for k in kinds):
        raise HTTPException(422, "所选服务商不支持该模型能力")
    # 每条模型都要声明走哪套接入协议。代码里只有协议实现，没有模型；
    # 声明清楚了，同一协议下换服务就只是改地址和密钥，不必再改代码。
    protocol = str(body.get("protocol") or "").strip().lower()
    available = media_protocol_options(kinds)
    if protocol not in available:
        raise HTTPException(422, detail={
            "field": "protocol",
            "message": f"必须声明接入协议，可选：{', '.join(sorted(available))}",
        })
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
    item["protocol"] = protocol
    params = body.get("params")
    if isinstance(params, dict) and params:
        item["params"] = params
    item.update(normalize_token_limits(body))
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
    token_limits = normalize_token_limits({})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client:
            headers = {"Authorization": f"Bearer {api_key.strip()}"}
            if kind in {"video", "image"}:
                response = await client.get(f"{base_url}/models", headers=headers)
                if response.is_success:
                    try:
                        catalog = response.json()
                        model_ids = {
                            str(item.get("id") or "").strip()
                            for item in (catalog.get("data") or [])
                            if isinstance(item, dict)
                        }
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise HTTPException(
                            422,
                            "模型目录已响应，但返回格式不是 OpenAI models 兼容格式",
                        ) from exc
                    if model.strip() not in model_ids:
                        raise HTTPException(422, "模型目录中没有该媒体模型 ID")
            else:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    headers={**headers, "Content-Type": "application/json"},
                    json={"model": model.strip(), "messages": [{"role": "user", "content": "Reply with OK only."}], "max_tokens": 8, "temperature": 0},
                )
            if response.is_success and kind in {"text", "vlm"}:
                token_limits = await _discover_model_token_capabilities_with_client(
                    client, base_url, api_key, model,
                )
    except httpx.HTTPError as exc:
        raise HTTPException(422, f"连接失败：{type(exc).__name__}，请检查 Base URL 和网络") from exc
    latency_ms = int((time.perf_counter() - started) * 1000)
    if not response.is_success:
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
    if kind in {"video", "image"}:
        return {
            "ok": True, "latency_ms": latency_ms, "probe": "model_catalog",
            "preview": "凭证与模型目录识别通过；为避免产生费用，未执行媒体生成",
            **normalize_token_limits({}),
        }
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(422, "服务已响应，但返回格式不是 OpenAI chat/completions 兼容格式") from exc
    return {
        "ok": True,
        "latency_ms": latency_ms,
        "preview": str(content or "")[:80],
        **token_limits,
    }


async def _discover_model_token_capabilities_with_client(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
) -> dict:
    """低成本读取供应商模型元数据；不支持 /models 时回退 128K/32K。"""
    fallback = normalize_token_limits({})
    get = getattr(client, "get", None)
    if get is None:
        return fallback
    try:
        response = await get(
            f"{base_url.strip().rstrip('/')}/models",
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
        )
        if not response.is_success:
            return fallback
        detected = extract_provider_token_limits(response.json(), model)
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return fallback
    return normalize_token_limits(detected)


async def prepare_model_token_capabilities(body: dict) -> dict:
    """新增/编辑模型时自动补齐 token 能力，显式测试结果优先。"""
    prepared = dict(body or {})
    if prepared.get("context_window_tokens") or prepared.get("max_output_tokens"):
        prepared.update(normalize_token_limits(prepared))
        return prepared
    base_url = str(prepared.get("base_url") or "").strip().rstrip("/")
    api_key = str(prepared.get("api_key") or "").strip()
    model = str(prepared.get("model") or "").strip()
    kinds = prepared.get("kinds") or []
    if not (base_url and api_key and model and any(kind in {"text", "vlm"} for kind in kinds)):
        prepared.update(normalize_token_limits({}))
        return prepared
    try:
        _assert_public_http_url(base_url)
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5)) as client:
            limits = await _discover_model_token_capabilities_with_client(
                client, base_url, api_key, model,
            )
    except (HTTPException, httpx.HTTPError):
        limits = normalize_token_limits({})
    prepared.update(limits)
    return prepared


@router.post("/models/test")
async def test_model_connection(body: dict):
    from app.capabilities.dispatch import ui_route
    routed = await ui_route("system.model_test", {"draft": body})
    if routed is not None:
        return routed
    protocol = str(body.get("protocol") or "").strip().lower()
    if protocol == "minimax_h3":
        # H3 不是 OpenAI 兼容接口，用它自己的能力探测：这样"测试连接"验的是
        # 真实出片前提（模式/加速档/VAE 是否就绪），而不只是端口通不通。
        from app import hiagent, minimax_h3

        try:
            return {
                **await minimax_h3.probe_connection(
                    str(body.get("base_url") or ""),
                    minimax_h3.connection_from_catalog_item(
                        body,
                        base_url_override=str(body.get("base_url") or ""),
                        api_key_override=str(body.get("api_key") or ""),
                    ),
                ),
                **normalize_token_limits({}),
            }
        except hiagent.ProviderError as exc:
            raise HTTPException(422, f"模型测试失败：{exc}") from exc
    return await _probe_openai_model(
        str(body.get("base_url") or ""), str(body.get("api_key") or ""),
        str(body.get("model") or ""),
        str(body.get("kind") or "") or probe_kind(body.get("kinds")))


@router.put("/models/{model_id}")
async def update_model_route(model_id: str, body: dict):
    from app.capabilities.dispatch import ui_route
    current = next((item for item in _custom_models() if item.get("id") == model_id), {})
    probe_body = {**current, **body}
    if not str(body.get("api_key") or "").strip() and current.get("api_key"):
        probe_body["api_key"] = current["api_key"]
    prepared = await prepare_model_token_capabilities(probe_body)
    patch = {**body, **{
        key: prepared[key]
        for key in ("context_window_tokens", "max_output_tokens", "token_limits_source")
        if key in prepared
    }}
    routed = await ui_route("system.model_update", {"model_id": model_id, "patch": patch})
    if routed is not None:
        return routed
    return update_model(model_id, patch)


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
    if any(key in body for key in ("context_window_tokens", "max_output_tokens", "token_limits_source")):
        item.update(normalize_token_limits({**item, **body}))
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
    provider = str(item.get("provider") or "")
    uses_h3 = provider == "minimax_h3" or (
        provider.startswith("custom:")
        and str(item.get("protocol") or "") == "minimax_h3"
    )
    if uses_h3:
        from app import hiagent, minimax_h3
        from app.video_plan import record_minimax_h3_probe_snapshot

        connection = (
            minimax_h3.connection_from_catalog_item(
                item,
                base_url_override=str(override.get("base_url") or ""),
                api_key_override=str(override.get("api_key") or ""),
            )
            if provider != "minimax_h3"
            else minimax_h3.default_connection()
        )
        try:
            result = await minimax_h3.probe_connection(
                str(override.get("base_url") or item.get("base_url") or ""),
                connection,
            )
        except hiagent.ProviderError as exc:
            raise HTTPException(422, f"模型测试失败：{exc}") from exc
        response = {**result, **normalize_token_limits({})}
        if str(result.get("base_url") or "").rstrip("/") == connection.base_url:
            snapshot = record_minimax_h3_probe_snapshot(
                result,
                provider=provider,
                model=str(
                    item.get("model")
                    or config.DEFAULT_MINIMAX_H3_MODEL_VIDEO
                ),
            )
            response["capability_snapshot_id"] = snapshot.id
        return response
    try:
        credentials = json.loads(get_setting("model_credentials") or "{}")
    except (TypeError, json.JSONDecodeError):
        credentials = {}
    saved = credentials.get(model_id, {}) if isinstance(credentials, dict) else {}
    base_url = str(override.get("base_url") or saved.get("base_url") or item.get("base_url") or "")
    api_key = str(override.get("api_key") or saved.get("api_key") or item.get("api_key") or "")
    model = str(override.get("model") or item.get("model") or "")
    kind = probe_kind(item.get("kinds"))
    result = await _probe_openai_model(base_url, api_key, model, kind)
    if kind in {"text", "vlm"}:
        overrides = _token_capability_overrides()
        overrides[model_id] = normalize_token_limits(result)
        set_setting("model_token_capabilities", json.dumps(overrides, ensure_ascii=False))
    return result


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
    overrides = _token_capability_overrides()
    if model_id in overrides:
        overrides.pop(model_id, None)
        set_setting("model_token_capabilities", json.dumps(overrides, ensure_ascii=False))
    return {"ok": True}


@router.put("/models/{model_id}/credentials")
def put_model_credentials(model_id: str, body: dict, _admin: None = Depends(require_system_admin)):
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
    from app import config, hiagent, system_health

    def option(provider: str, model: str, available: bool = True) -> dict:
        return {"provider": provider, "model": model, "available": available}

    def selected(kind: str, label: str, options: list[dict]) -> dict:
        provider = hiagent.active_provider(kind)
        # 模型库可能一条都没有（全新部署）：这时如实报"未配置"，
        # 不能拿 options[0] 去索引一个空列表。
        active = next(
            (o for o in options if o["provider"] == provider),
            options[0] if options else {"provider": "", "model": ""},
        )
        return {
            "key": kind,
            "label": label,
            "provider": active["provider"],
            "model": active["model"],
            "options": options,
        }

    def catalog_options(kind: str) -> list[dict]:
        return [
            option(
                item["provider"], item["model"],
                bool(_public_model(item).get("key_configured")),
            )
            for item in _model_catalog()
            if kind in (item.get("kinds") or [])
        ]

    # 选项全部来自模型库：代码里不再有内嵌 provider，下拉里也就不会再出现
    # "同一家服务出现两次（一条来自代码、一条来自页面）"这种没法解释的情况。
    models = {
        kind: selected(kind, label, catalog_options(kind))
        for kind, label in (
            ("text", "Text 模型"),
            ("vlm", "VLM 模型"),
            ("video", "视频模型"),
            ("image", "图像模型"),
        )
    }
    credentials = system_health.credential_report(
        _model_catalog(),
        item_key_configured=lambda item: bool(_public_model(item).get("key_configured")),
        active_provider=hiagent.active_provider,
    )
    return {
        "ok": True,
        "gateway": config.HIAGENT_BASE_URL,
        "model_route": get_setting("model_route") or "hiagent",
        **credentials,
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
                  recovery_disposition,first_chunk_at,last_chunk_at,received_chars,meta
           FROM provider_calls ORDER BY id DESC LIMIT ?""",
        (min(limit, 200),),
    ).fetchall())
    for row in rows:
        row["effective_status"] = _effective_call_status(row)
        row["context"] = _call_meta_summary(row.pop("meta", None))
    return rows


_BUSINESS_CALL_KINDS = {
    "chat", "video_create", "video_poll", "image", "image_generate",
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
        "stage_key", "substage", "shard_id", "shard_count",
        "format_attempt", "semantic_attempt", "source_count", "scene_count",
        "unit_count", "input_chars", "output_chars", "local_recovery",
        "normalized_artifact_id", "reuse_successful_operation",
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
    if project_id:
        clauses.append("project_id=?")
        params.append(project_id)
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
                    recovery_disposition,first_chunk_at,last_chunk_at,received_chars,meta
             FROM provider_calls {where} ORDER BY id {order}""",
        params,
    ).fetchall())
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

    def _resolve_recovery_tail(start_id: str, chain_lookup: dict) -> dict:
        """Follow recovered_by_run_id hops to the chain's terminal run.

        A run interrupted mid-flight may be superseded by a *chain* of
        continuation runs — each service restart appends one more hop
        (docs/HARNESS_RUNBOOK.md: history is immutable, recovery only
        appends new attempts). An ancestor's displayed status must track the
        chain's *final* outcome, not just its immediate successor, otherwise
        an ancestor whose direct child was itself later superseded again
        gets stuck forever showing an intermediate "still recovering" state.
        `recovered_by_run_id` must never cycle by construction, but this
        still guards against a cycle or a dangling id so malformed data can
        never hang the request — traversal simply stops at the last
        resolvable hop instead.
        """
        visited = {start_id}
        current = chain_lookup.get(start_id)
        tail = current
        while current and current.get("recovered_by_run_id"):
            next_id = current["recovered_by_run_id"]
            if next_id in visited:
                break
            nxt = chain_lookup.get(next_id)
            if nxt is None:
                break
            visited.add(next_id)
            current = nxt
            tail = current
        return tail or {}

    def _superseded_message(successor_id: str, tail: dict) -> str:
        tail_id = tail.get("id") or successor_id
        tail_status = str(tail.get("status") or "").upper()
        status_word = {
            "RUNNING": "仍在运行",
            "CREATED": "已排队等待执行",
            "PAUSED_EXTERNAL": "自身也被中断（接管链尚未解析到终态）",
            "": "状态未知（接管链未能解析，请核对数据）",
        }.get(tail_status, tail_status.lower())
        if tail_id == successor_id:
            return f"历史记录：本次尝试已被 run {successor_id} 接管（{status_word}），本记录不会再被 worker 领取"
        return (
            f"历史记录：本次尝试已被 run {successor_id} 接管，经多次续跑后当前由 "
            f"run {tail_id} 继续（{status_word}），本记录不会再被 worker 领取"
        )

    def effective_run_status(row: dict, tail: dict | None = None) -> str:
        if row.get("recovered_by_run_id"):
            recovered_status = str((tail or {}).get("status") or "").upper()
            if recovered_status == "SUCCEEDED":
                return "recovered"
            if recovered_status in {"CREATED", "RUNNING", "PAUSED_EXTERNAL", ""}:
                # Superseded: this row itself is inert history. It is not
                # "recovering" — nothing will ever pick it up again, its
                # successor chain is simply not finished yet.
                return "superseded"
            return run_statuses.get(
                recovered_status,
                recovered_status.lower(),
            )
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
    # Snapshot of every run's *raw* status/link before the loop below starts
    # overwriting row["status"] with its projected value. Chain resolution
    # must always walk original database values — never an already-projected
    # one — otherwise resolving row A's chain could read row B's status after
    # B has already been rewritten earlier in the same loop.
    chain_lookup = {
        row["id"]: {
            "id": row["id"],
            "status": row["status"],
            "recovered_by_run_id": row.get("recovered_by_run_id"),
            "failure_message": row.get("failure_message"),
        }
        for row in run_recent
    }
    for row in run_recent:
        row["source"] = "run"
        row["run_id"] = row["id"]
        row["kind"] = row["workflow_type"]
        row["raw_status"] = row["status"]
        tail = (
            _resolve_recovery_tail(row["id"], chain_lookup)
            if row.get("recovered_by_run_id") else None
        )
        row["status"] = effective_run_status(row, tail)
        if tail and tail.get("id") and tail.get("id") != row.get("recovered_by_run_id"):
            row["recovered_tail_run_id"] = tail.get("id")
        if row["status"] == "recovering":
            row["error"] = "服务重启后已自动重新排队，等待 worker 领取"
        elif row["status"] == "superseded":
            row["error"] = _superseded_message(row.get("recovered_by_run_id") or "", tail or {})
        elif (
            row.get("recovered_by_run_id")
            and row["status"] in {"failed", "cancelled", "partial"}
        ):
            row["error"] = (tail or {}).get("failure_message")
        else:
            row["error"] = row.get("failure_message")
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
        tail = (
            _resolve_recovery_tail(row["id"], chain_lookup)
            if row.get("recovered_by_run_id") else None
        )
        add_count(effective_run_status(row, tail))

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
    result = {**dict(row), "source": "job"}
    if row["kind"] == "video":
        try:
            from app.video_plan import mode_audit_for_job
            result["video_mode_audit"] = mode_audit_for_job(job_id)
        except Exception:  # noqa: BLE001 - monitoring enrichment must not hide the task
            result["video_mode_audit"] = None
    return result


@router.post("/system/jobs/{job_id}/retry")
def retry_job(job_id: str, body: dict | None = None, _admin: None = Depends(require_system_admin)):
    """低层媒体 Job 的显式重试/恢复；Run 任务继续使用统一 Run 控制接口。"""
    from app import worker
    from app.hiagent import ProviderFailureDisposition
    from app.monitoring import audit

    request = body or {}
    conn = get_conn()
    row = conn.execute(
        """SELECT j.*, v.provider_task_id, v.image_inputs, s.duration_s
             FROM jobs j
             LEFT JOIN shot_versions v ON v.id=j.version_id
             LEFT JOIN shots s ON s.id=j.shot_id
            WHERE j.id=?""",
        (job_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "媒体任务不存在")
    item = dict(row)
    try:
        input_metadata = json.loads(item.get("image_inputs") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        input_metadata = {}
    shot_plan_id = str(input_metadata.get("shot_plan_id") or "")
    shot_plan = (
        conn.execute(
            "SELECT status FROM shot_video_generation_plans WHERE id=?",
            (shot_plan_id,),
        ).fetchone()
        if shot_plan_id else None
    )
    waiting_input_repair = bool(
        item["status"] == "waiting_human"
        and shot_plan is not None
        and shot_plan["status"] == "waiting_asset"
    )
    waiting_provider_create_resolution = bool(
        item["status"] == "waiting_human"
        and item.get("reason_code") == "VIDEO_PROVIDER_CREATE_UNRESOLVED"
    )
    waiting_provider_failure = bool(
        item["status"] == "waiting_human"
        and item.get("provider_failure_disposition")
        == ProviderFailureDisposition.MANUAL_REVIEW.value
    )
    if item["status"] not in {
        "failed", "cancelled", "abandoned", "paused", "paused_external",
        "paused_budget", "waiting_retry",
    } and not waiting_input_repair and not waiting_provider_create_resolution and not waiting_provider_failure:
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
    if item.get("kind") != "video":
        raise HTTPException(409, detail={
            "code": "JOB_RETRY_UNSUPPORTED_KIND",
            "message": "只有视频供应商任务支持此重试入口",
        })
    else:
        has_provider_task = bool(item.get("provider_task_id"))
        provider_recovery = bool(
            waiting_provider_create_resolution
            or item.get("provider_non_cancellable")
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
        isolated_provider_recovery = bool(
            has_provider_task
            and (
                request.get("isolate_provider_result")
                or item["status"] in {"cancelled", "abandoned"}
                or item.get("cancellation_requested")
                or item.get("abandoned")
                or not item.get("provider_result_adoptable", 1)
            )
        )
        provider_terminal_failure = bool(
            item.get("provider_failure_disposition")
            == ProviderFailureDisposition.EXTERNAL_TERMINAL.value
            or (
                has_provider_task
                and item.get("provider_create_state") == "model_rejected"
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
        manual_provider_failure = bool(
            item.get("provider_failure_disposition")
            == ProviderFailureDisposition.MANUAL_REVIEW.value
        )
        if manual_provider_failure and not request.get("allow_new_submission"):
            raise HTTPException(409, detail={
                "code": "PROVIDER_TECHNICAL_FAILURE_CONFIRMATION_REQUIRED",
                "message": "原供应商任务发生技术失败，重新提交可能产生新费用",
                "retryability": {
                    "retryable": True,
                    "action": "confirm_new_submission",
                    "paid_risk": "may_create_new_charge",
                    "will_submit_new_provider_task": True,
                    "will_continue_existing_provider_task": False,
                    "message": "确认后将重新校验预算并创建新的供应商任务",
                },
            })
        if manual_provider_failure:
            target_status = "queued"
            new_submission_epoch = True
            if not item.get("episode_id"):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_CONTEXT_MISSING",
                    "message": "任务缺少分集上下文，不能安全重新提交",
                })
            retryability = {
                "retryable": True,
                "action": "new_submission_after_technical_failure",
                "paid_risk": "may_create_new_charge",
                "will_submit_new_provider_task": True,
                "will_continue_existing_provider_task": False,
                "message": "已确认技术失败重提并重新校验预算；将创建新的供应商任务",
            }
        elif has_provider_task:
            target_status = "waiting_provider"
            new_submission_epoch = False
            retryability = {
                "retryable": True,
                "action": "continue_poll",
                "paid_risk": "no_new_charge",
                "will_submit_new_provider_task": False,
                "will_continue_existing_provider_task": True,
                "result_isolated": isolated_provider_recovery,
                "message": (
                    "将继续查询同一个供应商任务；结果只进入隔离审计，不会参与采用"
                    if isolated_provider_recovery
                    else "将继续查询同一个供应商任务，不会重复提交或产生新任务"
                ),
            }
        elif provider_recovery_unconfirmed:
            target_status = "queued"
            new_submission_epoch = True
            if not item.get("episode_id"):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_CONTEXT_MISSING",
                    "message": "任务缺少分集上下文，不能安全重新提交",
                })
            retryability = {
                "retryable": True,
                "action": "new_submission_after_unconfirmed_provider",
                "paid_risk": "may_create_new_charge",
                "will_submit_new_provider_task": True,
                "will_continue_existing_provider_task": False,
                "message": "已确认继续并重新校验预算；将开启新的提交批次，并可能产生新费用",
            }
        else:
            target_status = "queued"
            new_submission_epoch = True
            if not item.get("episode_id"):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_CONTEXT_MISSING",
                    "message": "任务缺少分集上下文，不能安全重新提交",
                })
            retryability = {
                "retryable": True,
                "action": "new_submission",
                "paid_risk": "may_create_new_charge",
                "will_submit_new_provider_task": True,
                "will_continue_existing_provider_task": False,
                "message": "该任务尚无供应商断点，将重新提交并可能产生新费用",
            }

        def activate_video_slot() -> None:
            try:
                activated = conn.execute(
                    """UPDATE jobs
                          SET video_slot_active=1
                        WHERE id=? AND status=?
                          AND COALESCE(state_revision,0)=?""",
                    (
                        job_id,
                        item["status"],
                        int(item.get("state_revision") or 0),
                    ),
                )
                if activated.rowcount != 1:
                    raise HTTPException(409, "任务已被其他操作更新")
                if item.get("version_id"):
                    conn.execute(
                        """UPDATE shot_versions
                              SET video_slot_active=1
                            WHERE id=?""",
                        (item["version_id"],),
                    )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, detail={
                    "code": "SHOT_VIDEO_ACTIVE_CONFLICT",
                    "message": "同一镜头已有活动视频任务，请继续或停止当前任务后再重试",
                }) from exc

        if new_submission_epoch:
            if not item.get("version_id"):
                raise HTTPException(409, detail={
                    "code": "JOB_RETRY_CONTEXT_MISSING",
                    "message": "任务缺少视频版本上下文，不能安全重新提交",
                })
            from app.completion_grant import (
                close_provider_video_budget_claim_liability,
                ensure_video_budget_authority_tables,
                reserve_provider_video_budget,
            )
            from app.video_cost_model import initial_shot_generation_cost

            ensure_video_budget_authority_tables(conn)
            version_or_job_id = str(item.get("version_id") or job_id)
            provider_operation_id = (
                f"video-create-{version_or_job_id}-{new_id('epoch')}"
            )
            estimate = initial_shot_generation_cost(
                float(item.get("duration_s") or 5)
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                activate_video_slot()
                conn.execute(
                    """UPDATE budget_reservations
                          SET status='released',settled_at=?,actual_cost_cny=0
                        WHERE job_id=? AND status IN ('reserved','running')""",
                    (time.time(), job_id),
                )
                if (
                    (manual_provider_failure or provider_recovery_unconfirmed)
                    and item.get("provider_operation_id")
                ):
                    close_provider_video_budget_claim_liability(
                        str(item["provider_operation_id"]),
                        job_id=job_id,
                        reason=(
                            "technical_failure_resubmission_confirmed"
                            if manual_provider_failure
                            else "unresolved_create_resubmission_confirmed"
                        ),
                        conn=conn,
                    )
                elif (
                    not provider_recovery_unconfirmed
                    and item.get("provider_create_state") == "not_started"
                    and item.get("provider_operation_id")
                ):
                    released_at = time.time()
                    conn.execute(
                        """UPDATE provider_video_budget_claims
                              SET status='released',updated_at=?,released_at=?
                            WHERE operation_id=? AND job_id=? AND status='reserved'""",
                        (
                            released_at,
                            released_at,
                            item["provider_operation_id"],
                            job_id,
                        ),
                    )
                # 金额不再构成生成拦截（会员分档时长制，非按金额计费）：
                # reserve_budget/reserve_provider_video_budget 均已删除
                # cap 比较分支，恒定返回 True——仍然调用它们只是为了保留
                # budget_reservations/provider_video_budget_claims 审计台账
                # 的记账副作用。历史上这里会在任一者返回 False 时把 job 打成
                # paused_budget 并抛 409（JOB_RETRY_AUTHORITY_MISSING /
                # JOB_RETRY_BUDGET_BLOCKED），那条拦截分支已删除——见
                # CLAUDE.md「Retiring Features」与本次「成本预算拦截体系
                # 退场」。
                worker.media_scheduler.reserve_budget(
                    job_id,
                    item["episode_id"],
                    estimate,
                    worker.episode_video_budget_limit(item["episode_id"]),
                    conn=conn,
                )
                reserve_provider_video_budget(
                    episode_id=str(item["episode_id"]),
                    job_id=job_id,
                    version_id=str(item["version_id"]),
                    operation_id=provider_operation_id,
                    amount_cny=estimate,
                    conn=conn,
                )
            except Exception:
                conn.rollback()
                raise
            cursor = conn.execute(
                """UPDATE jobs
                      SET status=?,error=NULL,next_retry_at=NULL,
                          cancellation_requested=0,lease_owner=NULL,lease_expires_at=NULL,
                          provider_operation_id=?,provider_create_state='not_started',
                          provider_non_cancellable=0,provider_submitted_at=NULL,
                          provider_failure_category=NULL,provider_failure_kind=NULL,
                          provider_failure_disposition=NULL,provider_failure_retryable=NULL,
                          reason_code=NULL,reason_text=NULL,
                          state_revision=COALESCE(state_revision,0)+1,updated_at=?
                    WHERE id=? AND status=?
                      AND COALESCE(state_revision,0)=?""",
                (
                    target_status,
                    provider_operation_id,
                    time.time(),
                    job_id,
                    item["status"],
                    int(item.get("state_revision") or 0),
                ),
            )
        elif isolated_provider_recovery:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """UPDATE jobs
                      SET status='waiting_provider',error=?,next_retry_at=NULL,
                          cancellation_requested=0,abandoned=0,
                          video_slot_active=0,provider_poll_required=1,
                          provider_result_adoptable=0,
                          provider_create_state='accepted',
                          provider_non_cancellable=1,
                          lease_owner=NULL,lease_expires_at=NULL,
                          state_revision=COALESCE(state_revision,0)+1,updated_at=?
                   WHERE id=? AND status=?
                     AND COALESCE(state_revision,0)=?""",
                (
                    retryability["message"],
                    time.time(),
                    job_id,
                    item["status"],
                    int(item.get("state_revision") or 0),
                ),
            )
            if item.get("version_id"):
                conn.execute(
                    """UPDATE shot_versions
                          SET provider_task_id=?,status='waiting_provider',
                              error=?,video_slot_active=0
                        WHERE id=?""",
                    (
                        item.get("provider_task_id"),
                        retryability["message"],
                        item["version_id"],
                    ),
                )
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                activate_video_slot()
            except Exception:
                conn.rollback()
                raise
            cursor = conn.execute(
                """UPDATE jobs SET status=?,error=?,next_retry_at=NULL,
                           cancellation_requested=0,lease_owner=NULL,lease_expires_at=NULL,
                           state_revision=COALESCE(state_revision,0)+1,updated_at=?
                   WHERE id=? AND status=?
                     AND COALESCE(state_revision,0)=?""",
                (
                    target_status,
                    retryability["message"],
                    time.time(),
                    job_id,
                    item["status"],
                    int(item.get("state_revision") or 0),
                ),
            )
        if cursor.rowcount != 1:
            conn.rollback()
            raise HTTPException(409, "任务已被其他操作更新")
        if new_submission_epoch and item.get("version_id"):
            conn.execute(
                """UPDATE shot_versions
                      SET provider_task_id=NULL,status='queued',error=NULL
                    WHERE id=?""",
                (item["version_id"],),
            )
        if waiting_input_repair and item.get("version_id"):
            conn.execute(
                """UPDATE shot_versions SET status='queued',error=NULL
                   WHERE id=? AND status='waiting_human'""",
                (item["version_id"],),
            )
            if shot_plan_id:
                conn.execute(
                    """UPDATE shot_video_generation_plans
                          SET status='planned',updated_at=?
                        WHERE id=? AND status='waiting_asset'""",
                    (time.time(), shot_plan_id),
                )
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
            "minimax_h3": bool(config.MINIMAX_H3_BASE_URL and config.MINIMAX_H3_API_KEY),
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
        if "text_generation_concurrency" in changed:
            from app.generation_concurrency import reload_generation_limits
            reload_generation_limits()
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
        try:
            from app.generation_concurrency import reload_generation_limits
            reload_generation_limits()
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
def put_keys(body: dict, _admin: None = Depends(require_system_admin)):
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
        "minimax_h3": "MINIMAX_H3_API_KEY",
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
