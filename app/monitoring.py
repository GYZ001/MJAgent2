"""监制房共享契约：类型化设置、查询脱敏与轻量审计。

这里刻意只放可被 API、运行时和测试共同复用的纯规则，避免前后端各自猜测
设置类型或日志敏感字段。
"""
from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from app import config
from app.db import get_conn, new_id, now


_MONITOR_FEATURE_ENV = {
    "overview_state_v2": "MONITOR_OVERVIEW_V2_ENABLED",
    "jobs_query_v2": "MONITOR_JOBS_QUERY_V2_ENABLED",
    "run_center_v2": "MONITOR_RUN_CENTER_V2_ENABLED",
    "call_detail_v2": "MONITOR_CALL_DETAIL_V2_ENABLED",
    "settings_edit_v2": "MONITOR_SETTINGS_EDIT_V2_ENABLED",
}


def monitor_features() -> dict[str, bool]:
    """Independent, fail-safe rollout controls for the five PRD surfaces."""
    return {
        key: os.environ.get(env_name, "true").strip().lower()
        not in {"0", "false", "off", "no"}
        for key, env_name in _MONITOR_FEATURE_ENV.items()
    }


def _number(
    label: str,
    default: str,
    minimum: float,
    maximum: float,
    *,
    step: float = 1,
    unit: str = "",
    integer: bool = True,
    immediate: bool = True,
    experimental: bool = False,
    description: str = "",
) -> dict[str, Any]:
    spec = {
        "label": label, "type": "integer" if integer else "number",
        "default": default, "min": minimum, "max": maximum, "step": step,
        "unit": unit, "immediate": immediate, "experimental": experimental,
    }
    if description:
        spec["description"] = description
    return spec


def _boolean(label: str, default: str, *, experimental: bool = False) -> dict[str, Any]:
    return {
        "label": label, "type": "boolean", "default": default,
        "immediate": True, "experimental": experimental,
    }


# 监制房可写白名单。其余 DEFAULT_SETTINGS 仍能被程序内部读取，但不能通过通用
# 运维接口任意写入；模型路由键在下方按专用规则补充。
SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    "video_submit_concurrency": _number("视频提交并发", "4", 1, 64, unit="任务"),
    "video_inflight_limit": _number("上游视频在途上限", "8", 1, 128, unit="任务"),
    "video_poll_concurrency": _number("视频轮询并发", "8", 1, 128, unit="任务"),
    "reference_pipeline_concurrency": _number("参考图流水线并发", "6", 1, 64, unit="任务"),
    "image_request_concurrency": _number("图片请求并发", "4", 1, 64, unit="请求"),
    "vlm_request_concurrency": _number("VLM 质检并发", "6", 1, 64, unit="请求"),
    "download_concurrency": _number("下载并发", "3", 1, 64, unit="任务"),
    "finalize_concurrency": _number("落盘/校验并发", "4", 1, 64, unit="任务"),
    "episode_video_inflight_limit": _number("单集上游在途上限", "8", 1, 128, unit="任务"),
    "project_video_inflight_limit": _number("单项目上游在途上限", "12", 1, 256, unit="任务"),
    "reference_prepared_backlog": _number("参考图领先视频槽位数", "8", 0, 128, unit="镜"),
    "video_ready_low_watermark": _number("视频就绪低水位", "2", 0, 128, unit="镜"),
    "video_ready_high_watermark": _number("视频就绪高水位", "6", 0, 128, unit="镜"),
    "reference_shot_cohort_limit": _number("参考图镜头批次上限", "1", 1, 32, unit="镜"),
    "video_concurrency": _number("兼容视频并发数", "4", 1, 64, unit="任务"),
    "auto_concurrency": _number("兼容旧版视频并发", "8", 1, 128, unit="任务"),
    "episode_cost_limit_cny": _number("单集成本上限", "100", 0, 1_000_000, step=.01, unit="元", integer=False),
    "max_ref_images": _number("单镜头最多参考图数", "2", 0, 16, unit="张"),
    "auto_retake_threshold": _number(
        "历史质量阈值（不触发重抽）", "0.6", 0, 1, step=.05, integer=False,
        description="兼容旧配置读取；QA 分数不再控制自动重抽、重试或门禁。",
    ),
    "max_repair_attempts": _number("修复重试上限", "8", 0, 30, unit="次"),
    "provider_call_retention_days": _number("模型调用日志保留天数", "30", 1, 365, unit="天", immediate=False),
    "error_log_retention_days": _number("错误日志保留天数", "30", 1, 365, unit="天", immediate=False),
    "use_character_refs": _boolean("定妆照参考图", "true"),
    "auto_qa": _boolean("自动质检", "true"),
    "storyboard_workspace_safe_readonly": _boolean("分镜台安全只读模式", "false"),
    "storyboard_structure_edit_enabled": _boolean("分镜结构编辑", "true", experimental=True),
    "storyboard_source_rebind_enabled": _boolean("分镜原文重绑定", "true", experimental=True),
    "video_reference_batch_prompt": _boolean("批量参考图提示词", "true"),
    "video_reference_role_adaptive": _boolean("质量角色自适应", "false", experimental=True),
    "media_scheduler_policy": {
        "label": "调度策略", "type": "enum", "default": "stage_aware",
        "options": ["legacy", "stage_aware"], "immediate": True,
        "experimental": False,
    },
}

