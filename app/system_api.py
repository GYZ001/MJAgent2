"""System, settings, model-catalog, credentials, and filesystem API routes."""
from __future__ import annotations

import json
import os
import re
import string
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException

from app import config
from app.db import get_conn, get_setting, new_id, rows_to_dicts, set_setting

router = APIRouter(prefix="/api")

# ---------- 文件系统目录浏览（本机部署，供导出目录选择器使用） ----------

def _list_drives() -> list[str]:
    if os.name != "nt":
        return []
    return [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]


@router.get("/system/browse")
def browse_dir(path: str = ""):
    """列出某目录下的子目录，供前端目录选择器逐级浏览。
    path 为空时：Windows 返回盘符列表，POSIX 从根目录开始。"""
    drives = _list_drives()
    p = (path or "").strip()
    if not p:
        if os.name == "nt":
            return {"path": "", "parent": None, "drives": drives,
                    "dirs": [{"name": d, "path": d} for d in drives]}
        p = "/"
    base = Path(p)
    if not base.exists() or not base.is_dir():
        raise HTTPException(404, f"目录不存在：{p}")
    dirs = []
    try:
        for child in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue  # 个别子项无权访问/不可达，跳过
    except PermissionError:
        raise HTTPException(403, f"无权访问：{p}")
    parent = str(base.parent) if base.parent != base else None
    return {"path": str(base), "parent": parent, "drives": drives, "dirs": dirs}


@router.post("/system/mkdir")
def make_dir(body: dict):
    """在指定父目录下新建文件夹，供选择器「新建文件夹」使用。"""
    parent = (body.get("path") or "").strip()
    name = (body.get("name") or "").strip()
    if not parent or not name:
        raise HTTPException(422, "缺少父目录或文件夹名")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
        raise HTTPException(422, '文件夹名含非法字符（不能包含 \\ / : * ? " < > |）')
    dest = Path(parent) / name
    try:
        dest.mkdir(parents=True, exist_ok=True)
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
    ("hiagent", "d2a5n9rnvvm49eucvnvg", "文本推理模型", ("text",)),
    ("hiagent", "d71l5c8nfdb167kligqg", "Text 模型", ("text",)),
    ("hiagent", "d7ev7il5boeaebtf4sgg", "视觉质检模型", ("vlm",)),
    ("hiagent", "d7jf6nd5boeaebtfbdqg", "Seedance 视频生成", ("video",)),
    ("hiagent", "d7ute7ppcc7n89uuqqp0", "Seedream 图像生成", ("image",)),
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
    public["key_configured"] = bool(item.get("api_key") or credentials.get(item.get("id"), {}).get("api_key"))
    return public


@router.get("/models")
def get_models():
    return {"items": [_public_model(item) for item in _model_catalog()]}


@router.post("/models")
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


async def _probe_openai_model(base_url: str, api_key: str, model: str, kind: str = "text") -> dict:
    base_url = base_url.strip().rstrip("/")
    if not re.fullmatch(r"https?://[^\s]+", base_url):
        raise HTTPException(422, "Base URL 必须是有效的 http(s) 地址")
    if not api_key.strip() or not model.strip():
        raise HTTPException(422, "模型 ID 和 API Key 不能为空")
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
    return await _probe_openai_model(
        str(body.get("base_url") or ""), str(body.get("api_key") or ""),
        str(body.get("model") or ""), str(body.get("kind") or "text"))


@router.put("/models/{model_id}")
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

