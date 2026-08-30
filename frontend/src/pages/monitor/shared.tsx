// 观测台（MonitorPage）拆分出的公共层：分区路由常量与工具、任务/调用日志的业务
// 标签与状态派生、以及两个跨分区复用的展示组件（DataBoundary/Pagination）。
// 模型中心（ModelsSection）与系统设置（SettingsSection）各自的专属常量/工具留在
// 各自文件里，不放这里——这里只放 Jobs/Calls/Overview 与整体路由共用的部分。
import { useEffect, useRef } from "react";
import { api } from "../../api";
import type { Call, Job } from "../../api";

export type MonitorSection =
  | "overview"
  | "runs"
  | "jobs"
  | "models"
  | "calls"
  | "settings";
export type BlockStatus = "loading" | "ready-empty" | "ready-data" | "error" | "stale";

export function monitorVideoModeLabel(mode?: string | null) {
  return ({
    REFERENCE_IMAGE_MODE: "参考图",
    FIRST_FRAME_MODE: "上一视频尾帧首帧",
    FIRST_LAST_FRAME_MODE: "首尾帧",
    VIDEO_INPUT_MODE: "视频参考",
  } as Record<string, string>)[mode || ""] || mode || "待执行";
}

export const SECTIONS: Array<{
  key: MonitorSection;
  label: string;
  description: string;
}> = [
  { key: "overview", label: "总览", description: "关键状态与异常" },
  { key: "jobs", label: "任务队列", description: "生成任务与失败" },
  { key: "models", label: "模型中心", description: "模型分配与连接" },
  { key: "calls", label: "调用日志", description: "分类、摘要与详情" },
  { key: "settings", label: "系统设置", description: "校验、预览与生效" },
];
export const SYSTEM_SECTION_DESCRIPTIONS: Partial<Record<MonitorSection, string>> = {
  overview: "查看全局健康、配置同步与项目级汇总",
  models: "管理模型分配、服务连接与可用性",
  settings: "校验、预览并应用全局系统策略",
};
export const VALID_SECTIONS = new Set(SECTIONS.map((item) => item.key));
export const JOB_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_retry: "等待重试",
  waiting_human: "等待人工确认",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
  paused_budget: "预算暂停",
  paused_external: "外部中断",
  cancelled: "已取消",
  recovering: "恢复排队中",
  recovered: "已自动续跑",
  superseded: "已被接管",
};
export const WORKFLOW_LABELS: Record<string, string> = {
  character_bible: "人物谱",
  character_references: "人物定妆照",
  scene_bible: "场景设定",
  scene_references: "场景参考图",
  episode_mapping: "分集规划",
  // workflow_type 仍叫 screenplay（改 key 会让历史 workflow_runs 认不出来），
  // 但这条链路产出的是映射包，不再产出剧本。这里报它现在实际在做的事。
  screenplay: "映射包",
  storyboard: "分镜",
  scene_generation: "关键帧生成",
  video_generation: "视频生成",
  episode_video_completion: "全片视频补齐",
  delivery: "交付",
  delivery_package: "交付候选",
};
export const CALL_STATUS_LABELS: Record<string, string> = {
  RUNNING: "调用中",
  INTERRUPTED: "已中断",
  RETRYING: "已自动重试",
  RECOVERED: "续跑已成功",
  OK: "成功",
  FAILED: "失败",
  TIMEOUT: "超时",
  NETWORK_ERROR: "网络错误",
  TASK_FAILED: "任务失败",
  QA_ERROR: "质检异常",
  REPAIR_STALLED: "修复停滞",
};
export const CALL_KIND_LABELS: Record<string, string> = {
  chat: "文本模型调用",
  vlm: "视觉模型调用",
  vlm_qa: "画面质检",
  video_create: "创建视频任务",
  video_poll: "轮询视频结果",
  image_generate: "生成图片",
  image_edit: "图生图",
  scene_image: "关键帧生成",
  screenplay_prompt: "映射包生成",
  plan_prompt: "分集规划",
  bible_prompt: "人物谱生成",
  references_prompt: "参考图规划",
  storyboard_shot_prompt: "逐镜分镜生成",
  storyboard_outline_prompt: "分镜大纲生成",
};
export function nowQuery() {
  return new URLSearchParams(window.location.search);
}
export function assertProjectScope<T extends { scope?: { project_id?: string } }>(payload: T, projectId?: string): T {
  if (projectId && payload.scope?.project_id !== projectId) {
    throw new Error("观测响应的项目范围与当前路由不一致，已拒绝渲染");
  }
  return payload;
}
export function querySection() {
  const tail = window.location.pathname.split("/").filter(Boolean).at(-1);
  if (tail === "runs") return "jobs";
  if (tail && VALID_SECTIONS.has(tail as MonitorSection))
    return tail as MonitorSection;
  const raw = nowQuery().get("section") as MonitorSection | null;
  if (raw === "runs") return "jobs";
  return raw && VALID_SECTIONS.has(raw) ? raw : "overview";
}
export function queryTarget(patch: Record<string, string | null>) {
  const params = nowQuery();
  const requestedSection = patch.section as MonitorSection | null | undefined;
  for (const [key, value] of Object.entries(patch)) {
    if (key === "section") continue;
    value ? params.set(key, value) : params.delete(key);
  }
  let pathname = window.location.pathname;
  if (requestedSection) {
    if (/^\/system(?:\/|$)/.test(pathname)) {
      pathname = `/system/${requestedSection}`;
    } else if (/\/observability(?:\/|$)/.test(pathname)) {
      pathname = pathname.replace(/\/observability(?:\/[^/]+)?$/, `/observability/${requestedSection}`);
    } else {
      params.set("section", requestedSection);
      pathname = "/monitor";
    }
  }
  return `${pathname}${params.toString() ? `?${params}` : ""}`;
}
export function writeQuery(patch: Record<string, string | null>, push = true) {
  const target = queryTarget(patch);
  window.history[push ? "pushState" : "replaceState"]({}, "", target);
  window.dispatchEvent(new Event("manju:locationchange"));
}
export function fmtTime(value?: number | null) {
  return value
    ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
    : "—";
}
export function jobStatusLabel(status: string) {
  return JOB_STATUS_LABELS[status] || "状态待确认";
}
function workflowLabel(raw?: string) {
  return raw ? WORKFLOW_LABELS[raw] || "其他业务任务" : "任务";
}
export function jobWorkLabel(job: Job) {
  const scope =
    job.episode_no != null
      ? `第${job.episode_no}集${job.shot_no != null ? ` · 镜${job.shot_no}` : ""} · `
      : "";
  return `${scope}${workflowLabel(job.workflow_type || job.kind)}`;
}
export function jobBusinessLabel(job: Job) {
  return [
    job.project_name || "未关联项目",
    jobWorkLabel(job),
    jobStatusLabel(job.status),
  ].join(" · ");
}
export function isProviderCreateUnresolved(
  job: Pick<Job, "reason_code" | "error">,
) {
  return job.reason_code === "VIDEO_PROVIDER_CREATE_UNRESOLVED"
    || Boolean(job.error?.includes("[VIDEO_PROVIDER_CREATE_UNRESOLVED]"));
}
export const PROVIDER_RESUBMISSION_WARNING =
  "未找到可继续查询的供应商任务编号。请先核对供应商后台；确认后会创建新的 operation ID，重新核算本集额度并建立独立预算 claim。原请求费用仍可能已经产生。";