_MODEL_PROVIDER_OPTIONS = {
    "model_text_provider": ["hiagent", "openrouter", "bailian", "deepseek", "zhipu"],
    "model_vlm_provider": ["hiagent", "openrouter", "bailian"],
    "model_video_provider": ["hiagent"],
    "model_image_provider": ["hiagent"],
    "model_route": ["hiagent", "openrouter"],
}
for _key, _options in _MODEL_PROVIDER_OPTIONS.items():
    SETTINGS_SCHEMA[_key] = {
        "label": _key, "type": "enum", "default": config.DEFAULT_SETTINGS.get(_key, _options[0]),
        "options": _options, "immediate": True, "experimental": False,
    }

for _key in (
    "hiagent_model_text", "hiagent_model_vlm", "hiagent_model_video", "hiagent_model_image",
    "openrouter_model_text", "openrouter_model_vlm", "bailian_model_text", "bailian_model_vlm",
    "deepseek_model_text", "zhipu_model_text",
):
    SETTINGS_SCHEMA[_key] = {
        "label": _key, "type": "string", "default": "", "max_length": 180,
        "immediate": True, "experimental": False,
    }


def public_settings_schema() -> dict[str, dict[str, Any]]:
    return deepcopy(SETTINGS_SCHEMA)


def _custom_provider_exists(value: str) -> bool:
    if not value.startswith("custom:"):
        return False
    try:
        rows = json.loads(get_conn().execute(
            "SELECT value FROM settings WHERE key='custom_models'"
        ).fetchone()["value"] or "[]")
    except (TypeError, KeyError, json.JSONDecodeError):
        return False
    return any(isinstance(item, dict) and item.get("provider") == value for item in rows)


def normalize_setting(key: str, value: Any) -> str:
    spec = SETTINGS_SCHEMA.get(key)
    if not spec:
        raise HTTPException(422, detail={"field": key, "message": "未声明的设置项"})
    kind = spec["type"]
    if kind == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        raw = str(value).strip().lower()
        if raw not in {"true", "false"}:
            raise HTTPException(422, detail={"field": key, "message": "必须为 true 或 false"})
        return raw
    if kind in {"integer", "number"}:
        if isinstance(value, bool) or value is None or str(value).strip() == "":
            raise HTTPException(422, detail={"field": key, "message": "必须填写有限数值"})
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, detail={"field": key, "message": "必须填写数值"}) from exc
        if not math.isfinite(number):
            raise HTTPException(422, detail={"field": key, "message": "NaN/Infinity 不是合法数值"})
        if number < spec["min"] or number > spec["max"]:
            raise HTTPException(422, detail={
                "field": key, "message": f"必须在 {spec['min']}~{spec['max']} 之间",
            })
        if kind == "integer" and not number.is_integer():
            raise HTTPException(422, detail={"field": key, "message": "必须为整数"})
        step = float(spec.get("step") or 0)
        if step and kind == "number":
            base = float(spec.get("min") or 0)
            quotient = (number - base) / step
            if abs(quotient - round(quotient)) > 1e-8:
                raise HTTPException(422, detail={"field": key, "message": f"步长必须为 {step:g}"})
        return str(int(number)) if kind == "integer" else format(number, ".12g")
    raw = str(value).strip()
    if kind == "enum":
        allowed = set(spec.get("options") or [])
        if raw not in allowed and not (key in {"model_text_provider", "model_vlm_provider"} and _custom_provider_exists(raw)):
            raise HTTPException(422, detail={"field": key, "message": f"只允许：{', '.join(sorted(allowed))}"})
        return raw
    if not raw:
        raise HTTPException(422, detail={"field": key, "message": "不能为空"})
    if len(raw) > int(spec.get("max_length") or 1000):
        raise HTTPException(422, detail={"field": key, "message": "内容过长"})
    return raw


