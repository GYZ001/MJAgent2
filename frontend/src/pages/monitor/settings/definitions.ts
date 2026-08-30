import type { SettingSchema } from "../../../api";

export function normalizeDraft(spec: SettingSchema, raw: string) {
  if (spec.type === "boolean")
    return raw === "true" ? "true" : raw === "false" ? "false" : null;
  if (spec.type === "integer" || spec.type === "number") {
    if (!raw.trim()) return null;
    const value = Number(raw);
    if (
      !Number.isFinite(value) ||
      (spec.type === "integer" && !Number.isInteger(value)) ||
      value < (spec.min ?? -Infinity) ||
      value > (spec.max ?? Infinity)
    )
      return null;
    return spec.type === "integer"
      ? String(value)
      : String(Number(value.toPrecision(12)));
  }
  if (spec.type === "enum") return spec.options?.includes(raw) ? raw : null;
  if (!raw.trim() && spec.allow_empty) return "";
  return raw.trim() || null;
}

export interface SettingGroupDefinition {
  id: string;
  title: string;
  description: string;
  affects: string[];
  keys: string[];
}

export const SETTING_GROUP_DEFINITIONS: SettingGroupDefinition[] = [
  {
    id: "text-generation",
    title: "映射包与分镜生成",
    description:
      "控制同时运行多少集的剧本/分镜工作流，工作流内部实际发起多少个并发文本模型请求，以及输出校验失败后的修复重试次数。",
    affects: ["映射包批量生成", "分镜批量生成", "文本模型"],
    keys: [
      "text_generation_workflow_concurrency",
      "text_generation_concurrency",
      "max_repair_attempts",
    ],
  },
  {
    id: "video-flow",
    title: "视频生成与任务调度",
    description: "控制任务如何排队、提交、轮询，以及单集和项目的在途上限。",
    affects: ["视频生成", "任务队列", "运行中心"],
    keys: [
      "video_submit_concurrency",
      "video_inflight_limit",
      "video_poll_concurrency",
      "episode_video_inflight_limit",
      "project_video_inflight_limit",
      "reference_prepared_backlog",
      "video_ready_low_watermark",
      "video_ready_high_watermark",
      "media_scheduler_policy",
      "video_plan_confidence_floor",
      "video_concurrency",
      "auto_concurrency",
    ],
  },
  {
    id: "reference-images",
    title: "参考图与视觉生成",
    description: "控制人物、场景和镜头参考图的生成速度、批次与输入方式。",
    affects: ["人物定妆照", "场景参考图", "关键帧与视频输入"],
    keys: [
      "reference_pipeline_concurrency",
      "image_request_concurrency",
      "reference_shot_cohort_limit",
      "max_ref_images",
      "use_character_refs",
      "video_reference_batch_prompt",
      "video_reference_role_adaptive",
    ],
  },
  {
    id: "delivery-files",
    title: "下载、落盘与交付",
    description: "控制生成结果下载、本地校验和交付文件写入速度。",
    affects: ["媒体下载", "文件校验", "交付候选"],
    keys: [
      "download_concurrency",
      "finalize_concurrency",
      "provider_media_public_base_url",
      "provider_media_max_download_bytes",
    ],
  },
  {
    id: "budget-logs",
    title: "预算与运行记录",
    description: "控制单集费用保护，以及调用记录和错误记录的保留周期。",
    affects: ["预算限制", "调用日志", "故障排查"],
    keys: [
      "episode_cost_limit_cny",
      "provider_call_retention_days",
      "error_log_retention_days",
    ],
  },
  {
    id: "storyboard-safety",
    title: "分镜台编辑保护",
    description: "控制分镜结构编辑、原文重绑定和紧急只读保护。",
    affects: ["分镜台", "结构编辑", "原文绑定"],
    keys: [
      "storyboard_workspace_safe_readonly",
      "storyboard_structure_edit_enabled",
      "storyboard_source_rebind_enabled",
    ],
  },
];