export function jobNextStep(job: Job) {
  if (isProviderCreateUnresolved(job))
    return "供应商可能已接收创建请求；请先恢复原任务句柄，无法确认后再决定是否重新提交";
  if (job.status === "succeeded")
    return "任务已完成，无需处理";
  if (job.status === "running")
    return "正在执行，可查看进度或取消任务";
  if (job.status === "queued")
    return "正在等待执行，可查看排队详情或取消任务";
  if (job.status === "recovering")
    return "服务重启后已自动重新排队，等待 worker 领取继续执行，可查看详情或取消任务";
  if (job.status === "superseded")
    return "历史记录：已被后续尝试接管，真正在执行/已完成的是接管记录，本记录无需处理";
  if (job.status === "waiting_retry")
    return "正在等待自动重试，可查看失败原因";
  if (job.status === "waiting_human")
    return "等待人工确认，请打开详情处理";
  if (job.status === "paused_budget")
    return "因预算暂停，请查看范围和费用后恢复";
  if (job.status === "paused_external")
    return "任务被外部中断，可查看原因后恢复";
  if (job.status === "partial")
    return "部分步骤未完成，可查看详情后重试";
  if (job.status === "failed")
    return "任务未完成，可查看详情后重试";
  if (job.status === "cancelled")
    return "任务已取消；如需继续，请从详情重新发起";
  if (job.status === "recovered")
    return "任务已自动续跑完成，无需处理";
  return "状态待确认，请查看详情";
}
export function stampClass(status: string) {
  return ["succeeded", "recovered", "OK", "RECOVERED"].includes(status)
    ? "green"
    : [
          "failed",
          "partial",
          "paused_budget",
          "paused_external",
          "FAILED",
          "TIMEOUT",
          "NETWORK_ERROR",
          "TASK_FAILED",
          "QA_ERROR",
        ].includes(status)
      ? "red"
      : "gold";
}
export function callStatusLabel(status: string) {
  return CALL_STATUS_LABELS[status] || "状态待确认";
}
export function callPurpose(call: Call) {
  return CALL_KIND_LABELS[call.kind]
    || (call.category === "internal" ? "内部事件" : "其他业务调用");
}
export function callBusinessLabel(call: Call) {
  return [
    callPurpose(call),
    call.model_label || call.model || "未记录模型",
    callStatusLabel(call.effective_status),
  ].join(" · ");
}
export function callNextStep(call: Call) {
  if (call.error || !["OK", "RECOVERED"].includes(call.effective_status))
    return "调用未完成，可查看本次模型输入输出";
  return "查看本次模型输入输出";
}
export function blockStatus<T>(
  loading: boolean,
  error: string | null,
  data: T | null,
  empty: boolean,
): BlockStatus {
  if (loading && !data) return "loading";
  if (error && data) return "stale";
  if (error) return "error";
  return empty ? "ready-empty" : "ready-data";
}
export function encodeQuery(
  values: Record<string, string | number | boolean | undefined>,
) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "" && value !== false)
      params.set(key, String(value));
  });
  return params.toString();
}
export function track(
  name: string,
  dimensions: Record<string, string | number | boolean> = {},
  objectId = "",
) {
  void api
    .reportMonitorEvent(name, dimensions, objectId)
    .catch(() => undefined);
}

