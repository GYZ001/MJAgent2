"""报错码系统。

前端只拿到 错误码(code) + 问题分类(category) + 错误ID(error_id)；技术类报错的原文、
堆栈、请求上下文全部留在后端 error_logs 表，凭 error_id 可查根因。

错误处理策略（见 plan）：
- 业务/校验类（4xx：输入校验/状态冲突/资源不存在）：保留原有友好中文提示，附带 码+ID。
- 质量门禁类（供应商调用成功，但产物未通过业务 QA）：展示可操作的安全原因，不伪报外部服务故障。
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
    "quality_gate": {"label": "质量校验", "technical": False, "hint": ""},
    "provider":   {"label": "大模型/外部服务", "technical": True,
                   "hint": "大模型/外部服务调用失败，可稍后重试；若持续失败请把错误码反馈给技术人员。"},
    "generation": {"label": "内容生成", "technical": True,
                   "hint": "内容生成未通过格式或业务校验，可点击重试；若持续失败，请先按错误码检查具体原因，再决定是否调整「修复重试上限」。"},
    "generation_retry_grant": {
        "label": "内容生成",
        "technical": True,
        "hint": "本集有一次被中断的上游调用结果未确认，蓝图分片在准入阶段被安全拦截。"
                "普通重试与「修复重试上限」对此无效，需先清理该未确认调用后再重新生成；"
                "请把错误码反馈给技术人员。",
    },
    "generation_contract": {
        "label": "生成合同",
        "technical": True,
        "hint": "模型输出未通过确定性生成合同，请按错误码检查合同证据后重试。",
    },
    "generation_budget": {
        "label": "内容生成",
        "technical": True,
        "hint": "本次生成触发了单次运行的调用/输出/时长安全上限而中止，"
                "并非格式或业务校验失败，调整「修复重试上限」无效。"
                "已完成的分片会被复用，直接重新生成即可从中断处继续；"
                "若同一集反复触顶，请把错误码反馈给技术人员。",
    },
    "generation_identity_fixed_budget": {
        "label": "内容生成",
        "technical": True,
        "hint": "人物身份判定按团队既定原则一律 fail-closed，不做格式/语义修复重试"
                "（即不会放宽约束或换一次答案重新摇骰子；网关对完全相同请求的原样"
                "重放不算在内，命中即代表这轮判定本身失败），"
                "「修复重试上限」对本步骤无效，调整它不会改变结果。"
                "请先按错误码检查具体原因，再直接重新生成。",
    },
    "media":      {"label": "媒体处理", "technical": True,
                   "hint": "媒体处理失败（转码/文件读写等），请把错误码反馈给技术人员。"},
    "system":     {"label": "系统内部", "technical": True,
                   "hint": "服务器内部错误，请把错误码反馈给技术人员。"},
}
_FALLBACK = "system"


class ContentGenerationError(Exception):
    """A provider call succeeded, but generated content failed a quality gate."""


class ArtifactNeedsRebuildError(ValueError):
    """A persisted artifact predates a required evidence contract."""

    code = "ARTIFACT_NEEDS_REBUILD"
    retryable = False

    def __init__(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        reason: str,
    ) -> None:
        self.artifact_id = artifact_id
        self.artifact_type = artifact_type
        self.reason = reason
        super().__init__(
            f"[{self.code}] {artifact_type} {artifact_id or 'unknown'} "
            f"需要重建：{reason}"
        )

    def http_detail(
        self,
        *,
        recommended_action: str = "refresh",
    ) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "recommended_action": recommended_action,
        }


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
    if name == "ArtifactNeedsRebuildError":
        return "conflict", "ARTIFACT-REBUILD"
    if name in {
        "ContentGenerationError", "ScreenplayNarrativeGateError", "PrepPackGateError",
        "StructuredSemanticError",
    }:
        # PrepPackGateError: episode_prep_pack (screenplay 契约 6.0.0) 硬门禁
        # 未通过（覆盖账本/资产解析/hook 接地），供应商调用本身是成功的，属于
        # 业务质量校验失败，不应展示为系统内部错误（docs/TRANSFORM_FREEZE_PLAN.md）。
        # StructuredSemanticError（真实第18轮 EP10 回归 ERR-20260824-b16bb4）：
        # app.harness.model_gateway.chat_structured 在调用方自己的 validate
        # 回调判定内容不通过时抛出——供应商调用同样是成功的，失败的是我们自己
        # 的业务/身份消歧校验（如 identity.current 的 source_label 唯一性），
        # 跟 PrepPackGateError 是同一性质，不应走 5xx 技术类外壳把真实原因
        # （比如"source_label 重复：师弟"）藏成"系统内部错误"。
        return "quality_gate", "QA"
    if name == "ProviderError":
        return "provider", "LLM"
    if name == "ScreenplaySceneShardError":
        return "generation_contract", "GEN-CONTRACT"
    if name == "StageError" and any(
        marker in _extract_message(exc)
        for marker in (
            "[BLUEPRINT_GENERATION_CALL_BUDGET]",
            "[BLUEPRINT_GENERATION_TOKEN_BUDGET]",
            "[BLUEPRINT_GENERATION_TIME_BUDGET]",
        )
    ):
        # Runaway breakers (call/token/wall clock). The run was stopped on
        # capacity, not on a format or business check, so it must not carry the
        # generic "内容生成未通过格式或业务校验" hint.
        return "generation_budget", "GEN-BUDGET"
    if name == "StageError" and "JSON 解析失败" in _extract_message(exc):
        return "generation", "JSON"
    if (
        name == "StageError"
        and "BLUEPRINT_PROVIDER_RETRY_GRANT_REQUIRED" in _extract_message(exc)
    ):
        return "generation_retry_grant", "GEN-RETRY-GRANT"
    if (
        name == "StageError"
        and "IDENTITY_DISCOVERY_FIXED_RETRY_BUDGET" in _extract_message(exc)
    ):
        # 人物身份判定 fail-closed，format_retry_limit/semantic_retry_limit
        # 硬编码为 0（见 app/portraits.py 身份判定调用点），通用的
        # "调整「修复重试上限」" 提示对这条路径不成立，必须走专用提示。
        return "generation_identity_fixed_budget", "GEN-IDENTITY-BUDGET"
    if name in {
        "StageError",
        "CompileError",
        "ScreenplayIRIdentityConflictError",
    }:
        return "generation", "GEN"
    if name == "FullRegenDenied":
        return "conflict", "FULL-REGEN-DENIED"
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