export const SETTING_FIELD_IMPACTS: Record<string, string> = {
  text_generation_workflow_concurrency: "同一时间最多运行多少集的剧本或分镜工作流",
  text_generation_concurrency:
    "同一时间最多发起多少个真实文本模型请求（剧本、分镜等工作流共用同一个请求池）",
  video_submit_concurrency: "每次可同时提交多少个视频生成任务",
  video_inflight_limit: "供应商侧允许同时处理的视频任务总量",
  video_poll_concurrency: "同时查询多少个视频任务的完成状态",
  episode_video_inflight_limit: "单集可占用的视频生成槽位上限",
  project_video_inflight_limit: "单个项目可占用的视频生成槽位上限",
  reference_prepared_backlog: "视频生成前预先准备多少个镜头的参考图",
  video_ready_low_watermark: "就绪任务不足此数量时加快准备",
  video_ready_high_watermark: "就绪任务达到此数量后放缓准备",
  media_scheduler_policy: "任务队列选择下一项媒体工作的方式",
  video_plan_confidence_floor: "AI 模式计划低于此置信度时阻止付费提交",
  video_concurrency: "仍使用旧链路时的视频并发兼容值",
  auto_concurrency: "旧版自动生成流程的并发兼容值",
  reference_pipeline_concurrency: "同时推进多少条参考图准备流水线",
  image_request_concurrency: "同时向图片模型发送多少个请求",
  reference_shot_cohort_limit: "每批共同准备参考图的镜头数量",
  max_ref_images: "单个镜头最多携带多少张参考图",
  use_character_refs: "视频生成时是否携带人物定妆照",
  video_reference_batch_prompt: "是否批量生成视频参考图提示词",
  video_reference_role_adaptive: "是否根据镜头角色自动调整参考图策略",
  auto_retake_threshold: "兼容历史配置；质检分数不再触发自动重做",
  max_repair_attempts: "同一问题允许自动修复的最大次数",
  download_concurrency: "同时下载多少个模型生成结果",
  finalize_concurrency: "同时执行多少个文件落盘与校验任务",
  provider_media_public_base_url: "自有对象存储或 CDN 中项目媒体目录的公开基址",
  provider_media_max_download_bytes: "参考视频发布校验允许读取的最大文件大小",
  episode_cost_limit_cny: "单集达到此费用后暂停继续产生费用",
  provider_call_retention_days: "调用日志可在监制房查询的保留天数",
  error_log_retention_days: "错误记录可用于排障的保留天数",
  storyboard_workspace_safe_readonly: "紧急情况下把分镜台切换为只读",
  storyboard_structure_edit_enabled: "是否允许增删和调整分镜结构",
  storyboard_source_rebind_enabled: "是否允许重新绑定分镜对应的原文",
};
const SETTING_OPTION_LABELS: Record<string, Record<string, string>> = {
  media_scheduler_policy: {
    legacy: "兼容调度",
    stage_aware: "分阶段调度",
  },
};

export function settingOptionLabel(key: string, value: string) {
  return SETTING_OPTION_LABELS[key]?.[value] || value;
}

const LEGACY_QA_RETRY_SETTING_KEYS = new Set([
  "auto_retake_threshold",
  "video_hard_gate_enabled",
  "video_reference_gen_retries",
  "video_reference_consistency_retries",
]);

export function isLegacyQaRetrySettingKey(key: string): boolean {
  return LEGACY_QA_RETRY_SETTING_KEYS.has(key);
}

export function categorizeSettingKeys(keys: string[]) {
  const remaining = new Set(keys);
  const groups = SETTING_GROUP_DEFINITIONS.map((group) => ({
    ...group,
    keys: group.keys.filter((key) => {
      if (!remaining.has(key)) return false;
      remaining.delete(key);
      return true;
    }),
  })).filter((group) => group.keys.length > 0);
  if (remaining.size)
    groups.push({
      id: "other",
      title: "其他系统能力",
      description: "尚未归入常用业务流程的兼容或扩展设置。",
      affects: ["系统兼容能力"],
      keys: Array.from(remaining),
    });
  return groups;
}
