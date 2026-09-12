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
        "0",
        0,
        32,
        unit="请求",
        description="真实文本 provider 请求并发；0=自动（慢启动探到供应商真实容量，拥塞减半，安全阀 32）。显式数字=固定上限。",
    ),
    "text_generation_workflow_concurrency": _number(
        "剧本/分镜工作流并发", "0", 0, 32, unit="集",
        description="同一时间最多运行多少集剧本或分镜工作流；0=自动（只受机器水位闸约束，安全阀 32）。",
    ),
    "series_queue_concurrency": _number(
        "连播台并行任务数", "0", 0, 32, unit="任务",
        description="同一项目同时跑多少个连播任务；0=自动（只受机器水位闸约束，安全上限 32）。任务之间互不等待。",
    ),
    "series_episode_concurrency": _number(
        "每个连播任务并行集数", "0", 0, 32, unit="集",
        description="一个连播任务内部同时生成多少集；0=自动（只受机器水位闸约束，安全上限 32）。供应商并发配额另算。",
    ),
    "admission_memory_pct": _number(
        "机器水位闸：内存占用率上限", "70", 30, 95, unit="%",
        description="内存占用率达到本值就不再放新并发（连播台集槽位、视频提交、文本调用），已在跑的不受影响。",
    ),
    "admission_disk_io_pct": _number(
        "机器水位闸：磁盘 IO 利用率上限", "70", 30, 100, unit="%",
        description="数据目录所在磁盘的 IO 利用率（同 iostat %util）达到本值就不再放新并发。",
    ),
    "admission_cpu_load_ratio": _number(
        "机器水位闸：CPU 负载/核上限", "0.8", 0.3, 4, unit="倍", step=0.1,
        description="1 分钟平均负载除以核数达到本值就不再放新并发；合片编码吃的是 CPU。",
    ),
    "video_prev_frame_reference": _boolean("上一段画面作空间参考", "false"),
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
    "video_submit_concurrency": _number("视频提交并发", "0", 0, 64, unit="任务", description="0=自动（供应商信号自适应，安全阀 64）"),
    "video_inflight_limit": _number("上游视频在途上限", "0", 0, 128, unit="任务", description="0=自动（安全阀 128）"),
    "video_poll_concurrency": _number("视频轮询并发", "0", 0, 128, unit="任务", description="0=自动（安全阀 128）"),
    "reference_pipeline_concurrency": _number("参考图流水线并发", "0", 0, 64, unit="任务", description="0=自动（安全阀 64）"),
    "image_request_concurrency": _number("图片请求并发", "0", 0, 64, unit="请求", description="0=自动（慢启动探到供应商真实容量，拥塞减半，安全阀 64）"),
    "vlm_request_concurrency": _number("VLM 请求并发", "0", 0, 64, unit="请求", description="0=自动（供应商信号自适应，安全阀 64）"),
    "download_concurrency": _number("下载并发", "0", 0, 16, unit="任务", description="0=自动（安全阀 16，另受机器水位闸）"),
    "finalize_concurrency": _number("落盘/校验并发", "0", 0, 16, unit="任务", description="0=自动（安全阀 16，另受机器水位闸）"),
    "episode_video_inflight_limit": _number("单集上游在途上限", "0", 0, 128, unit="任务", description="0=自动（由全局视频在途通道兜底，不再手填数字）"),
    "project_video_inflight_limit": _number("单项目上游在途上限", "0", 0, 256, unit="任务", description="0=自动（同上）"),
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
    "provider_media_max_download_bytes": _number(
        "视频参考素材大小上限", str(512 * 1024 * 1024),
        1_048_576, 2_147_483_648, unit="字节",
    ),
    "media_scheduler_policy": {
        "label": "调度策略", "type": "enum", "default": "stage_aware",
        "options": ["legacy", "stage_aware"], "immediate": True,
        "experimental": False,
    },
    # EP-03 第二阶段：密码策略（app.auth.password_policy）+ 会话策略
    # （app.auth.session_policy）。全新键，B 上 settings 表不可能有旧值——
    # 首次读取一律走这里的 default（CLAUDE.md「新增策略键必须同时改 schema」）。
    "password_min_length": _number(
        "密码最小长度", "12", 6, 64, unit="位",
        description="低于此长度的新口令一律拒绝（管理员开户/重置、自助改密、邀请接受首次设密均适用）。",
    ),
    "password_classes": _number(
        "密码字符类别数", "3", 1, 4, unit="类",
        description="大写字母/小写字母/数字/符号四类中至少要包含几类，弱口令拒绝时会逐条报出还缺哪几类。",
    ),
    "password_max_age_days": _number(
        "密码有效期", "0", 0, 365, unit="天",
        description="0=不过期；超过此天数未改密，下次登录成功后会被强制要求改密（must_change_password）。",
    ),
    "password_history_size": _number(
        "密码历史校验条数", "5", 0, 24, unit="条",
        description="改密时与最近 N 次历史口令比对，拒绝重复使用；0=不校验历史。",
    ),
    "session_idle_timeout_min": _number(
        "会话空闲超时", "480", 5, 43200, unit="分钟",
        description="超过此时长无任何请求，会话失效，下次请求 401 并提示因空闲超时被登出。",
    ),
    "session_max_age_hours": _number(
        "会话最长时长", "12", 1, 720, unit="小时",
        description="即便持续活跃，会话从创建起超过此时长也会失效，需重新登录。",
    ),
    "session_max_concurrent": _number(
        "单账号最大并发会话数", "0", 0, 50, unit="个",
        description="0=不限；超过时踢掉最早登录的会话，被踢会话下次请求会收到明确原因。",
    ),
}