def validate_settings_patch(patch: dict[str, Any], current: dict[str, str]) -> dict[str, str]:
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(422, "设置变更不能为空")
    normalized = {str(key): normalize_setting(str(key), value) for key, value in patch.items()}
    merged = {**current, **normalized}
    low = int(merged.get("video_ready_low_watermark") or 0)
    high = int(merged.get("video_ready_high_watermark") or 0)
    if low > high:
        raise HTTPException(422, detail={
            "field": "video_ready_high_watermark",
            "message": "视频就绪高水位不能低于低水位",
        })
    episode_limit = int(merged.get("episode_video_inflight_limit") or 1)
    project_limit = int(merged.get("project_video_inflight_limit") or 1)
    if episode_limit > project_limit:
        raise HTTPException(422, detail={
            "field": "project_video_inflight_limit",
            "message": "单项目在途上限不能低于单集上限",
        })
    return normalized


_SECRET_KEY_RE = re.compile(r"(api[_-]?key|authorization|password|secret|access[_-]?token|token)", re.I)
_SENSITIVE_INPUT_KEY_RE = re.compile(r"^(prompt|messages?|input|input_text|system_prompt|user_content)$", re.I)
_ABS_PATH_RE = re.compile(r"(?<![\w:/])(?:[A-Za-z]:[\\/][^\s\"']+|/(?:Users|home|private|var|tmp|opt)/[^\s\"']+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_TOKEN_RE = re.compile(r"\b(?:sk|ak)-[A-Za-z0-9_-]{8,}\b", re.I)


def redact_monitor_value(value: Any, *, mask_sensitive_content: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "***" if _SECRET_KEY_RE.search(str(key))
                else "[敏感输入已隐藏]" if mask_sensitive_content and _SENSITIVE_INPUT_KEY_RE.search(str(key))
                else redact_monitor_value(item, mask_sensitive_content=mask_sensitive_content)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_monitor_value(item, mask_sensitive_content=mask_sensitive_content) for item in value]
    if isinstance(value, str):
        value = _BEARER_RE.sub(r"\1***", value)
        value = _TOKEN_RE.sub("***", value)
        return _ABS_PATH_RE.sub("[本机路径已隐藏]", value)
    return value


def redact_json_text(raw: str | None, *, mask_sensitive_content: bool = False) -> str | None:
    if not raw:
        return raw
    try:
        return json.dumps(redact_monitor_value(json.loads(raw), mask_sensitive_content=mask_sensitive_content), ensure_ascii=False)
    except (TypeError, json.JSONDecodeError):
        return str(redact_monitor_value(raw, mask_sensitive_content=mask_sensitive_content))


def ensure_monitor_audit_table() -> None:
    get_conn().execute(
        """CREATE TABLE IF NOT EXISTS monitor_audit(
               id TEXT PRIMARY KEY, ts REAL NOT NULL, action TEXT NOT NULL,
               object_type TEXT NOT NULL, object_id TEXT NOT NULL,
               outcome TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}'
           )"""
    )
    get_conn().commit()


def audit(action: str, object_type: str, object_id: str, outcome: str, detail: dict[str, Any] | None = None) -> None:
    ensure_monitor_audit_table()
    safe_detail = redact_monitor_value(detail or {})
    get_conn().execute(
        "INSERT INTO monitor_audit(id,ts,action,object_type,object_id,outcome,detail_json) VALUES(?,?,?,?,?,?,?)",
        (new_id("audit"), now(), action, object_type, object_id, outcome,
         json.dumps(safe_detail, ensure_ascii=False)),
    )
    get_conn().commit()