export function useBlockTelemetry(block: string, status: BlockStatus) {
  const previous = useRef<BlockStatus | null>(null);
  useEffect(() => {
    if (previous.current === status) return;
    previous.current = status;
    track("block_load", { block, result: status });
  }, [block, status]);
}

export function DataBoundary({
  status,
  error,
  updatedAt,
  onRetry,
  children,
  emptyLabel,
}: {
  status: BlockStatus;
  error?: string | null;
  updatedAt?: number;
  onRetry: () => void;
  children: React.ReactNode;
  emptyLabel: string;
}) {
  if (status === "loading")
    return (
      <div className="monitor-loading" role="status">
        正在加载，不以 0 或空状态代替…
      </div>
    );
  if (status === "error")
    return (
      <div className="monitor-state error" role="alert">
        数据加载失败：{error}
        <button onClick={onRetry}>重试</button>
      </div>
    );
  return (
    <>
      {status === "stale" && (
        <div className="monitor-state stale" role="status">
          数据可能过期，最后成功同步：{fmtTime(updatedAt)}。{error}
          <button onClick={onRetry}>立即刷新</button>
        </div>
      )}
      {status === "ready-empty" ? (
        <div className="empty monitor-table-empty">查询成功：{emptyLabel}</div>
      ) : (
        children
      )}
    </>
  );
}

export function Pagination({
  page,
  pageSize,
  total,
  pageCount,
  onPage,
  onPageSize,
}: {
  page: number;
  pageSize: number;
  total: number;
  pageCount: number;
  onPage: (page: number) => void;
  onPageSize: (size: number) => void;
}) {
  const start = total ? (page - 1) * pageSize + 1 : 0;
  const end = Math.min(page * pageSize, total);
  return (
    <div className="monitor-pagination" aria-label="分页">
      <span>
        显示 {start}–{end} / 共 {total} 条真实记录
      </span>
      <label>
        每页
        <select
          aria-label={`每页显示条数，当前 ${pageSize} 条`}
          value={pageSize}
          onChange={(e) => onPageSize(Number(e.target.value))}
        >
          {[10, 20, 40, 80].map((size) => (
            <option key={size}>{size}</option>
          ))}
        </select>
      </label>
      <button disabled={page <= 1}
        aria-label={page <= 1 ? "上一页，暂不可用：当前已是第一页" : "上一页"}
        onClick={() => onPage(page - 1)}>
        上一页
      </button>
      <b>
        {page} / {pageCount}
      </b>
      <button disabled={page >= pageCount}
        aria-label={page >= pageCount ? "下一页，暂不可用：当前已是最后一页" : "下一页"}
        onClick={() => onPage(page + 1)}>
        下一页
      </button>
    </div>
  );
}
