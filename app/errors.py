"""报错码系统。

前端只拿到 错误码(code) + 问题分类(category) + 错误ID(error_id)；技术类报错的原文、
堆栈、请求上下文全部留在后端 error_logs 表，凭 error_id 可查根因。

两类错误的处理策略（见 plan）：
- 业务/校验类（4xx：输入校验/状态冲突/资源不存在）：保留原有友好中文提示，附带 码+ID。
- 技术类（5xx、大模型/外部服务、内容生成、媒体处理）：前端只给安全通用提示 + 码+ID，原文进日志。
"""
from __future__ import annotations

import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any

from app import db

# 分类定义：technical=True 表示原文脱敏，前端只看安全提示 hint。
CATEGORIES: dict[str, dict[str, Any]] = {
    "validation": {"label": "输入校验", "technical": False, "hint": ""},
    "conflict":   {"label": "状态冲突", "technical": False, "hint": ""},
    "not_found":  {"label": "资源不存在", "technical": False, "hint": ""},
    "provider":   {"label": "大模型/外部服务", "technical": True,
                   "hint": "大模型/外部服务调用失败，可稍后重试；若持续失败请把错误码反馈给技术人员。"},
    "generation": {"label": "内容生成", "technical": True,
                   "hint": "内容生成未通过校验，可点击重试，或在监制房调高「修复重试上限」。"},
    "media":      {"label": "媒体处理", "technical": True,
                   "hint": "媒体处理失败（转码/文件读写等），请把错误码反馈给技术人员。"},
    "system":     {"label": "系统内部", "technical": True,
                   "hint": "服务器内部错误，请把错误码反馈给技术人员。"},
}
_FALLBACK = "system"


@dataclass
class ErrorRecord:
    error_id: str
    category: str
    category_label: str
    code: str
    is_technical: bool
    http_status: int | None
    action: str | None
    message: str           # 原始报错信息（仅后端日志用，前端不直接展示技术类原文）
    public: str            # 前端展示串：业务类=友好提示+码；技术类=安全提示+码


def new_error_id() -> str:
    """ERR-YYYYMMDD-xxxxxx：可排序、易 grep、读着报得清。"""
    return f"ERR-{time.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"


def _extract_message(exc: BaseException | None) -> str:
    if exc is None:
        return ""
    detail = getattr(exc, "detail", None)  # FastAPI HTTPException
    if detail is not None:
        return detail if isinstance(detail, str) else str(detail)
    return str(exc)


def classify(exc: BaseException | None, http_status: int | None = None) -> tuple[str, str]:
    """归类并产出报错码。返回 (category_key, code)。

    用类名判断 ProviderError/StageError，避免 import app.hiagent/app.stages 造成环依赖。"""
    name = type(exc).__name__ if exc is not None else ""
    if name == "ProviderError":
        return "provider", "LLM"
    if name == "StageError":
        return "generation", "GEN"
    status = http_status if http_status is not None else getattr(exc, "status_code", None)
    if status is not None:
        if status == 404:
            return "not_found", "NF-404"
        if status == 409:
            return "conflict", "CON-409"
        if status == 422:
            return "validation", "VAL-422"
        if 400 <= status < 500:
            return "validation", f"VAL-{status}"
    if isinstance(exc, OSError):
        return "media", "MED"
    return _FALLBACK, "SYS"


def _public_text(category: str, code: str, error_id: str, base_message: str, is_technical: bool) -> str:
    cat = CATEGORIES.get(category, CATEGORIES[_FALLBACK])
    if is_technical:
        return f"「{cat['label']}」{cat['hint']}（错误码 {code} · {error_id}）"
    base = (base_message or cat["label"]).strip()
    return f"{base}（{code} · {error_id}）"


def log_error(exc: BaseException | None, *, action: str | None = None,
              context: Any | None = None, http_status: int | None = None,
              message: str | None = None, public_message: str | None = None,
              meta: dict | None = None) -> ErrorRecord:
    """落库一条报错并返回展示用记录。

    - message：覆盖写入日志的原文（默认从 exc 提取）。
    - public_message：业务类展示串的基底（默认用 message；技术类忽略此项，只给安全提示）。
    """
    category, code = classify(exc, http_status)
    cat = CATEGORIES.get(category, CATEGORIES[_FALLBACK])
    is_tech = bool(cat["technical"])
    error_id = new_error_id()
    raw_message = message if message is not None else _extract_message(exc)
    status = http_status if http_status is not None else getattr(exc, "status_code", None)

    tb = None
    if exc is not None:
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        except Exception:  # noqa: BLE001 取堆栈失败不应淹没原始错误
            tb = None

    public_base = public_message if public_message is not None else raw_message
    public = _public_text(category, code, error_id, public_base, is_tech)

    merged_meta = dict(meta or {})
    retryable = getattr(exc, "retryable", None)
    if retryable is not None:
        merged_meta.setdefault("retryable", retryable)

    try:
        db.insert_error_log(
            error_id, category=category, category_label=cat["label"], code=code,
            is_technical=is_tech, http_status=status, action=action, context=context,
            message=raw_message, traceback_text=tb,
            exc_type=type(exc).__name__ if exc is not None else None, meta=merged_meta,
        )
    except Exception:  # noqa: BLE001 日志落库失败绝不能再抛，否则会掩盖真正的业务错误
        pass

    return ErrorRecord(error_id=error_id, category=category, category_label=cat["label"],
                       code=code, is_technical=is_tech, http_status=status, action=action,
                       message=raw_message, public=public)


def record_and_format(exc: BaseException, *, action: str | None = None,
                      context: Any | None = None) -> str:
    """后台任务专用：落库完整报错，返回写进 DB *_error 列 / 前端展示的脱敏串。"""
    return log_error(exc, action=action, context=context).public


def code_ref(exc: BaseException, *, action: str | None = None,
             context: Any | None = None) -> str:
    """落库完整报错，返回短引用串「（code · error_id）」。

    用于本就带人话上下文前缀的嵌套诊断项（如「漂移判定失败@第3集」），
    只把原始 str(exc) 换成可追查的码+ID，保留前缀里的定位信息。"""
    rec = log_error(exc, action=action, context=context)
    return f"（{rec.code} · {rec.error_id}）"