# provider 合法取值不再是写死枚举（CLAUDE.md「禁止黑白名单与枚举穷举」）：
# 判据改在 normalize_setting 的 "provider_ref" 分支里从模型库活数据推导——新增
# 一家供应商只需要在模型中心加一条，这里不用改代码。EP-05 第二阶段起这 5 个键
# 已不是选路的权威来源（app.models_registry.routing 按 model_bindings 选路），
# 但界面仍在写它们，写入必须真的生效（见 app.system_api.put_settings 里
# sync_legacy_binding 的调用），校验因此仍然保留、只是不再挂一张固定名单。
_LEGACY_PROVIDER_SETTING_KEYS = (
    "model_text_provider", "model_vlm_provider",
    "model_video_provider", "model_image_provider", "model_route",
)
for _key in _LEGACY_PROVIDER_SETTING_KEYS:
    SETTINGS_SCHEMA[_key] = {
        "label": _key, "type": "provider_ref",
        "default": config.DEFAULT_SETTINGS.get(_key, ""),
        "immediate": True, "experimental": False,
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

# WS1b 原「文本审核拒答换路目的地」设置项（text_moderation_fallback_route）
# 已在 EP-05 第三阶段删除：换路目的地改由 app.models_registry.routing 按
# text:default 优先级链推导，不再需要运维手填一个固定 "provider:model"，
# 不留两套换路机制并存（见 app/harness/model_gateway_moderation.py 模块文档）。


def public_settings_schema() -> dict[str, dict[str, Any]]:
    return deepcopy(SETTINGS_SCHEMA)


def _known_provider_identity(raw: str) -> bool:
    """``model_*_provider``/``model_route`` 的合法值判据：从数据推导，不挂
    固定名单。要么模型库里真有这条条目（覆盖 ``custom:*`` 与已迁移的内置
    条目，任意 kind），要么是内置协议家族的字面量——后者取自
    ``config.MANAGED_KEYS``（PUT /api/keys 已经在用的同一份真源，不是又起
    一份枚举），覆盖"env 尚未配置、模型库还没镜像出这条内置条目"的早期部署
    窗口，与 ``app.system_health._FAMILIES`` 同一口径。不做 kind 匹配——旧版
    ``_custom_provider_exists`` 也不做，保持"存在性判据"这条既有语义不变。
    """
    from app import config
    from app.model_registry import catalog_item

    if catalog_item(raw) is not None:
        return True
    return raw in {name.replace("_API_KEY", "").lower() for name in config.MANAGED_KEYS}


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
        if raw not in allowed:
            raise HTTPException(422, detail={"field": key, "message": f"只允许：{', '.join(sorted(allowed))}"})
        return raw
    if kind == "provider_ref":
        if not raw or not _known_provider_identity(raw):
            raise HTTPException(422, detail={
                "field": key, "message": "模型库中不存在该服务商条目，请先在模型中心添加",
            })
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
    episode_limit = int(merged.get("episode_video_inflight_limit") or 0)
    project_limit = int(merged.get("project_video_inflight_limit") or 0)
    if episode_limit and project_limit and episode_limit > project_limit:  # 0=自动，两侧都手填才比得出大小
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
    db = get_conn()
    # 只在本函数自己开启事务时提交，不在调用方的事务上隐式提交。
    caller_in_transaction = db.in_transaction
    db.execute(
        """CREATE TABLE IF NOT EXISTS monitor_audit(
               id TEXT PRIMARY KEY, ts REAL NOT NULL, action TEXT NOT NULL,
               object_type TEXT NOT NULL, object_id TEXT NOT NULL,
               outcome TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}'
           )"""
    )
    if db.in_transaction and not caller_in_transaction:
        db.commit()


def audit(action: str, object_type: str, object_id: str, outcome: str, detail: dict[str, Any] | None = None) -> None:
    ensure_monitor_audit_table()
    safe_detail = redact_monitor_value(detail or {})
    get_conn().execute(
        "INSERT INTO monitor_audit(id,ts,action,object_type,object_id,outcome,detail_json) VALUES(?,?,?,?,?,?,?)",
        (new_id("audit"), now(), action, object_type, object_id, outcome,
         json.dumps(safe_detail, ensure_ascii=False)),
    )
    get_conn().commit()
