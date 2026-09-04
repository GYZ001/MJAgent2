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
    "text_generation_concurrency": _number(
        "文本模型真实请求并发",
        "6",
        1,
        16,
        unit="请求",
        description="同一时间最多发起多少个真实文本 provider 请求；活动队列会立即按新值扩缩容。",
    ),
    "text_generation_workflow_concurrency": _number(
        "剧本/分镜工作流并发", "10", 1, 16, unit="集",
        description="同一时间最多运行多少集剧本或分镜工作流。",
    ),
    "series_queue_concurrency": _number(
        "连播台并行集数", "3", 1, 8, unit="集",
        description="同一项目同时生成多少集（跨任务共享；一个 10 集的任务内部也按这个数并行），同时也是同时在跑的任务数上限。",
    ),
    "video_prev_frame_reference": _boolean("上一段画面作空间参考", "true"),
    "screenplay_scene_shards_enabled": _boolean("启用剧本场次分片", "true"),
    "screenplay_targeted_identity_enabled": _boolean("启用定向人物解析", "true"),
    "screenplay_targeted_blueprint_review_enabled": _boolean("启用蓝图风险审稿", "true"),
    # RCA (2026-08-23)：4×2(reviewer)=8 路并发几乎顶满 text_generation_concurrency
    # 闸门，单集场次分片阶段自己把并发推进上游"高峰丢弃"区间——317 条
    # stream_cut_before_done 里 299 条是同一句 22 字罐头拒答；级联取消（分片
    # 失败旧代码会连累全集其余分片）又把浪费的时间放大到全部损失的 79.5%。
    # 级联本身已在代码里收窄为分片粒度隔离，这里把默认并发压回历史保守值，
    # 为其他阶段留出闸门余量，不再依赖"分片彼此独立"这条已被证伪的假设。
    "screenplay_scene_shard_parallelism": _number("单集场次分片并发", "2", 1, 8, unit="请求"),
    "screenplay_scene_shard_max_units": _number("场次分片单位上限", "24", 8, 64, unit="units"),
    "screenplay_scene_shard_max_output_chars": _number("场次分片输出字符上限", "12000", 3000, 30000, unit="字符"),
    "screenplay_scene_semantic_review_output_reserve_percent": _number(
        "场次语义审查输出预留",
        "100",
        0,
        200,
        unit="%",
        description=(
            "compact 最坏合法 JSON 之外的有界输出预留；短审查仍使用 2048-token floor，"
            "最终上限仍受模型与上下文能力约束。"
        ),
    ),
    # 默认 1→3（2026-09-01 ERR-20260901-037d7b）：映射台 chunk 调用实测两类
    # 随机失败——供应商 finish_reason=stop 早停截断（258/8000 token 就停笔）与
    # 修复调用口吃 JSON（"k": "k": "v"）——单次修复机会抽到坏样本即整步失败。
    # 失败是随机采样问题，模型调用免费（HiAgent 自有服务），多抽即过。
    "screenplay_format_retry_limit": _number("剧本格式修复上限", "3", 0, 3, unit="次"),
    "screenplay_semantic_retry_limit": _number("剧本语义修复上限", "1", 0, 3, unit="次"),
    "screenplay_fidelity_max_rounds": _number("剧本保真补写上限", "8", 1, 8, unit="轮"),
    "video_submit_concurrency": _number("视频提交并发", "15", 1, 64, unit="任务"),
    "video_inflight_limit": _number("上游视频在途上限", "15", 1, 128, unit="任务"),
    "video_poll_concurrency": _number("视频轮询并发", "15", 1, 128, unit="任务"),
    "reference_pipeline_concurrency": _number("参考图流水线并发", "15", 1, 64, unit="任务"),
    "image_request_concurrency": _number("图片请求并发", "4", 1, 64, unit="请求"),
    "download_concurrency": _number("下载并发", "3", 1, 64, unit="任务"),
    "finalize_concurrency": _number("落盘/校验并发", "4", 1, 64, unit="任务"),
    "episode_video_inflight_limit": _number("单集上游在途上限", "15", 1, 128, unit="任务"),
    "project_video_inflight_limit": _number("单项目上游在途上限", "15", 1, 256, unit="任务"),
    "reference_prepared_backlog": _number("参考图领先视频槽位数", "8", 0, 128, unit="镜"),
    "video_ready_low_watermark": _number("视频就绪低水位", "2", 0, 128, unit="镜"),
    "video_ready_high_watermark": _number("视频就绪高水位", "6", 0, 128, unit="镜"),
    "reference_shot_cohort_limit": _number("参考图镜头批次上限", "15", 1, 32, unit="镜"),
    "video_concurrency": _number("兼容视频并发数", "15", 1, 64, unit="任务"),
    "auto_concurrency": _number("兼容旧版视频并发", "15", 1, 128, unit="任务"),
    "max_ref_images": _number("单镜头最多参考图数", "2", 0, 16, unit="张"),
    "max_repair_attempts": _number("修复重试上限", "8", 0, 30, unit="次"),
    "provider_call_retention_days": _number("模型调用日志保留天数", "30", 1, 365, unit="天", immediate=False),
    "error_log_retention_days": _number("错误日志保留天数", "30", 1, 365, unit="天", immediate=False),
    "use_character_refs": _boolean("定妆照参考图", "true"),
    "storyboard_workspace_safe_readonly": _boolean("分镜台安全只读模式", "false"),
    "storyboard_structure_edit_enabled": _boolean("分镜结构编辑", "true", experimental=True),
    "storyboard_source_rebind_enabled": _boolean("分镜原文重绑定", "true", experimental=True),
    "video_reference_batch_prompt": _boolean("批量参考图提示词", "true"),
    "video_reference_role_adaptive": _boolean("质量角色自适应", "false", experimental=True),
    "video_plan_confidence_floor": _number(
        "视频计划最低置信度", "0.55", 0, 1, step=.05, integer=False,
    ),
    "provider_media_max_download_bytes": _number(
        "视频参考素材大小上限", str(512 * 1024 * 1024),
        1_048_576, 2_147_483_648, unit="字节",
    ),
    "media_scheduler_policy": {
        "label": "调度策略", "type": "enum", "default": "stage_aware",
        "options": ["legacy", "stage_aware"], "immediate": True,
        "experimental": False,
    },
}