@router.get("/system/health")
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
        return [option(item["provider"], item["model"], True) for item in _custom_models()
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


@router.get("/system/calls")
def recent_calls(limit: int = 30):
    rows = rows_to_dicts(get_conn().execute(
        "SELECT * FROM provider_calls ORDER BY id DESC LIMIT ?", (min(limit, 200),)).fetchall())
    return rows


@router.get("/system/errors")
def recent_errors(limit: int = 50):
    """最近报错码列表（不含原文/堆栈，只给概览）。凭 id 调下方详情接口查根因。"""
    rows = rows_to_dicts(get_conn().execute(
        """SELECT id, ts, category, category_label, code, is_technical, http_status, action, exc_type
           FROM error_logs ORDER BY ts DESC LIMIT ?""", (min(limit, 200),)).fetchall())
    return rows


@router.get("/system/errors/{error_id}")
def error_detail(error_id: str):
    """凭错误ID查全文：请求动作上下文 + 原始报错 + 堆栈，定位根因用。"""
    row = get_conn().execute("SELECT * FROM error_logs WHERE id=?", (error_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"错误ID不存在：{error_id}")
    return dict(row)


@router.get("/system/jobs")
def jobs_overview():
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

    # workflow_runs is the authoritative business-task ledger.  Resolve its scope
    # back to project/episode/shot labels so the legacy queue UI can present every
    # Harness workflow, including bible/reference generation that never creates a
    # row in the low-level media jobs table.
    run_recent = rows_to_dicts(conn.execute(
        """SELECT wr.*,
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
           ORDER BY wr.updated_at DESC LIMIT 200""").fetchall())
    for row in run_recent:
        row["source"] = "run"
        row["run_id"] = row["id"]
        row["kind"] = row["workflow_type"]
        row["raw_status"] = row["status"]
        row["status"] = run_statuses.get(row["status"], row["status"].lower())
        row["error"] = row.get("failure_message")
    for row in conn.execute("SELECT status, COUNT(*) c FROM workflow_runs GROUP BY status"):
        add_count(run_statuses.get(row["status"], row["status"].lower()), row["c"])

    # Keep legacy/untraced media jobs visible, but omit jobs already represented
    # by a valid Run so one business task is never counted twice.
    legacy_jobs = rows_to_dicts(conn.execute(
        """SELECT j.*, 'job' AS source, s.shot_no, e.episode_no,
                  e.title AS episode_title, p.name AS project_name
           FROM jobs j LEFT JOIN shots s ON s.id=j.shot_id
           LEFT JOIN episodes e ON e.id=j.episode_id LEFT JOIN projects p ON p.id=j.project_id
           WHERE j.run_id IS NULL
              OR NOT EXISTS (SELECT 1 FROM workflow_runs wr WHERE wr.id=j.run_id)
           ORDER BY j.updated_at DESC LIMIT 200""").fetchall())
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
           ORDER BY updated_at DESC LIMIT 200""").fetchall())
    for row in screenplay_recent:
        add_count(row["status"])
    recent = sorted(
        [*run_recent, *legacy_jobs, *screenplay_recent],
        key=lambda row: row.get("updated_at") or 0,
        reverse=True,
    )[:200]
    return {"counts": counts, "recent": recent}


@router.get("/settings")
def get_settings():
    rows = get_conn().execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@router.put("/settings")
def put_settings(body: dict):
    for key, value in body.items():
        skey = str(key)
        sval = str(value).strip()
        if skey in {"provider_call_retention_days", "error_log_retention_days"}:
            try:
                days = int(sval)
            except ValueError as exc:
                raise HTTPException(422, f"{skey} 必须是整数天数") from exc
            if not 1 <= days <= 365:
                raise HTTPException(422, f"{skey} 必须在 1~365 天之间")
            sval = str(days)
        custom_provider = sval.startswith("custom:") and any(m.get("provider") == sval for m in _custom_models())
        if skey == "model_text_provider" and sval not in {"hiagent", "openrouter", "bailian", "deepseek", "zhipu"} and not custom_provider:
            raise HTTPException(422, f"{skey} 只能是 hiagent、openrouter、bailian、deepseek 或 zhipu")
        if skey == "model_vlm_provider" and sval not in {"hiagent", "openrouter", "bailian"} and not custom_provider:
            raise HTTPException(422, f"{skey} 只能是 hiagent、openrouter 或 bailian")
        if skey == "model_route" and sval not in {"hiagent", "openrouter"}:
            raise HTTPException(422, f"{skey} 只能是 hiagent 或 openrouter")
        if skey in {"model_video_provider", "model_image_provider"} and sval != "hiagent":
            raise HTTPException(422, "当前视频/图像生成只支持火山 HiAgent")
        set_setting(skey, sval)
        if skey == "model_route":
            set_setting("model_text_provider", sval)
            set_setting("model_vlm_provider", sval)
    return {"ok": True}


# ---------- API Key 管理：前端填写 → 持久化 .env ----------

@router.get("/keys")
def get_keys():
    """获取各 provider 的 key 状态（不返回完整 key 值）。"""
    return config.get_key_status()


@router.put("/keys")
def put_keys(body: dict):
    """保存 API Key 到 .env 并热更新运行时变量。

    body 格式：{"hiagent": "sk-xxx", "openrouter": "sk-or-v1-xxx", "bailian": "sk-xxx", "deepseek": "sk-xxx"}
    前端传 provider 名（小写），后端映射到对应的环境变量名。
    """
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
        if p not in provider_to_key:
            raise HTTPException(422, f"不支持的 provider：{p}，可选：{', '.join(provider_to_key)}")
        env_keys[provider_to_key[p]] = str(value).strip()

    updated = config.save_keys_to_env(env_keys)
    if not updated:
        raise HTTPException(422, "没有提供有效的 Key")
    updated_providers = [k.replace("_API_KEY", "").lower() for k in updated]
    return {"ok": True, "updated": updated_providers}