_MODEL_PROVIDER_OPTIONS = {
    "model_text_provider": ["hiagent", "openrouter", "bailian", "deepseek", "zhipu"],
    "model_vlm_provider": ["hiagent", "openrouter", "bailian"],
    "model_video_provider": ["hiagent", "minimax_h3"],
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
    "minimax_h3_model_video", "minimax_h3_base_url",
    "openrouter_model_text", "openrouter_model_vlm", "bailian_model_text", "bailian_model_vlm",
    "deepseek_model_text", "zhipu_model_text",
):
    SETTINGS_SCHEMA[_key] = {
        "label": _key,
        "type": "string", "default": config.DEFAULT_SETTINGS.get(_key, ""), "max_length": 500,
        "immediate": True, "experimental": False,
    }

SETTINGS_SCHEMA["provider_media_public_base_url"] = {
    "label": "视频参考媒体公开基址",
    "type": "string",
    "default": config.DEFAULT_SETTINGS["provider_media_public_base_url"],
    "max_length": 500,
    "allow_empty": True,
    "format": "public_http_url",
    "immediate": True,
    "experimental": False,
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
        if raw not in allowed and not (
            key in {
                "model_text_provider", "model_vlm_provider",
                "model_video_provider", "model_image_provider",
            }
            and _custom_provider_exists(raw)
        ):
            raise HTTPException(422, detail={"field": key, "message": f"只允许：{', '.join(sorted(allowed))}"})
        return raw
    if not raw and spec.get("allow_empty"):
        return ""
    if not raw:
        raise HTTPException(422, detail={"field": key, "message": "不能为空"})
    if len(raw) > int(spec.get("max_length") or 1000):
        raise HTTPException(422, detail={"field": key, "message": "内容过长"})
    if spec.get("format") == "public_http_url":
        from app.system_api import _assert_public_http_url

        _assert_public_http_url(raw)
    if key == "minimax_h3_base_url" and not re.fullmatch(
        r"https?://(?:\[[0-9A-Fa-f:]+\]|[^\s/:?#]+)(?::\d+)?",
        raw,
    ):
        raise HTTPException(422, detail={
            "field": key,
            "message": "必须是仅包含协议、主机和可选端口的 http(s) 服务地址",
        })
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
