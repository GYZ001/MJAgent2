import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useNav, usePoll } from "../App";
import RunCenter from "../components/harness/RunCenter";
import SearchField from "../components/SearchField";
import { useFocusTrap } from "../hooks/useFocusTrap";

type MonitorSection =
  | "overview"
  | "runs"
  | "jobs"
  | "models"
  | "calls"
  | "settings";
type ModelKind = "text" | "vlm" | "video" | "image";
type BlockStatus = "loading" | "ready-empty" | "ready-data" | "error" | "stale";
type ProviderKey = string;

interface Job {
  id: string;
  source: "run" | "job" | "screenplay";
  run_id?: string;
  kind?: string;
  workflow_type?: string;
  scope_type?: string;
  scope_id?: string;
  project_id?: string;
  episode_id?: string;
  shot_id?: string;
  status: string;
  raw_status?: string;
  error?: string;
  reason_code?: string;
  state_revision?: number;
  shot_no?: number;
  episode_no?: number;
  episode_title?: string;
  project_name?: string;
  updated_at: number;
}
interface JobsSummary {
  counts: Record<string, number>;
  startup_recovery?: Record<string, number>;
  recent: Job[];
  total: number;
  server_time: number;
}
interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  page_count: number;
  server_time: number;
  query_ms?: number;
}
interface JobsPage extends Page<Job> {
  counts: Record<string, number>;
  startup_recovery?: Record<string, number>;
}
interface CallContext {
  project_id?: string;
  project_name?: string;
  episode_id?: string;
  episode_no?: number;
  episode_title?: string;
  shot_id?: string;
  shot_no?: number;
  stage?: string;
  purpose?: string;
  error_stage?: string;
}
interface Call {
  id: number;
  ts: number;
  kind: string;
  model?: string;
  model_label?: string;
  status: string;
  effective_status: string;
  category: "business" | "workflow" | "internal";
  http_status?: number;
  latency_ms: number;
  error?: string;
  run_id?: string;
  step_run_id?: string;
  trace_id?: string;
  operation_id?: string;
  attempt_no?: number;
  supersedes_call_id?: number;
  superseded_by_call_id?: number;
  context?: CallContext;
}
interface CallAggregate {
  key: string;
  project_id?: string;
  project_name: string;
  episode_no?: number;
  shot_no?: number;
  kind: string;
  root_cause: string;
  count: number;
  first_ts: number;
  last_ts: number;
  call_ids: number[];
  run_id?: string;
}
interface CallsPage extends Page<Call> {
  aggregates: CallAggregate[];
  failed_total: number;
}
interface CallDetail extends Call {
  request_json?: string;
  response_json?: string;
  meta?: string;
  request_json_size: number;
  response_json_size: number;
  meta_size: number;
  raw_access: boolean;
}
export interface SettingSchema {
  label: string;
  type: "integer" | "number" | "boolean" | "enum" | "string";
  default: string;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  options?: string[];
  immediate: boolean;
  experimental: boolean;
  max_length?: number;
}
interface SettingsView {
  values: Record<string, string>;
  effective: Record<string, string>;
  schema: Record<string, SettingSchema>;
  version: number;
  health: "ok" | "invalid";
  issues: Array<{ field: string; message: unknown }>;
  server_time: number;
  features: {
    overview_state_v2: boolean;
    jobs_query_v2: boolean;
    run_center_v2: boolean;
    call_detail_v2: boolean;
    settings_edit_v2: boolean;
  };
}
interface ModelOption {
  provider: ProviderKey;
  model: string;
  available: boolean;
}
interface ModelSelection {
  key: ModelKind;
  label: string;
  provider: ProviderKey;
  model: string;
  options: ModelOption[];
}
interface Health {
  ok: boolean;
  models?: Record<ModelKind, ModelSelection>;
}
interface CatalogModel {
  id: string;
  provider: ProviderKey;
  model: string;
  label: string;
  kinds: ModelKind[];
  builtin: boolean;
  provider_label?: string;
  base_url?: string;
  key_configured?: boolean;
}
interface ModelCatalog {
  items: CatalogModel[];
}

const SECTIONS: Array<{
  key: MonitorSection;
  label: string;
  description: string;
}> = [
  { key: "overview", label: "总览", description: "关键状态与异常" },
  { key: "runs", label: "运行中心", description: "步骤与人工门禁" },
  { key: "jobs", label: "任务队列", description: "生成任务与失败" },
  { key: "models", label: "模型中心", description: "模型分配与连接" },
  { key: "calls", label: "调用日志", description: "分类、摘要与详情" },
  { key: "settings", label: "系统设置", description: "校验、预览与生效" },
];
const VALID_SECTIONS = new Set(SECTIONS.map((item) => item.key));
const MODEL_ROWS: Array<{ key: ModelKind; label: string; note: string }> = [
  { key: "text", label: "Text 模型", note: "分集 / 剧本 / 分镜 / 文本修复" },
  { key: "vlm", label: "VLM 模型", note: "参考图评审 / 视频质检" },
  { key: "video", label: "视频模型", note: "Seedance 视频生成" },
  { key: "image", label: "图像模型", note: "Seedream 参考图 / 定妆照" },
];
const PROVIDER_LABELS: Record<string, string> = {
  hiagent: "火山",
  openrouter: "OpenRouter",
  bailian: "百炼",
  deepseek: "DeepSeek",
  zhipu: "智谱",
};
const JOB_STATUS_LABELS: Record<string, string> = {
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
};
const RUN_TO_JOB: Record<string, string> = {
  CREATED: "queued",
  RUNNING: "running",
  WAITING_RETRY: "waiting_retry",
  WAITING_HUMAN: "waiting_human",
  PAUSED_BUDGET: "paused_budget",
  PAUSED_EXTERNAL: "paused_external",
  SUCCEEDED: "succeeded",
  PARTIAL: "partial",
  FAILED: "failed",
  CANCELLED: "cancelled",
};
const WORKFLOW_LABELS: Record<string, string> = {
  character_bible: "人物谱",
  character_references: "人物定妆照",
  scene_bible: "场景圣经",
  scene_references: "场景参考图",
  episode_mapping: "分集映射",
  screenplay: "剧本",
  storyboard: "分镜",
  scene_generation: "关键帧生成",
  video_generation: "视频生成",
  episode_video_completion: "全片视频补齐",
  delivery: "交付",
  delivery_package: "交付包",
};
const CALL_STATUS_LABELS: Record<string, string> = {
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
const CALL_KIND_LABELS: Record<string, string> = {
  chat: "文本模型调用",
  vlm: "视觉模型调用",
  vlm_qa: "视频质检",
  video_create: "创建视频任务",
  video_poll: "轮询视频结果",
  image_generate: "生成图片",
  image_edit: "图生图",
  scene_image: "关键帧生成",
  screenplay_prompt: "剧本生成",
  plan_prompt: "分集规划",
  bible_prompt: "人物谱生成",
  references_prompt: "参考图规划",
  storyboard_shot_prompt: "逐镜分镜生成",
  storyboard_outline_prompt: "分镜大纲生成",
};
const CALL_CATEGORY_LABELS = {
  business: "业务模型调用",
  workflow: "工作流事件",
  internal: "指标与内部事件",
};

function nowQuery() {
  return new URLSearchParams(window.location.search);
}
function querySection() {
  const raw = nowQuery().get("section") as MonitorSection | null;
  return raw && VALID_SECTIONS.has(raw) ? raw : "overview";
}
function writeQuery(patch: Record<string, string | null>, push = true) {
  const params = nowQuery();
  for (const [key, value] of Object.entries(patch))
    value ? params.set(key, value) : params.delete(key);
  const target = `/monitor${params.toString() ? `?${params}` : ""}`;
  window.history[push ? "pushState" : "replaceState"]({}, "", target);
}
function fmtTime(value?: number | null) {
  return value
    ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
    : "—";
}
function jobStatusLabel(status: string) {
  return JOB_STATUS_LABELS[status] || "其他 / 内部状态";
}
function workflowLabel(raw?: string) {
  return raw ? WORKFLOW_LABELS[raw] || "其他业务任务" : "任务";
}
function jobWorkLabel(job: Job) {
  const scope =
    job.episode_no != null
      ? `第${job.episode_no}集${job.shot_no != null ? ` · 镜${job.shot_no}` : ""} · `
      : "";
  return `${scope}${workflowLabel(job.workflow_type || job.kind)}`;
}
function stampClass(status: string) {
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
function callStatusLabel(status: string) {
  return CALL_STATUS_LABELS[status] || "未知状态";
}
function callPurpose(call: Call) {
  const ctx = call.context || {};
  const scope =
    ctx.episode_no != null
      ? `第${ctx.episode_no}集${ctx.shot_no != null ? `第${ctx.shot_no}镜` : ""} · `
      : "";
  return `${scope}${ctx.purpose || CALL_KIND_LABELS[call.kind] || (call.category === "internal" ? "内部事件" : "其他业务调用")}`;
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
function encodeQuery(
  values: Record<string, string | number | boolean | undefined>,
) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "" && value !== false)
      params.set(key, String(value));
  });
  return params.toString();
}
function track(
  name: string,
  dimensions: Record<string, string | number | boolean> = {},
  objectId = "",
) {
  void api
    .post("/system/monitor/events", { name, dimensions, object_id: objectId })
    .catch(() => undefined);
}

function useBlockTelemetry(block: string, status: BlockStatus) {
  const previous = useRef<BlockStatus | null>(null);
  useEffect(() => {
    if (previous.current === status) return;
    previous.current = status;
    track("block_load", { block, result: status });
  }, [block, status]);
}

function DataBoundary({
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

function Pagination({
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
          value={pageSize}
          onChange={(e) => onPageSize(Number(e.target.value))}
        >
          {[10, 20, 40, 80].map((size) => (
            <option key={size}>{size}</option>
          ))}
        </select>
      </label>
      <button disabled={page <= 1} onClick={() => onPage(page - 1)}>
        上一页
      </button>
      <b>
        {page} / {pageCount}
      </b>
      <button disabled={page >= pageCount} onClick={() => onPage(page + 1)}>
        下一页
      </button>
    </div>
  );
}

function JsonSection({
  label,
  raw,
  size,
}: {
  label: string;
  raw?: string;
  size: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section className="call-json-section">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span>{label}</span>
        <small>
          {size.toLocaleString()} bytes · {open ? "收起" : "展开并渲染"}
        </small>
      </button>
      {open && <pre tabIndex={0}>{raw || "查询成功：此段无内容"}</pre>}
    </section>
  );
}

function CallDrawer({
  call,
  onClose,
  onRun,
}: {
  call: Call | CallDetail;
  onClose: () => void;
  onRun: (id: string) => void;
}) {
  const [detail, setDetail] = useState<CallDetail | null>(
    "request_json_size" in call ? call : null,
  );
  const [error, setError] = useState("");
  const [copied, setCopied] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const drawerRef = useFocusTrap(true, onClose);
  const load = useCallback(async () => {
    setError("");
    try {
      const next = (await api.get(`/system/calls/${call.id}`)) as CallDetail;
      setDetail(next);
      track(
        "call_detail",
        {
          size_bucket:
            next.request_json_size + next.response_json_size > 100000
              ? "large"
              : "normal",
        },
        String(call.id),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, [call.id]);
  useEffect(() => {
    if ("request_json_size" in call) setDetail(call);
    else void load();
  }, [call, load]);
  const copy = async (label: string, value?: string) => {
    try {
      await navigator.clipboard.writeText(value || "");
      setCopied(`${label}已复制（脱敏版本）`);
    } catch {
      setCopied("复制失败，请手动选择文本");
    }
  };
  const download = async () => {
    setDownloading(true);
    setDownloadError("");
    try {
      const blob = await api.download(`/system/calls/${call.id}/download`);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `provider-call-${call.id}-masked.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setDownloadError((e as Error).message);
    } finally {
      setDownloading(false);
    }
  };
  return (
    <div
      className="monitor-drawer-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.currentTarget === e.target) onClose();
      }}
    >
      <aside
        className="monitor-drawer call-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="call-title"
        ref={(node) => {
          drawerRef.current = node;
        }}
      >
        <header>
          <div>
            <span className="eyebrow">CALL DETAIL</span>
            <h3 id="call-title">{callPurpose(call)}</h3>
          </div>
          <button onClick={onClose} aria-label="关闭调用详情">
            ×
          </button>
        </header>
        <div className="call-summary-grid">
          <div>
            <span>模型</span>
            <b>{call.model_label || call.model || "未记录模型"}</b>
          </div>
          <div>
            <span>状态</span>
            <b>{callStatusLabel(call.effective_status)}</b>
          </div>
          <div>
            <span>耗时</span>
            <b>{(call.latency_ms / 1000).toFixed(1)} 秒</b>
          </div>
          <div>
            <span>输入规模</span>
            <b>
              {detail
                ? `${detail.request_json_size.toLocaleString()} bytes`
                : "详情加载后显示"}
            </b>
          </div>
          <div>
            <span>错误阶段</span>
            <b>{call.context?.error_stage || "未记录"}</b>
          </div>
          <div>
            <span>重试关系</span>
            <b>
              {call.supersedes_call_id
                ? `续接 #${call.supersedes_call_id}`
                : call.superseded_by_call_id
                  ? `续跑为 #${call.superseded_by_call_id}`
                  : "首次尝试"}
            </b>
          </div>
          <div>
            <span>下一步</span>
            <b>
              {call.run_id
                ? "查看关联 Run"
                : call.error
                  ? "按错误建议处理"
                  : "无需处理"}
            </b>
          </div>
        </div>
        {call.error && <div className="monitor-impact">{call.error}</div>}
        {call.run_id && (
          <button className="btn small" onClick={() => onRun(call.run_id!)}>
            查看关联 Run
          </button>
        )}
        {error && (
          <div className="monitor-state error" role="alert">
            详情加载失败：{error}
            <button onClick={load}>重试</button>
          </div>
        )}
        {!detail && !error && (
          <div className="monitor-loading">正在按需加载脱敏详情…</div>
        )}
        {detail && (
          <>
            <div className="monitor-state ready">
              本机路径、凭证及令牌形态字段已在服务端遮罩；常规入口不提供未遮罩原文。
            </div>
            <JsonSection
              label="发送内容"
              raw={detail.request_json}
              size={detail.request_json_size}
            />
            <JsonSection
              label="接收内容"
              raw={detail.response_json}
              size={detail.response_json_size}
            />
            <JsonSection
              label="元信息"
              raw={detail.meta}
              size={detail.meta_size}
            />
            <div className="monitor-drawer-actions">
              <span role="status">{copied}</span>
              <button
                onClick={() =>
                  void copy("详情", JSON.stringify(detail, null, 2))
                }
              >
                复制脱敏详情
              </button>
              <button disabled={downloading} onClick={() => void download()}>
                {downloading ? "下载中…" : "下载脱敏 JSON"}
              </button>
            </div>
            {downloadError && (
              <div className="monitor-state error" role="alert">
                下载失败：{downloadError}
                <button onClick={() => void download()}>重试下载</button>
              </div>
            )}
          </>
        )}
      </aside>
    </div>
  );
}

function JobDrawer({
  job,
  onClose,
  onChanged,
  onRun,
}: {
  job: Job;
  onClose: () => void;
  onChanged: () => void;
  onRun: (id: string) => void;
}) {
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [copied, setCopied] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const drawerRef = useFocusTrap(true, onClose);
  const load = useCallback(async () => {
    setError("");
    try {
      setDetail(
        (await api.get(
          `/system/jobs/${encodeURIComponent(job.id)}?source=${job.source}`,
        )) as Record<string, unknown>,
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, [job.id, job.source]);
  useEffect(() => {
    void load();
  }, [load]);
  const runAction = async (action: "retry" | "resume" | "cancel") => {
    setBusy(action);
    setError("");
    setActionMessage("");
    try {
      if (job.source === "run")
        await api.post(
          `/runs/${encodeURIComponent(job.run_id || job.id)}/${action}`,
        );
      else if (action === "cancel")
        await api.post(`/jobs/${encodeURIComponent(job.id)}/cancel`);
      else
        await api.post(`/system/jobs/${encodeURIComponent(job.id)}/retry`, {
          expected_version: job.state_revision ?? 0,
        });
      track("job_action", { action, object_status: job.status }, job.id);
      setActionMessage(
        `${jobWorkLabel(job)}：${action === "cancel" ? "取消请求已接受" : action === "resume" ? "恢复请求已接受，正在从检查点继续" : "重试请求已接受，正在排队"}`,
      );
      onChanged();
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
      setConfirmCancel(false);
    }
  };
  const sourceUrl = job.project_id
    ? job.episode_id
      ? `/projects/${encodeURIComponent(job.project_id)}/episodes/${encodeURIComponent(job.episode_id)}/${(job.workflow_type || job.kind) === "storyboard" ? "board" : (job.workflow_type || job.kind) === "screenplay" ? "script" : "wall"}`
      : `/projects/${encodeURIComponent(job.project_id)}/bible`
    : "";
  const canRetry =
    job.source !== "screenplay" &&
    ["failed", "partial", "cancelled"].includes(job.status);
  const canResume = [
    "paused_external",
    "paused_budget",
    "waiting_retry",
    "waiting_human",
  ].includes(job.status);
  const canCancel =
    job.source !== "screenplay" &&
    ["running", "queued", "recovering"].includes(job.status);
  return (
    <div
      className="monitor-drawer-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.currentTarget === e.target) onClose();
      }}
    >
      <aside
        className="monitor-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="job-title"
        ref={(node) => {
          drawerRef.current = node;
        }}
      >
        <header>
          <div>
            <span className="eyebrow">JOB DETAIL</span>
            <h3 id="job-title">{jobWorkLabel(job)}</h3>
          </div>
          <button onClick={onClose} aria-label="关闭任务详情">
            ×
          </button>
        </header>
        <div className="monitor-gate-summary">
          <b>{jobStatusLabel(job.status)}</b>
          <span>
            {job.project_name || "上下文未关联"} · {fmtTime(job.updated_at)}
          </span>
        </div>
        {job.error && (
          <div className="monitor-impact">
            <b>业务影响：</b>
            {job.error}
          </div>
        )}
        {error && (
          <div className="monitor-state error" role="alert">
            {error}
            <button onClick={load}>重试</button>
          </div>
        )}
        {actionMessage && (
          <div className="monitor-state ready" role="status">
            {actionMessage}
          </div>
        )}
        {!detail && !error && (
          <div className="monitor-loading">正在加载任务详情…</div>
        )}
        {detail && (
          <details>
            <summary>技术详情</summary>
            <pre>{JSON.stringify(detail, null, 2)}</pre>
          </details>
        )}
        <div className="monitor-drawer-actions">
          <span role="status">{copied}</span>
          {job.reason_code && (
            <button
              onClick={async () => {
                await navigator.clipboard.writeText(job.reason_code || "");
                setCopied(`错误码 ${job.reason_code} 已复制`);
              }}
            >
              复制错误码
            </button>
          )}
          {job.run_id && (
            <button onClick={() => onRun(job.run_id!)}>查看 Run</button>
          )}
          {sourceUrl && (
            <button
              onClick={() => {
                window.location.href = sourceUrl;
              }}
            >
              去源页面处理
            </button>
          )}
          {canRetry && (
            <button disabled={!!busy} onClick={() => void runAction("retry")}>
              {busy ? "处理中…" : "重试"}
            </button>
          )}
          {canResume && (
            <button disabled={!!busy} onClick={() => void runAction("resume")}>
              {busy ? "处理中…" : "从检查点恢复"}
            </button>
          )}
          {canCancel && !confirmCancel && (
            <button className="danger" onClick={() => setConfirmCancel(true)}>
              取消任务
            </button>
          )}
        </div>
        {canCancel && confirmCancel && (
          <div className="monitor-inline-confirm">
            取消运行中的任务可能仍产生上游费用。
            <button disabled={!!busy} onClick={() => void runAction("cancel")}>
              确认取消
            </button>
            <button onClick={() => setConfirmCancel(false)}>返回</button>
          </div>
        )}
        {!canRetry && job.status === "succeeded" && (
          <p className="hint">
            已完成任务不可重试；如需新版本请从源页面重新发起。
          </p>
        )}
      </aside>
    </div>
  );
}

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
  return raw.trim() || null;
}

interface SettingGroupDefinition {
  id: string;
  title: string;
  description: string;
  affects: string[];
  keys: string[];
}

const SETTING_GROUP_DEFINITIONS: SettingGroupDefinition[] = [
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
    id: "quality-repair",
    title: "质量检查与自动修复",
    description: "控制视觉质检、自动重做判断和失败后的修复尝试。",
    affects: ["视频质检", "评审墙", "自动重做"],
    keys: [
      "vlm_request_concurrency",
      "auto_qa",
      "auto_retake_threshold",
      "max_repair_attempts",
    ],
  },
  {
    id: "delivery-files",
    title: "下载、落盘与交付",
    description: "控制生成结果下载、本地校验和交付文件写入速度。",
    affects: ["媒体下载", "文件校验", "交付包"],
    keys: ["download_concurrency", "finalize_concurrency"],
  },
  {
    id: "budget-logs",
    title: "预算与运行记录",
    description: "控制单集费用保护，以及调用记录和错误记录的保留周期。",
    affects: ["预算门禁", "调用日志", "故障排查"],
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

const SETTING_FIELD_IMPACTS: Record<string, string> = {
  video_submit_concurrency: "每次可同时提交多少个视频生成任务",
  video_inflight_limit: "供应商侧允许同时处理的视频任务总量",
  video_poll_concurrency: "同时查询多少个视频任务的完成状态",
  episode_video_inflight_limit: "单集可占用的视频生成槽位上限",
  project_video_inflight_limit: "单个项目可占用的视频生成槽位上限",
  reference_prepared_backlog: "视频生成前预先准备多少个镜头的参考图",
  video_ready_low_watermark: "就绪任务不足此数量时加快准备",
  video_ready_high_watermark: "就绪任务达到此数量后放缓准备",
  media_scheduler_policy: "任务队列选择下一项媒体工作的方式",
  video_concurrency: "仍使用旧链路时的视频并发兼容值",
  auto_concurrency: "旧版自动生成流程的并发兼容值",
  reference_pipeline_concurrency: "同时推进多少条参考图准备流水线",
  image_request_concurrency: "同时向图片模型发送多少个请求",
  reference_shot_cohort_limit: "每批共同准备参考图的镜头数量",
  max_ref_images: "单个镜头最多携带多少张参考图",
  use_character_refs: "视频生成时是否携带人物定妆照",
  video_reference_batch_prompt: "是否批量生成视频参考图提示词",
  video_reference_role_adaptive: "是否根据镜头角色自动调整参考图策略",
  vlm_request_concurrency: "同时执行多少个视觉质量检查",
  auto_qa: "生成完成后是否自动进入质量检查",
  auto_retake_threshold: "质量分低于此值时触发自动重做",
  max_repair_attempts: "同一问题允许自动修复的最大次数",
  download_concurrency: "同时下载多少个模型生成结果",
  finalize_concurrency: "同时执行多少个文件落盘与校验任务",
  episode_cost_limit_cny: "单集达到此费用后暂停继续产生费用",
  provider_call_retention_days: "调用日志可在监制房查询的保留天数",
  error_log_retention_days: "错误记录可用于排障的保留天数",
  storyboard_workspace_safe_readonly: "紧急情况下把分镜台切换为只读",
  storyboard_structure_edit_enabled: "是否允许增删和调整分镜结构",
  storyboard_source_rebind_enabled: "是否允许重新绑定分镜对应的原文",
};

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

function SettingsPanel({
  state,
  loading,
  error,
  refresh,
  toast,
  registerGuard,
  editable,
}: {
  state: SettingsView | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<SettingsView | null>;
  toast: (message: string, error?: boolean) => void;
  registerGuard: (guard: (() => boolean) | null, unsaved?: boolean) => void;
  editable: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set());
  const [result, setResult] = useState<
    Array<{
      key: string;
      requested: string;
      effective: string;
      apply_mode: string;
    }>
  >([]);
  const values = state?.values || {};
  const schema = state?.schema || {};
  const visibleSchemaEntries = Object.entries(schema).filter(
    ([key]) =>
      !key.startsWith("model_") &&
      !key.includes("_model_") &&
      key !== "model_route",
  );
  const visibleSchema = Object.fromEntries(visibleSchemaEntries);
  const settingGroups = categorizeSettingKeys(
    visibleSchemaEntries.map(([key]) => key),
  );
  const normalized = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(draft).map(([key, raw]) => [
          key,
          normalizeDraft(schema[key], raw),
        ]),
      ),
    [draft, schema],
  );
  const fieldErrors = useMemo(() => {
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(normalized))
      if (value == null)
        next[key] =
          `请输入合法的${schema[key]?.type === "integer" ? "整数" : "值"}`;
    const merged = {
      ...values,
      ...Object.fromEntries(
        Object.entries(normalized).filter(([, value]) => value != null),
      ),
    } as Record<string, string>;
    if (
      Number(merged.video_ready_low_watermark) >
      Number(merged.video_ready_high_watermark)
    )
      next.video_ready_high_watermark = "高水位不能低于低水位";
    if (
      Number(merged.episode_video_inflight_limit) >
      Number(merged.project_video_inflight_limit)
    )
      next.project_video_inflight_limit = "单项目上限不能低于单集上限";
    return next;
  }, [normalized, schema, values]);
  const changed = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(normalized).filter(
          ([key, value]) => value != null && value !== values[key],
        ),
      ) as Record<string, string>,
    [normalized, values],
  );
  const dirty =
    Object.keys(changed).length > 0 || Object.keys(fieldErrors).length > 0;
  useEffect(() => {
    const guard = dirty
      ? () => window.confirm("系统设置仍有未保存修改，确定离开吗？")
      : null;
    registerGuard(guard, dirty);
    const before = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => {
      registerGuard(null, false);
      window.removeEventListener("beforeunload", before);
    };
  }, [dirty, registerGuard]);
  useEffect(() => {
    if (state)
      setDraft((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([key]) => key in state.schema),
        ),
      );
  }, [state?.version]); // eslint-disable-line react-hooks/exhaustive-deps
  const edit = (key: string, raw: string) => {
    const normalizedValue = normalizeDraft(schema[key], raw);
    setDraft((current) => {
      const next = { ...current };
      if (normalizedValue != null && normalizedValue === values[key])
        delete next[key];
      else next[key] = raw;
      return next;
    });
    setPreview(false);
    setResult([]);
    setSaveError("");
  };
  const save = async () => {
    if (
      !state ||
      !Object.keys(changed).length ||
      Object.keys(fieldErrors).length
    )
      return;
    setSaving(true);
    setSaveError("");
    try {
      const response = await api.put("/settings", {
        version: state.version,
        patch: changed,
      });
      setResult(response.items || []);
      setDraft({});
      setPreview(false);
      track("settings_submit", {
        result: "succeeded",
        filter_count: Object.keys(changed).length,
      });
      await refresh();
      const requiresRestart = (response.items || []).some(
        (item: { apply_mode: string }) => item.apply_mode === "restart",
      );
      toast(
        requiresRestart
          ? `系统设置 v${response.version} 已保存；标记项将在重启后生效`
          : `系统设置 v${response.version} 已整体即时生效`,
      );
    } catch (e) {
      track("settings_submit", { result: "failed" });
      setSaveError((e as Error).message);
      toast((e as Error).message, true);
    } finally {
      setSaving(false);
    }
  };
  const status = blockStatus(loading, error, state, !state);
  const toggleGroup = (groupId: string) =>
    setOpenGroups((current) => {
      const next = new Set(current);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  return (
    <section className="card monitor-section monitor-settings">
      <div className="monitor-section-head compact">
        <div>
          <span className="eyebrow">SYSTEM POLICY</span>
          <h2>系统设置</h2>
        </div>
        <p>按功能展开 · 每项说明影响范围 · 修改后统一预览保存</p>
      </div>
      {!editable && (
        <div className="monitor-state stale" role="status">
          设置编辑新链路已由发布开关切为只读；现有运行时配置保持不变。
        </div>
      )}
      <DataBoundary
        status={status}
        error={error}
        updatedAt={state?.server_time}
        onRetry={() => void refresh()}
        emptyLabel="没有可维护设置"
      >
        {state && (
          <>
            {state.health === "invalid" && (
              <div className="monitor-state error" role="alert">
                检测到历史非法配置，运行时健康状态不可视为正常：
                {state.issues.map((issue) => issue.field).join("、")}
              </div>
            )}
            <div className="setting-category-list">
              {settingGroups.map((group) => {
                const expanded = openGroups.has(group.id);
                const changedCount = group.keys.filter(
                  (key) => key in changed,
                ).length;
                const errorCount = group.keys.filter(
                  (key) => key in fieldErrors,
                ).length;
                const panelId = `settings-panel-${group.id}`;
                return (
                  <section
                    className={`setting-category ${expanded ? "open" : ""}`}
                    id={`settings-group-${group.id}`}
                    key={group.id}
                  >
                    <button
                      type="button"
                      className="setting-category-toggle"
                      aria-expanded={expanded}
                      aria-controls={panelId}
                      onClick={() => toggleGroup(group.id)}
                    >
                      <span>
                        <b>{group.title}</b>
                        <small>{group.description}</small>
                      </span>
                      <span className="setting-category-meta">
                        {group.affects.map((effect) => (
                          <em key={effect}>{effect}</em>
                        ))}
                        <strong>
                          {errorCount
                            ? `${errorCount} 项错误`
                            : changedCount
                              ? `${changedCount} 项已改`
                              : `${group.keys.length} 项设置`}
                        </strong>
                        <i aria-hidden="true">⌄</i>
                      </span>
                    </button>
                    {expanded && (
                      <div className="monitor-settings-grid" id={panelId}>
                        {group.keys.map((key) => {
                          const spec = visibleSchema[key];
                          const current =
                            draft[key] ?? values[key] ?? spec.default;
                          const id = `setting-${key}`;
                          return (
                            <div
                              key={key}
                              className={`setting-field ${fieldErrors[key] ? "invalid" : ""}`}
                            >
                              <label htmlFor={id}>
                                <b>
                                  {spec.label}
                                  {spec.experimental ? <em>实验</em> : ""}
                                </b>
                                <small className="setting-field-impact">
                                  影响：
                                  {SETTING_FIELD_IMPACTS[key] ||
                                    group.affects.join("、")}
                                </small>
                                <small>
                                  {spec.unit || "无单位"} ·{" "}
                                  {spec.type === "boolean"
                                    ? "开关"
                                    : spec.type === "enum"
                                      ? `可选 ${spec.options?.join(" / ")}`
                                      : spec.type === "string"
                                        ? `最多 ${spec.max_length || 1000} 字符`
                                        : `${spec.min}~${spec.max}，步长 ${spec.step}`}
                                </small>
                              </label>
                              {spec.type === "boolean" ? (
                                <input
                                  id={id}
                                  type="checkbox"
                                  checked={current === "true"}
                                  onChange={(e) =>
                                    edit(
                                      key,
                                      e.target.checked ? "true" : "false",
                                    )
                                  }
                                  disabled={!editable}
                                />
                              ) : spec.type === "enum" ? (
                                <select
                                  id={id}
                                  value={current}
                                  onChange={(e) => edit(key, e.target.value)}
                                  disabled={!editable}
                                >
                                  {spec.options?.map((option) => (
                                    <option key={option}>{option}</option>
                                  ))}
                                </select>
                              ) : (
                                <input
                                  id={id}
                                  type={
                                    spec.type === "string" ? "text" : "number"
                                  }
                                  min={spec.min}
                                  max={spec.max}
                                  step={spec.step}
                                  value={current}
                                  onChange={(e) => edit(key, e.target.value)}
                                  aria-invalid={!!fieldErrors[key]}
                                  disabled={!editable}
                                />
                              )}
                              {fieldErrors[key] && (
                                <b className="setting-field-error" role="alert">
                                  {fieldErrors[key]}
                                </b>
                              )}
                              <details className="setting-field-technical">
                                <summary>技术信息</summary>
                                <code>
                                  {key} · 默认 {spec.default} ·{" "}
                                  {spec.immediate ? "即时生效" : "需重启生效"}
                                </code>
                              </details>
                              <button
                                type="button"
                                disabled={!editable || draft[key] === undefined}
                                onClick={() =>
                                  setDraft((currentDraft) => {
                                    const next = { ...currentDraft };
                                    delete next[key];
                                    return next;
                                  })
                                }
                              >
                                重置此项
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </section>
                );
              })}
            </div>
            {preview && (
              <div
                className="settings-preview"
                role="dialog"
                aria-label="设置差异预览"
              >
                <h3>确认设置差异</h3>
                {Object.entries(changed).map(([key, value]) => (
                  <div key={key}>
                    <b>{schema[key].label}</b>
                    <span>
                      {values[key]} → {value}
                    </span>
                    <small>
                      {schema[key].immediate
                        ? "保存成功后即时生效"
                        : "保存后需重启生效"}
                    </small>
                  </div>
                ))}
              </div>
            )}
            {result.length > 0 && (
              <div className="settings-result">
                <b>权威生效结果</b>
                {result.map((item) => (
                  <span key={item.key}>
                    {schema[item.key]?.label || item.key}：请求 {item.requested}{" "}
                    / 有效 {item.effective}（
                    {item.apply_mode === "immediate" ? "即时" : "需重启"}）
                  </span>
                ))}
              </div>
            )}
            {saveError && (
              <div className="monitor-state error" role="alert">
                保存失败：{saveError}。草稿仍保留，可修正后重试。
              </div>
            )}
            <div className="monitor-settings-actions">
              <span>
                {Object.keys(changed).length
                  ? `${Object.keys(changed).length} 项合法改动待保存`
                  : Object.keys(fieldErrors).length
                    ? `${Object.keys(fieldErrors).length} 项校验错误`
                    : "当前没有未保存修改"}
              </span>
              <button
                onClick={() => {
                  setDraft({});
                  setPreview(false);
                }}
                disabled={!editable || !dirty}
              >
                全部重置
              </button>
              {!preview ? (
                <button
                  className="btn primary small"
                  disabled={
                    !editable ||
                    !Object.keys(changed).length ||
                    !!Object.keys(fieldErrors).length
                  }
                  onClick={() => {
                    setPreview(true);
                    track("settings_preview", {
                      filter_count: Object.keys(changed).length,
                    });
                  }}
                >
                  预览差异
                </button>
              ) : (
                <button
                  className="btn primary small"
                  disabled={!editable || saving}
                  onClick={() => void save()}
                >
                  {saving ? "整体提交中…" : "批准并保存全部"}
                </button>
              )}
            </div>
          </>
        )}
      </DataBoundary>
    </section>
  );
}

function ModelCenter({
  health,
  catalog,
  settings,
  refreshHealth,
  refreshCatalog,
  refreshSettings,
  toast,
}: {
  health: Health | null;
  catalog: ModelCatalog | null;
  settings: SettingsView | null;
  refreshHealth: () => Promise<Health | null>;
  refreshCatalog: () => Promise<ModelCatalog | null>;
  refreshSettings: () => Promise<SettingsView | null>;
  toast: (message: string, error?: boolean) => void;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [library, setLibrary] = useState(false);
  const [search, setSearch] = useState("");
  const [provider, setProvider] = useState("");
  const [capability, setCapability] = useState("");
  const [connection, setConnection] = useState("");
  const [message, setMessage] = useState("");
  const [credential, setCredential] = useState<CatalogModel | null>(null);
  const [credentialDraft, setCredentialDraft] = useState({
    base_url: "",
    api_key: "",
  });
  const [testedSignature, setTestedSignature] = useState("");
  const [testing, setTesting] = useState(false);
  const [newModel, setNewModel] = useState(false);
  const [editingModel, setEditingModel] = useState<CatalogModel | null>(null);
  const [modelDraft, setModelDraft] = useState({
    label: "",
    provider_label: "",
    base_url: "",
    api_key: "",
    model: "",
    kinds: ["text"] as ModelKind[],
  });
  const [newTested, setNewTested] = useState("");
  const libraryRef = useFocusTrap(library, () => setLibrary(false));
  const credentialRef = useFocusTrap(!!credential, () => setCredential(null));
  const newRef = useFocusTrap(newModel || !!editingModel, () => {
    setNewModel(false);
    setEditingModel(null);
  });
  const catalogLabel = (providerKey: string, model: string) =>
    catalog?.items.find(
      (item) => item.provider === providerKey && item.model === model,
    )?.label ||
    model ||
    "未配置";
  const assignmentPatch = useMemo(() => {
    const patch: Record<string, string> = {};
    for (const row of MODEL_ROWS) {
      const selection = health?.models?.[row.key];
      if (!selection) continue;
      const providerKey =
        draft[`model_${row.key}_provider`] ?? selection.provider;
      if (providerKey !== selection.provider)
        patch[`model_${row.key}_provider`] = providerKey;
      const modelKey = `${providerKey === "hiagent" ? "hiagent" : providerKey}_model_${row.key}`;
      const modelValue = draft[modelKey];
      if (
        modelValue &&
        modelValue !==
          selection.options.find((option) => option.provider === providerKey)
            ?.model
      )
        patch[modelKey] = modelValue;
    }
    return patch;
  }, [draft, health]);
  const saveAssignments = async () => {
    if (!settings) return;
    try {
      const response = await api.put("/settings", {
        version: settings.version,
        patch: assignmentPatch,
      });
      setDraft({});
      await Promise.all([refreshHealth(), refreshSettings()]);
      const scope = response.effect_scope;
      toast(
        scope?.new_tasks && scope?.queued_not_started && !scope?.running_tasks
          ? "模型分配已保存；新任务和未启动队列使用新模型，运行中任务保持启动快照"
          : "模型分配已保存；请以服务端返回的生效范围为准",
      );
    } catch (e) {
      toast((e as Error).message, true);
    }
  };
  const filtered = (catalog?.items || []).filter((item) => {
    const q = search.trim().toLowerCase();
    return (
      (!q || `${item.label} ${item.model}`.toLowerCase().includes(q)) &&
      (!provider || item.provider === provider) &&
      (!capability || item.kinds.includes(capability as ModelKind)) &&
      (!connection || (connection === "configured") === !!item.key_configured)
    );
  });
  const groupedModels = filtered.reduce<Record<string, CatalogModel[]>>(
    (groups, item) => {
      (groups[item.provider] ||= []).push(item);
      return groups;
    },
    {},
  );
  const defaults: Record<string, string> = {
    openrouter: "https://openrouter.ai/api/v1",
    bailian: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    deepseek: "https://api.deepseek.com/v1",
    zhipu: "https://open.bigmodel.cn/api/paas/v4",
  };
  const credentialSignature = JSON.stringify(credentialDraft);
  const newSignature = JSON.stringify(modelDraft);
  return (
    <section className="card model-hub monitor-section">
      <div className="model-hub-head">
        <div>
          <h3>模型中心</h3>
          <p>四类职责、友好名称与生效范围清晰可见。</p>
        </div>
        <button
          className="btn primary small"
          onClick={() => {
            setEditingModel(null);
            setModelDraft({
              label: "",
              provider_label: "",
              base_url: "",
              api_key: "",
              model: "",
              kinds: ["text"],
            });
            setNewTested("");
            setNewModel(true);
          }}
        >
          添加模型
        </button>
        <button className="btn small" onClick={() => setLibrary(true)}>
          管理模型
        </button>
      </div>
      <div className="model-grid">
        {MODEL_ROWS.map((row) => {
          const selection = health?.models?.[row.key];
          if (!selection)
            return (
              <div className="monitor-loading" key={row.key}>
                正在加载 {row.label}…
              </div>
            );
          const providerKey =
            draft[`model_${row.key}_provider`] ?? selection.provider;
          const options = selection.options.filter(
            (option) => option.available,
          );
          const models =
            catalog?.items.filter(
              (item) =>
                item.provider === providerKey && item.kinds.includes(row.key),
            ) || [];
          const currentModel =
            draft[`${providerKey}_model_${row.key}`] ??
            options.find((option) => option.provider === providerKey)?.model ??
            "";
          return (
            <div className="model-row" key={row.key}>
              <div className="model-name">
                <span
                  className={`model-kind-icon ${row.key}`}
                  aria-hidden="true"
                >
                  {row.key[0].toUpperCase()}
                </span>
                <b>{row.label}</b>
                <span>{row.note}</span>
              </div>
              <div className="model-selects">
                <label className="model-select-field">
                  <span>服务</span>
                  <select
                    aria-label={`${row.label}服务商`}
                    value={providerKey}
                    disabled={row.key === "video" || row.key === "image"}
                    onChange={(e) =>
                      setDraft((value) => ({
                        ...value,
                        [`model_${row.key}_provider`]: e.target.value,
                      }))
                    }
                  >
                    {options.map((option) => (
                      <option key={option.provider} value={option.provider}>
                        {PROVIDER_LABELS[option.provider] ||
                          catalog?.items.find(
                            (item) => item.provider === option.provider,
                          )?.provider_label ||
                          option.provider}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="model-select-field">
                  <span>模型</span>
                  <select
                    aria-label={`${row.label}目标模型`}
                    value={currentModel}
                    onChange={(e) =>
                      setDraft((value) => ({
                        ...value,
                        [`${providerKey}_model_${row.key}`]: e.target.value,
                      }))
                    }
                  >
                    {models.length ? (
                      models.map((item) => (
                        <option key={item.id} value={item.model}>
                          {item.label} · {item.model}
                        </option>
                      ))
                    ) : (
                      <option value={currentModel}>
                        {catalogLabel(providerKey, currentModel)}
                      </option>
                    )}
                  </select>
                </label>
              </div>
              <div className="model-current">
                <span className="model-live-dot" />
                当前运行
                <strong>
                  {PROVIDER_LABELS[selection.provider] ||
                    catalog?.items.find(
                      (item) => item.provider === selection.provider,
                    )?.provider_label ||
                    "自定义服务"}
                </strong>
                <code>
                  {catalogLabel(selection.provider, selection.model)} ·{" "}
                  {selection.model}
                </code>
                <small>
                  保存后：新任务与尚未启动的排队任务使用新分配；运行中任务保持启动快照。
                </small>
              </div>
            </div>
          );
        })}
      </div>
      <div className="model-actions">
        <span>
          {Object.keys(assignmentPatch).length
            ? `${Object.keys(assignmentPatch).length} 项未保存分配`
            : "没有未保存分配"}
        </span>
        <button
          className="btn primary small"
          disabled={!Object.keys(assignmentPatch).length}
          onClick={() => void saveAssignments()}
        >
          保存模型分配
        </button>
      </div>
      {library && (
        <div
          className="model-modal-backdrop"
          role="presentation"
          onMouseDown={(e) => {
            if (e.currentTarget === e.target) setLibrary(false);
          }}
        >
          <section
            className="model-modal model-library-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="library-title"
            ref={(node) => {
              libraryRef.current = node;
            }}
          >
            <div className="model-modal-head">
              <div>
                <span className="eyebrow">MODEL LIBRARY</span>
                <h2 id="library-title">管理模型</h2>
                <p>搜索、分组与连接状态筛选不会修改模型数据。</p>
              </div>
              <button
                className="model-modal-close"
                onClick={() => setLibrary(false)}
                aria-label="关闭模型库"
              >
                ×
              </button>
            </div>
            <div className="monitor-toolbar">
              <SearchField
                value={search}
                onChange={setSearch}
                placeholder="搜索名称或模型 ID"
                ariaLabel="搜索模型库"
              />
              <label>
                <span>服务商</span>
                <select
                  value={provider}
                  onChange={(e) => setProvider(e.target.value)}
                >
                  <option value="">全部</option>
                  {Array.from(
                    new Set(
                      (catalog?.items || []).map((item) => item.provider),
                    ),
                  ).map((item) => (
                    <option key={item} value={item}>
                      {PROVIDER_LABELS[item] ||
                        catalog?.items.find((model) => model.provider === item)
                          ?.provider_label ||
                        "自定义服务"}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>能力</span>
                <select
                  value={capability}
                  onChange={(e) => setCapability(e.target.value)}
                >
                  <option value="">全部</option>
                  {MODEL_ROWS.map((row) => (
                    <option key={row.key} value={row.key}>
                      {row.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>连接</span>
                <select
                  value={connection}
                  onChange={(e) => setConnection(e.target.value)}
                >
                  <option value="">全部</option>
                  <option value="configured">已配置</option>
                  <option value="pending">仅看待配置</option>
                </select>
              </label>
            </div>
            <div className="model-library-list">
              {Object.entries(groupedModels).map(([providerKey, items]) => (
                <section className="model-provider-group" key={providerKey}>
                  <h3>
                    {PROVIDER_LABELS[providerKey] ||
                      items[0]?.provider_label ||
                      providerKey}
                    <small>{items.length} 个模型</small>
                  </h3>
                  {items.map((item) => (
                    <div className="model-library-item" key={item.id}>
                      <div className="model-library-main">
                        <div>
                          <b>{item.label}</b>
                          {!item.builtin && (
                            <span className="stamp gold">自定义</span>
                          )}
                        </div>
                        <code>
                          {PROVIDER_LABELS[item.provider] ||
                            item.provider_label ||
                            item.provider}{" "}
                          · {item.model}
                        </code>
                        <span>{item.kinds.join(" / ")}</span>
                      </div>
                      <span
                        className={`stamp ${item.key_configured ? "green" : "red"}`}
                      >
                        {item.key_configured ? "连接已配置" : "待配置"}
                      </span>
                      <div className="model-library-actions">
                        <button
                          aria-label={`测试 ${item.label}`}
                          onClick={async () => {
                            setMessage(`正在测试 ${item.label}…`);
                            try {
                              const result = await api.post(
                                `/models/${encodeURIComponent(item.id)}/test`,
                              );
                              setMessage(
                                `${item.label} 可用 · ${result.latency_ms} ms`,
                              );
                            } catch (e) {
                              setMessage((e as Error).message);
                            }
                          }}
                        >
                          测试
                        </button>
                        <button
                          aria-label={`配置 ${item.label} 的连接`}
                          onClick={() => {
                            setLibrary(false);
                            setCredential(item);
                            setCredentialDraft({
                              base_url:
                                item.base_url || defaults[item.provider] || "",
                              api_key: "",
                            });
                            setTestedSignature("");
                          }}
                        >
                          连接
                        </button>
                        {!item.builtin && (
                          <button
                            aria-label={`编辑 ${item.label}`}
                            onClick={() => {
                              setLibrary(false);
                              setEditingModel(item);
                              setModelDraft({
                                label: item.label,
                                provider_label:
                                  item.provider_label || item.provider,
                                base_url: item.base_url || "",
                                api_key: "",
                                model: item.model,
                                kinds: item.kinds,
                              });
                              setNewTested("");
                            }}
                          >
                            编辑
                          </button>
                        )}
                        {!item.builtin && (
                          <button
                            className="danger"
                            aria-label={`删除 ${item.label}`}
                            onClick={async () => {
                              if (
                                !window.confirm(
                                  `确认删除 ${item.label}？在用模型会被后端阻止。`,
                                )
                              )
                                return;
                              try {
                                await api.del(
                                  `/models/${encodeURIComponent(item.id)}`,
                                );
                                await refreshCatalog();
                                toast(`${item.label} 已删除`);
                              } catch (e) {
                                toast((e as Error).message, true);
                              }
                            }}
                          >
                            删除
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </section>
              ))}
            </div>
            {message && (
              <div className="model-library-feedback" role="status">
                {message}
              </div>
            )}
          </section>
        </div>
      )}
      {credential && (
        <div className="model-modal-backdrop" role="presentation">
          <section
            className="model-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="credential-title"
            ref={(node) => {
              credentialRef.current = node;
            }}
          >
            <div className="model-modal-head">
              <div>
                <span className="eyebrow">MODEL CONNECTION</span>
                <h2 id="credential-title">{credential.label} 的连接</h2>
                <p>密钥留空表示不修改现有值；接口不会回显明文。</p>
              </div>
              <button
                className="model-modal-close"
                onClick={() => setCredential(null)}
                aria-label="关闭连接配置"
              >
                ×
              </button>
            </div>
            <div className="model-form-grid">
              <label className="model-form-field model-form-wide">
                <span>Base URL</span>
                <input
                  value={credentialDraft.base_url}
                  onChange={(e) => {
                    setCredentialDraft((value) => ({
                      ...value,
                      base_url: e.target.value,
                    }));
                    setTestedSignature("");
                  }}
                />
              </label>
              <label className="model-form-field model-form-wide">
                <span>该模型专用 API Key</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={credentialDraft.api_key}
                  placeholder={
                    credential.key_configured
                      ? "留空则不修改现有密钥"
                      : "输入 API Key"
                  }
                  onChange={(e) => {
                    setCredentialDraft((value) => ({
                      ...value,
                      api_key: e.target.value,
                    }));
                    setTestedSignature("");
                  }}
                />
              </label>
            </div>
            <div className="model-modal-actions">
              <button
                disabled={testing}
                onClick={async () => {
                  setTesting(true);
                  try {
                    await api.post(
                      `/models/${encodeURIComponent(credential.id)}/test`,
                      credentialDraft,
                    );
                    setTestedSignature(credentialSignature);
                    toast(`${credential.label} 连接测试通过`);
                  } catch (e) {
                    toast((e as Error).message, true);
                  } finally {
                    setTesting(false);
                  }
                }}
              >
                {testing ? "测试中…" : "测试连接"}
              </button>
              <button
                className="btn primary small"
                disabled={testedSignature !== credentialSignature}
                onClick={async () => {
                  try {
                    await api.put(
                      `/models/${encodeURIComponent(credential.id)}/credentials`,
                      credentialDraft,
                    );
                    await refreshCatalog();
                    setCredential(null);
                    toast(`${credential.label} 的连接已保存`);
                  } catch (e) {
                    toast((e as Error).message, true);
                  }
                }}
              >
                保存连接
              </button>
            </div>
          </section>
        </div>
      )}
      {(newModel || editingModel) && (
        <div className="model-modal-backdrop" role="presentation">
          <section
            className="model-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="new-model-title"
            ref={(node) => {
              newRef.current = node;
            }}
          >
            <div className="model-modal-head">
              <div>
                <span className="eyebrow">CUSTOM MODEL</span>
                <h2 id="new-model-title">
                  {editingModel
                    ? "编辑 OpenAI 兼容模型"
                    : "添加 OpenAI 兼容模型"}
                </h2>
                <p>保存前必须通过当前配置的连接测试。</p>
              </div>
              <button
                className="model-modal-close"
                onClick={() => {
                  setNewModel(false);
                  setEditingModel(null);
                }}
                aria-label={editingModel ? "关闭编辑模型" : "关闭添加模型"}
              >
                ×
              </button>
            </div>
            <div className="model-form-grid">
              <label className="model-form-field">
                <span>显示名称</span>
                <input
                  value={modelDraft.label}
                  onChange={(e) => {
                    setModelDraft((value) => ({
                      ...value,
                      label: e.target.value,
                    }));
                    setNewTested("");
                  }}
                />
              </label>
              <label className="model-form-field">
                <span>服务名称</span>
                <input
                  value={modelDraft.provider_label}
                  onChange={(e) => {
                    setModelDraft((value) => ({
                      ...value,
                      provider_label: e.target.value,
                    }));
                    setNewTested("");
                  }}
                />
              </label>
              <label className="model-form-field model-form-wide">
                <span>Base URL</span>
                <input
                  value={modelDraft.base_url}
                  onChange={(e) => {
                    setModelDraft((value) => ({
                      ...value,
                      base_url: e.target.value,
                    }));
                    setNewTested("");
                  }}
                />
              </label>
              <label className="model-form-field">
                <span>模型 ID</span>
                <input
                  value={modelDraft.model}
                  onChange={(e) => {
                    setModelDraft((value) => ({
                      ...value,
                      model: e.target.value,
                    }));
                    setNewTested("");
                  }}
                />
              </label>
              <label className="model-form-field">
                <span>API Key</span>
                <input
                  type="password"
                  autoComplete="new-password"
                  value={modelDraft.api_key}
                  placeholder={
                    editingModel ? "留空则不修改现有密钥" : "输入 API Key"
                  }
                  onChange={(e) => {
                    setModelDraft((value) => ({
                      ...value,
                      api_key: e.target.value,
                    }));
                    setNewTested("");
                  }}
                />
              </label>
              <fieldset className="model-form-field model-form-wide">
                <legend>模型能力</legend>
                {(["text", "vlm"] as ModelKind[]).map((kind) => (
                  <label key={kind}>
                    <input
                      type="checkbox"
                      checked={modelDraft.kinds.includes(kind)}
                      onChange={(e) =>
                        setModelDraft((value) => ({
                          ...value,
                          kinds: e.target.checked
                            ? [...value.kinds, kind]
                            : value.kinds.filter((item) => item !== kind),
                        }))
                      }
                    />
                    {kind === "text" ? "Text" : "VLM"}
                  </label>
                ))}
              </fieldset>
            </div>
            <div className="model-modal-actions">
              <button
                onClick={async () => {
                  try {
                    if (editingModel)
                      await api.post(
                        `/models/${encodeURIComponent(editingModel.id)}/test`,
                        modelDraft,
                      );
                    else
                      await api.post("/models/test", {
                        ...modelDraft,
                        provider: "custom",
                      });
                    setNewTested(newSignature);
                    toast("连接测试通过");
                  } catch (e) {
                    toast((e as Error).message, true);
                  }
                }}
              >
                测试连接
              </button>
              <button
                className="btn primary small"
                disabled={
                  newTested !== newSignature || !modelDraft.kinds.length
                }
                onClick={async () => {
                  try {
                    if (editingModel)
                      await api.put(
                        `/models/${encodeURIComponent(editingModel.id)}`,
                        modelDraft,
                      );
                    else
                      await api.post("/models", {
                        ...modelDraft,
                        provider: "custom",
                      });
                    await refreshCatalog();
                    setNewModel(false);
                    setEditingModel(null);
                    toast(
                      editingModel
                        ? `${modelDraft.label} 已更新`
                        : `${modelDraft.label} 已加入模型库`,
                    );
                  } catch (e) {
                    toast((e as Error).message, true);
                  }
                }}
              >
                {editingModel ? "保存模型修改" : "添加到模型库"}
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}

export default function MonitorPage() {
  const { toast, registerNavigationGuard } = useNav();
  const initial = nowQuery();
  const [activeSection, setActiveSection] =
    useState<MonitorSection>(querySection());
  const [urlNotice, setUrlNotice] = useState("");
  const [jobSearch, setJobSearch] = useState(initial.get("job_search") || "");
  const [jobStatus, setJobStatus] = useState(initial.get("job_status") || "");
  const [jobProject, setJobProject] = useState(
    initial.get("job_project") || "",
  );
  const [jobWorkflow, setJobWorkflow] = useState(
    initial.get("job_workflow") || "",
  );
  const [jobFrom, setJobFrom] = useState(initial.get("job_from") || "");
  const [jobTo, setJobTo] = useState(initial.get("job_to") || "");
  const [jobSort, setJobSort] = useState(initial.get("job_sort") || "desc");
  const [jobPage, setJobPage] = useState(
    Math.max(1, Number(initial.get("job_page")) || 1),
  );
  const [jobPageSize, setJobPageSize] = useState(
    Math.max(1, Number(initial.get("job_page_size")) || 20),
  );
  const [selectedJob, setSelectedJob] = useState<Job | null>(null);
  const [selectedJobId, setSelectedJobId] = useState(
    initial.get("job_id") || "",
  );
  const [callSearch, setCallSearch] = useState(
    initial.get("call_search") || "",
  );
  const [callStatus, setCallStatus] = useState(
    initial.get("call_status") || "",
  );
  const [callCategory, setCallCategory] = useState(
    initial.get("call_category") || "business",
  );
  const [callModel, setCallModel] = useState(initial.get("call_model") || "");
  const [callFrom, setCallFrom] = useState(initial.get("call_from") || "");
  const [callTo, setCallTo] = useState(initial.get("call_to") || "");
  const [callProject, setCallProject] = useState(
    initial.get("call_project") || "",
  );
  const [callFunction, setCallFunction] = useState(
    initial.get("call_function") || "",
  );
  const [callSort, setCallSort] = useState(initial.get("call_sort") || "desc");
  const [callIds, setCallIds] = useState(initial.get("call_ids") || "");
  const [callPage, setCallPage] = useState(
    Math.max(1, Number(initial.get("call_page")) || 1),
  );
  const [callPageSize, setCallPageSize] = useState(
    Math.max(1, Number(initial.get("call_page_size")) || 20),
  );
  const [selectedCall, setSelectedCall] = useState<Call | null>(null);
  const [selectedCallId, setSelectedCallId] = useState(
    Number(initial.get("call_id") || 0),
  );
  const [objectLoadError, setObjectLoadError] = useState("");
  const [selectedRunId, setSelectedRunId] = useState<string | null>(
    initial.get("run_id"),
  );
  const [focusToken, setFocusToken] = useState(initial.get("focus") || "");
  const jobsSummaryPoll = usePoll<JobsSummary>(
    () => api.get("/system/jobs"),
    4000,
    [],
  );
  const settingsPoll = usePoll<SettingsView>(
    () => api.get("/settings?include_schema=true"),
    0,
    [],
  );
  const features = settingsPoll.data?.features || {
    overview_state_v2: true,
    jobs_query_v2: true,
    run_center_v2: true,
    call_detail_v2: true,
    settings_edit_v2: true,
  };
  const healthPoll = usePoll<Health>(() => api.get("/system/health"), 0, []);
  const catalogPoll = usePoll<ModelCatalog>(() => api.get("/models"), 0, []);
  const jobQuery = encodeQuery({
    page: jobPage,
    page_size: jobPageSize,
    search: jobSearch,
    status: jobStatus,
    project_id: jobProject,
    workflow: jobWorkflow,
    from_ts: jobFrom ? new Date(jobFrom).getTime() / 1000 : undefined,
    to_ts: jobTo ? new Date(jobTo).getTime() / 1000 : undefined,
    sort: jobSort,
  });
  const jobsPagePoll = usePoll<JobsPage>(
    () => api.get(`/system/jobs/query?${jobQuery}`),
    activeSection === "jobs" ? 4000 : 0,
    [activeSection, jobQuery],
  );
  const callQuery = encodeQuery({
    page: callPage,
    page_size: callPageSize,
    search: callSearch,
    status: callStatus,
    category: callCategory,
    model: callModel,
    project_id: callProject,
    function: callFunction,
    from_ts: callFrom ? new Date(callFrom).getTime() / 1000 : undefined,
    to_ts: callTo ? new Date(callTo).getTime() / 1000 : undefined,
    sort: callSort,
    ids: callIds,
  });
  const callsPagePoll = usePoll<CallsPage>(
    () => api.get(`/system/calls/query?${callQuery}`),
    activeSection === "calls" || activeSection === "overview" ? 6000 : 0,
    [activeSection, callQuery],
  );
  useEffect(() => {
    if (!jobsPagePoll.data || jobPage <= jobsPagePoll.data.page_count) return;
    setJobPage(jobsPagePoll.data.page_count);
    writeQuery({ job_page: String(jobsPagePoll.data.page_count) }, false);
    toast(`任务数据已变化，已回到最后合法页 ${jobsPagePoll.data.page_count}`);
  }, [jobPage, jobsPagePoll.data, toast]);
  useEffect(() => {
    if (!callsPagePoll.data || callPage <= callsPagePoll.data.page_count)
      return;
    setCallPage(callsPagePoll.data.page_count);
    writeQuery({ call_page: String(callsPagePoll.data.page_count) }, false);
    toast(`调用数据已变化，已回到最后合法页 ${callsPagePoll.data.page_count}`);
  }, [callPage, callsPagePoll.data, toast]);
  useEffect(() => {
    const onPop = () => {
      const p = nowQuery();
      const raw = p.get("section");
      if (raw && !VALID_SECTIONS.has(raw as MonitorSection)) {
        setUrlNotice(`已忽略非法区域参数：${raw}`);
        setActiveSection("overview");
      } else {
        setUrlNotice("");
        setActiveSection(querySection());
      }
      setSelectedRunId(p.get("run_id"));
      setFocusToken(p.get("focus") || "");
      setJobSearch(p.get("job_search") || "");
      setJobStatus(p.get("job_status") || "");
      setJobProject(p.get("job_project") || "");
      setJobWorkflow(p.get("job_workflow") || "");
      setJobFrom(p.get("job_from") || "");
      setJobTo(p.get("job_to") || "");
      setJobSort(p.get("job_sort") || "desc");
      setJobPage(Math.max(1, Number(p.get("job_page")) || 1));
      setJobPageSize(Math.max(1, Number(p.get("job_page_size")) || 20));
      setSelectedJobId(p.get("job_id") || "");
      setCallSearch(p.get("call_search") || "");
      setCallStatus(p.get("call_status") || "");
      setCallCategory(p.get("call_category") || "business");
      setCallModel(p.get("call_model") || "");
      setCallFrom(p.get("call_from") || "");
      setCallTo(p.get("call_to") || "");
      setCallProject(p.get("call_project") || "");
      setCallFunction(p.get("call_function") || "");
      setCallSort(p.get("call_sort") || "desc");
      setCallIds(p.get("call_ids") || "");
      setCallPage(Math.max(1, Number(p.get("call_page")) || 1));
      setCallPageSize(Math.max(1, Number(p.get("call_page_size")) || 20));
      setSelectedCallId(Number(p.get("call_id") || 0));
      if (!p.get("job_id")) setSelectedJob(null);
      if (!p.get("call_id")) setSelectedCall(null);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const openSection = (
    section: MonitorSection,
    patch: Record<string, string | null> = {},
  ) => {
    setObjectLoadError("");
    const cleanup: Record<string, string | null> = {};
    if (section !== "jobs") {
      cleanup.job_id = null;
      setSelectedJob(null);
      setSelectedJobId("");
    }
    if (section !== "calls") {
      cleanup.call_id = null;
      setSelectedCall(null);
      setSelectedCallId(0);
    }
    if (section !== "runs") cleanup.focus = null;
    setActiveSection(section);
    writeQuery({
      section: section === "overview" ? null : section,
      ...cleanup,
      ...patch,
    });
    track("drilldown", {
      source: nowQuery().get("section") || "overview",
      target_type: section,
      filter_count: Object.values(patch).filter(Boolean).length,
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  };
  const openRun = (runId: string) => {
    const focus = String(Date.now());
    setSelectedRunId(runId);
    setFocusToken(focus);
    track("deep_link", { source: activeSection, target_type: "run" }, runId);
    openSection("runs", { run_id: runId, focus });
  };
  const jobsStatus = blockStatus(
    jobsSummaryPoll.loading,
    jobsSummaryPoll.error,
    jobsSummaryPoll.data,
    !!jobsSummaryPoll.data && jobsSummaryPoll.data.total === 0,
  );
  const callsStatus = blockStatus(
    callsPagePoll.loading,
    callsPagePoll.error,
    callsPagePoll.data,
    !!callsPagePoll.data && callsPagePoll.data.total === 0,
  );
  const settingsStatus = blockStatus(
    settingsPoll.loading,
    settingsPoll.error,
    settingsPoll.data,
    !settingsPoll.data,
  );
  const healthStatus = blockStatus(
    healthPoll.loading,
    healthPoll.error,
    healthPoll.data,
    !healthPoll.data,
  );
  const catalogStatus = blockStatus(
    catalogPoll.loading,
    catalogPoll.error,
    catalogPoll.data,
    !!catalogPoll.data && catalogPoll.data.items.length === 0,
  );
  useBlockTelemetry("jobs", jobsStatus);
  useBlockTelemetry("calls", callsStatus);
  useBlockTelemetry("settings", settingsStatus);
  useBlockTelemetry("health", healthStatus);
  useBlockTelemetry("model_catalog", catalogStatus);
  useEffect(() => {
    if (!jobsPagePoll.data) return;
    track("query_result", {
      query_type: "jobs",
      total: jobsPagePoll.data.total,
      page_size: jobsPagePoll.data.page_size,
      query_ms: jobsPagePoll.data.query_ms || 0,
    });
  }, [jobsPagePoll.data?.server_time]);
  useEffect(() => {
    if (!callsPagePoll.data) return;
    track("query_result", {
      query_type: "calls",
      total: callsPagePoll.data.total,
      page_size: callsPagePoll.data.page_size,
      query_ms: callsPagePoll.data.query_ms || 0,
    });
  }, [callsPagePoll.data?.server_time]);
  const counts = jobsSummaryPoll.data?.counts || {};
  const systemExceptions = callsPagePoll.data?.failed_total || 0;
  useEffect(() => {
    if (activeSection !== "jobs" || !selectedJobId) return;
    setObjectLoadError("");
    void api
      .get(`/system/jobs/${encodeURIComponent(selectedJobId)}?source=auto`)
      .then((item) => {
        setSelectedJob(item as Job);
        track(
          "deep_link",
          { source: "url", target_type: "job" },
          selectedJobId,
        );
      })
      .catch((error) => {
        setSelectedJob(null);
        setObjectLoadError(
          `目标任务无法定位：${(error as Error).message}。不会改选其他任务。`,
        );
        track(
          "deep_link",
          { source: "url", target_type: "job", result: "failed" },
          selectedJobId,
        );
      });
  }, [activeSection, selectedJobId]);
  useEffect(() => {
    if (activeSection !== "calls" || !selectedCallId) return;
    if (selectedCall?.id === selectedCallId) return;
    setObjectLoadError("");
    void api
      .get(`/system/calls/${selectedCallId}`)
      .then((item) => {
        setSelectedCall(item as Call);
        track(
          "deep_link",
          { source: "url", target_type: "call" },
          String(selectedCallId),
        );
      })
      .catch((error) => {
        setSelectedCall(null);
        setObjectLoadError(
          `目标调用无法定位：${(error as Error).message}。不会改选其他调用。`,
        );
        track(
          "deep_link",
          { source: "url", target_type: "call", result: "failed" },
          String(selectedCallId),
        );
      });
  }, [activeSection, selectedCall?.id, selectedCallId]);
  return (
    <div className="monitor-page">
      <header className="desk-head">
        <div className="crumb">漫剧案头 / 监制房</div>
        <h1>
          监制房 <span className="sub">异常与待办优先的制作工作台</span>
        </h1>
        <hr className="rule" />
      </header>
      {urlNotice && (
        <div className="monitor-state error" role="alert">
          {urlNotice}
        </div>
      )}
      {objectLoadError && (
        <div className="monitor-state error" role="alert">
          {objectLoadError}
          <button
            onClick={() => {
              setObjectLoadError("");
              setSelectedJobId("");
              setSelectedCallId(0);
              writeQuery({ job_id: null, call_id: null }, false);
            }}
          >
            返回当前列表
          </button>
        </div>
      )}
      <div className="monitor-block-strip" aria-label="数据块状态">
        {[
          ["任务", jobsStatus, jobsSummaryPoll.data?.server_time],
          ["调用", callsStatus, callsPagePoll.data?.server_time],
          ["设置", settingsStatus, settingsPoll.data?.server_time],
          ["健康", healthStatus, undefined],
          ["模型库", catalogStatus, undefined],
        ].map(([label, status, stamp]) => (
          <span className={`monitor-block-chip ${status}`} key={String(label)}>
            {label}：
            {status === "loading"
              ? "加载中"
              : status === "error"
                ? "失败"
                : status === "stale"
                  ? "已过期"
                  : status === "ready-empty"
                    ? "已确认空"
                    : "已同步"}
            {stamp && status !== "loading"
              ? ` · ${fmtTime(Number(stamp))}`
              : ""}
          </span>
        ))}
      </div>
      <nav className="monitor-subnav" aria-label="监制房子菜单">
        {SECTIONS.map((section) => {
          const badge =
            section.key === "jobs" && jobsSummaryPoll.data
              ? (counts.running || 0) +
                (counts.queued || 0) +
                (counts.waiting_human || 0)
              : section.key === "calls" && callsPagePoll.data
                ? systemExceptions
                : undefined;
          return (
            <button
              key={section.key}
              className={activeSection === section.key ? "active" : ""}
              aria-current={activeSection === section.key ? "page" : undefined}
              onClick={() => openSection(section.key)}
            >
              <span>
                {section.label}
                {badge != null && badge > 0 && <em>{badge}</em>}
              </span>
              <small>{section.description}</small>
            </button>
          );
        })}
      </nav>
      {activeSection === "overview" && !features.overview_state_v2 && (
        <section
          className="card monitor-section monitor-state stale"
          role="status"
        >
          新版总览已由独立发布开关停用；任务、运行与调用账本仍可从子菜单直接访问。
        </section>
      )}
      {activeSection === "overview" && features.overview_state_v2 && (
        <div className="monitor-section">
          <div className="monitor-section-head">
            <div>
              <span className="eyebrow">CONTROL OVERVIEW</span>
              <h2>制作运行总览</h2>
            </div>
            <p>正在运行、待我处理、系统异常与近期完成。</p>
          </div>
          <DataBoundary
            status={jobsStatus}
            error={jobsSummaryPoll.error}
            updatedAt={jobsSummaryPoll.data?.server_time}
            onRetry={() => void jobsSummaryPoll.refresh()}
            emptyLabel="当前确实没有制作任务"
          >
            <div className="stat-row monitor-stats">
              {[
                {
                  label: "正在运行",
                  count:
                    (counts.running || 0) +
                    (counts.queued || 0) +
                    (counts.recovering || 0),
                  status: "running,queued,recovering",
                },
                {
                  label: "待我处理",
                  count:
                    (counts.waiting_human || 0) +
                    (counts.paused_budget || 0) +
                    (counts.paused_external || 0),
                  status: "waiting_human,paused_budget,paused_external",
                },
                {
                  label: "系统异常",
                  count: (counts.failed || 0) + (counts.partial || 0),
                  status: "failed,partial",
                },
                {
                  label: "近期完成",
                  count: counts.succeeded || 0,
                  status: "succeeded",
                },
              ].map((item) => (
                <button
                  className="stat-cell"
                  key={item.label}
                  onClick={() => {
                    setJobStatus(item.status);
                    setJobSearch("");
                    setJobProject("");
                    setJobWorkflow("");
                    setJobFrom("");
                    setJobTo("");
                    setSelectedJobId("");
                    setJobPage(1);
                    openSection("jobs", {
                      source: "overview",
                      job_status: item.status,
                      job_search: null,
                      job_project: null,
                      job_workflow: null,
                      job_from: null,
                      job_to: null,
                      job_page: null,
                      job_id: null,
                    });
                  }}
                >
                  <div className="s-label">{item.label}</div>
                  <div className="cost-ink">{item.count}</div>
                  <span>按统一口径下钻 →</span>
                </button>
              ))}
            </div>
          </DataBoundary>
          <div className="monitor-overview-grid">
            <section className="card monitor-overview-card">
              <div className="monitor-card-head">
                <div>
                  <span className="eyebrow">EXCEPTIONS</span>
                  <h3>需要关注</h3>
                </div>
                <button
                  onClick={() => {
                    setCallCategory("business");
                    setCallSearch("");
                    setCallStatus("");
                    setCallModel("");
                    setCallFrom("");
                    setCallTo("");
                    setCallProject("");
                    setCallFunction("");
                    setCallSort("desc");
                    setCallIds("");
                    setCallPage(1);
                    openSection("calls", {
                      source: "overview",
                      call_category: "business",
                      call_search: null,
                      call_status: null,
                      call_model: null,
                      call_from: null,
                      call_to: null,
                      call_project: null,
                      call_function: null,
                      call_sort: null,
                      call_ids: null,
                      call_page: null,
                    });
                  }}
                >
                  查看全部
                </button>
              </div>
              <DataBoundary
                status={callsStatus}
                error={callsPagePoll.error}
                updatedAt={callsPagePoll.data?.server_time}
                onRetry={() => void callsPagePoll.refresh()}
                emptyLabel="当前确实没有业务调用记录"
              >
                {callsPagePoll.data?.aggregates.length ? (
                  <div className="monitor-brief-list">
                    {callsPagePoll.data.aggregates.slice(0, 5).map((group) => (
                      <button
                        key={group.key}
                        onClick={() => {
                          setCallSearch("");
                          setCallCategory("business");
                          setCallStatus("");
                          setCallModel("");
                          setCallFrom("");
                          setCallTo("");
                          setCallProject(group.project_id || "");
                          setCallFunction("");
                          setCallSort("desc");
                          setCallIds(group.call_ids.join(","));
                          setCallPage(1);
                          openSection("calls", {
                            source: "overview",
                            call_category: "business",
                            call_search: null,
                            call_status: null,
                            call_model: null,
                            call_from: null,
                            call_to: null,
                            call_project: group.project_id || null,
                            call_function: null,
                            call_sort: null,
                            call_ids: group.call_ids.join(","),
                            call_page: null,
                          });
                        }}
                      >
                        <span>
                          <b>
                            {CALL_KIND_LABELS[group.kind] || "其他业务异常"} ·{" "}
                            {group.project_name}
                          </b>
                          <small>
                            {group.episode_no
                              ? `第${group.episode_no}集`
                              : "上下文未关联"}{" "}
                            · 首次 {fmtTime(group.first_ts)} · 最近{" "}
                            {fmtTime(group.last_ts)}
                          </small>
                        </span>
                        <span className="stamp red">影响 {group.count}</span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="monitor-ok">
                    全量查询成功：当前没有需要关注的业务异常
                  </div>
                )}
              </DataBoundary>
            </section>
            <section className="card monitor-overview-card">
              <div className="monitor-card-head">
                <div>
                  <span className="eyebrow">RECENT JOBS</span>
                  <h3>最近任务</h3>
                </div>
                <button onClick={() => openSection("jobs")}>查看全部</button>
              </div>
              {jobsSummaryPoll.data?.recent.slice(0, 5).map((job) => (
                <button
                  className="monitor-recent-job"
                  key={job.id}
                  onClick={() => {
                    setJobStatus("");
                    setJobSearch("");
                    setJobProject("");
                    setJobWorkflow("");
                    setJobFrom("");
                    setJobTo("");
                    setJobPage(1);
                    openSection("jobs", {
                      source: "overview",
                      job_id: job.id,
                      job_status: null,
                      job_search: null,
                      job_project: null,
                      job_workflow: null,
                      job_from: null,
                      job_to: null,
                      job_page: null,
                    });
                    setSelectedJob(job);
                    setSelectedJobId(job.id);
                  }}
                >
                  <span>
                    <b>{job.project_name || "上下文未关联"}</b>
                    <small>{jobWorkLabel(job)}</small>
                  </span>
                  <span className={`stamp ${stampClass(job.status)}`}>
                    {jobStatusLabel(job.status)}
                  </span>
                </button>
              ))}
            </section>
            <section className="card monitor-overview-card monitor-system-card">
              <div className="monitor-card-head">
                <div>
                  <span className="eyebrow">SYSTEM</span>
                  <h3>配置概况</h3>
                </div>
                <button onClick={() => openSection("settings")}>
                  管理设置
                </button>
              </div>
              <DataBoundary
                status={settingsStatus}
                error={settingsPoll.error}
                updatedAt={settingsPoll.data?.server_time}
                onRetry={() => void settingsPoll.refresh()}
                emptyLabel="未获得配置"
              >
                <dl>
                  <div>
                    <dt>配置版本</dt>
                    <dd>v{settingsPoll.data?.version}</dd>
                  </div>
                  <div>
                    <dt>上游在途上限</dt>
                    <dd>{settingsPoll.data?.effective.video_inflight_limit}</dd>
                  </div>
                  <div>
                    <dt>视频提交并发</dt>
                    <dd>
                      {settingsPoll.data?.effective.video_submit_concurrency}
                    </dd>
                  </div>
                  <div>
                    <dt>单集预算</dt>
                    <dd>
                      ¥ {settingsPoll.data?.effective.episode_cost_limit_cny}
                    </dd>
                  </div>
                </dl>
              </DataBoundary>
            </section>
          </div>
        </div>
      )}
      {activeSection === "runs" && !features.run_center_v2 && (
        <section
          className="card monitor-section monitor-state stale"
          role="status"
        >
          新版运行中心已由独立发布开关停用，权威任务状态未被修改。
        </section>
      )}
      {activeSection === "runs" && features.run_center_v2 && (
        <div className="monitor-section">
          <RunCenter
            selectedRunId={selectedRunId}
            focusToken={focusToken}
            onSelect={(id) => {
              setSelectedRunId(id);
              writeQuery({ run_id: id }, false);
            }}
          />
        </div>
      )}
      {activeSection === "jobs" && !features.jobs_query_v2 && (
        <section
          className="card monitor-section monitor-state stale"
          role="status"
        >
          全量任务查询已由独立发布开关停用；页面不会把旧的有限数据伪装成全量结果。
        </section>
      )}
      {activeSection === "jobs" && features.jobs_query_v2 && (
        <section className="card monitor-section">
          <div className="monitor-section-head compact">
            <div>
              <span className="eyebrow">JOB QUEUE</span>
              <h2>任务队列</h2>
            </div>
            <p>
              {nowQuery().get("source") === "overview"
                ? "来自总览 · 已清除冲突筛选"
                : "服务端全量查询与真实总数"}
            </p>
          </div>
          <div className="monitor-toolbar">
            <label className="monitor-search">
              <span>搜索</span>
              <SearchField
                value={jobSearch}
                placeholder="搜索项目、集数、镜号或错误"
                ariaLabel="搜索任务"
                onChange={(value) => {
                  setJobSearch(value);
                  setJobPage(1);
                  writeQuery(
                    { job_search: value || null, job_page: null },
                    false,
                  );
                }}
              />
            </label>
            <label>
              <span>状态</span>
              <select
                value={jobStatus}
                onChange={(e) => {
                  setJobStatus(e.target.value);
                  setJobPage(1);
                  writeQuery(
                    { job_status: e.target.value || null, job_page: null },
                    false,
                  );
                }}
              >
                <option value="">全部状态</option>
                <option value="running,queued,recovering">
                  正在运行（合并）
                </option>
                <option value="waiting_human,paused_budget,paused_external">
                  待我处理（合并）
                </option>
                <option value="failed,partial">系统异常（合并）</option>
                {Object.entries(JOB_STATUS_LABELS).map(([key, label]) => (
                  <option value={key} key={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>项目 ID</span>
              <input
                value={jobProject}
                onChange={(e) => {
                  setJobProject(e.target.value);
                  setJobPage(1);
                  writeQuery({
                    job_project: e.target.value || null,
                    job_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>工作流</span>
              <select
                value={jobWorkflow}
                onChange={(e) => {
                  setJobWorkflow(e.target.value);
                  setJobPage(1);
                  writeQuery({
                    job_workflow: e.target.value || null,
                    job_page: null,
                  });
                }}
              >
                <option value="">全部</option>
                {Object.entries(WORKFLOW_LABELS).map(([key, label]) => (
                  <option value={key} key={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>开始时间</span>
              <input
                type="datetime-local"
                value={jobFrom}
                onChange={(e) => {
                  setJobFrom(e.target.value);
                  setJobPage(1);
                  writeQuery({
                    job_from: e.target.value || null,
                    job_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>结束时间</span>
              <input
                type="datetime-local"
                value={jobTo}
                onChange={(e) => {
                  setJobTo(e.target.value);
                  setJobPage(1);
                  writeQuery({
                    job_to: e.target.value || null,
                    job_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>排序</span>
              <select
                value={jobSort}
                onChange={(e) => {
                  setJobSort(e.target.value);
                  setJobPage(1);
                  writeQuery({ job_sort: e.target.value, job_page: null });
                }}
              >
                <option value="desc">最新优先</option>
                <option value="asc">最早优先</option>
              </select>
            </label>
            <button
              className="monitor-clear"
              onClick={() => {
                setJobSearch("");
                setJobStatus("");
                setJobProject("");
                setJobWorkflow("");
                setJobFrom("");
                setJobTo("");
                setJobSort("desc");
                setJobPage(1);
                writeQuery(
                  {
                    job_search: null,
                    job_status: null,
                    job_project: null,
                    job_workflow: null,
                    job_from: null,
                    job_to: null,
                    job_sort: null,
                    job_page: null,
                    source: null,
                  },
                  false,
                );
              }}
            >
              清除组合筛选
            </button>
          </div>
          <DataBoundary
            status={blockStatus(
              jobsPagePoll.loading,
              jobsPagePoll.error,
              jobsPagePoll.data,
              !!jobsPagePoll.data && jobsPagePoll.data.total === 0,
            )}
            error={jobsPagePoll.error}
            updatedAt={jobsPagePoll.data?.server_time}
            onRetry={() => void jobsPagePoll.refresh()}
            emptyLabel="当前筛选下没有任务，可清除筛选重试"
          >
            <div className="monitor-table-wrap">
              <table className="ledger monitor-ledger jobs-ledger">
                <thead>
                  <tr>
                    <th>更新时间</th>
                    <th>项目</th>
                    <th>工作项</th>
                    <th>状态</th>
                    <th>影响与下一步</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {jobsPagePoll.data?.items.map((job) => (
                    <tr key={`${job.source}-${job.id}`}>
                      <td className="mono">{fmtTime(job.updated_at)}</td>
                      <td>{job.project_name || "上下文未关联"}</td>
                      <td>{jobWorkLabel(job)}</td>
                      <td>
                        <span className={`stamp ${stampClass(job.status)}`}>
                          {jobStatusLabel(job.status)}
                        </span>
                      </td>
                      <td className="monitor-error-cell">
                        {job.error ||
                          (job.status === "succeeded"
                            ? "任务已完成，无需处理"
                            : "查看详情获取下一步")}
                      </td>
                      <td>
                        <button
                          className="btn small"
                          disabled={!features.call_detail_v2}
                          onClick={() => {
                            setSelectedJob(job);
                            setSelectedJobId(job.id);
                            setObjectLoadError("");
                            writeQuery({ job_id: job.id });
                          }}
                          aria-label={`查看任务 ${job.id} 详情`}
                        >
                          详情 / 处理
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataBoundary>
          {jobsPagePoll.data && (
            <Pagination
              page={jobPage}
              pageSize={jobPageSize}
              total={jobsPagePoll.data.total}
              pageCount={jobsPagePoll.data.page_count}
              onPage={(value) => {
                setJobPage(value);
                writeQuery({ job_page: String(value) }, false);
              }}
              onPageSize={(value) => {
                setJobPageSize(value);
                setJobPage(1);
                writeQuery({ job_page_size: String(value), job_page: null });
              }}
            />
          )}
        </section>
      )}
      {activeSection === "calls" && (
        <section className="card monitor-section">
          <div className="monitor-section-head compact">
            <div>
              <span className="eyebrow">PROVIDER CALLS</span>
              <h2>调用日志</h2>
            </div>
            <p>
              {nowQuery().get("source") === "overview"
                ? "来自总览 · 与异常聚合共享口径"
                : "摘要优先，正文按需加载"}
            </p>
          </div>
          <div className="monitor-toolbar">
            <label className="monitor-search">
              <span>搜索</span>
              <SearchField
                value={callSearch}
                placeholder="搜索功能、模型、HTTP 或错误"
                ariaLabel="搜索调用日志"
                onChange={(value) => {
                  setCallSearch(value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery(
                    {
                      call_search: value || null,
                      call_ids: null,
                      call_page: null,
                    },
                    false,
                  );
                }}
              />
            </label>
            <label>
              <span>类别</span>
              <select
                value={callCategory}
                onChange={(e) => {
                  setCallCategory(e.target.value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery({
                    call_category: e.target.value || null,
                    call_ids: null,
                    call_page: null,
                  });
                }}
              >
                {Object.entries(CALL_CATEGORY_LABELS).map(([key, label]) => (
                  <option value={key} key={key}>
                    {label}
                  </option>
                ))}
                <option value="">全部类别</option>
              </select>
            </label>
            <label>
              <span>状态</span>
              <select
                value={callStatus}
                onChange={(e) => {
                  setCallStatus(e.target.value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery({
                    call_status: e.target.value || null,
                    call_ids: null,
                    call_page: null,
                  });
                }}
              >
                <option value="">全部状态</option>
                {Object.entries(CALL_STATUS_LABELS).map(([key, label]) => (
                  <option value={key} key={key}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>模型 ID</span>
              <input
                value={callModel}
                onChange={(e) => {
                  setCallModel(e.target.value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery({
                    call_model: e.target.value || null,
                    call_ids: null,
                    call_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>项目 ID</span>
              <input
                value={callProject}
                onChange={(e) => {
                  setCallProject(e.target.value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery({
                    call_project: e.target.value || null,
                    call_ids: null,
                    call_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>功能</span>
              <input
                value={callFunction}
                onChange={(e) => {
                  setCallFunction(e.target.value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery({
                    call_function: e.target.value || null,
                    call_ids: null,
                    call_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>开始时间</span>
              <input
                type="datetime-local"
                value={callFrom}
                onChange={(e) => {
                  setCallFrom(e.target.value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery({
                    call_from: e.target.value || null,
                    call_ids: null,
                    call_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>结束时间</span>
              <input
                type="datetime-local"
                value={callTo}
                onChange={(e) => {
                  setCallTo(e.target.value);
                  setCallIds("");
                  setCallPage(1);
                  writeQuery({
                    call_to: e.target.value || null,
                    call_ids: null,
                    call_page: null,
                  });
                }}
              />
            </label>
            <label>
              <span>排序</span>
              <select
                value={callSort}
                onChange={(e) => {
                  setCallSort(e.target.value);
                  setCallPage(1);
                  writeQuery({ call_sort: e.target.value, call_page: null });
                }}
              >
                <option value="desc">最新优先</option>
                <option value="asc">最早优先</option>
              </select>
            </label>
            <button
              className="monitor-clear"
              onClick={() => {
                setCallSearch("");
                setCallStatus("");
                setCallCategory("business");
                setCallModel("");
                setCallFrom("");
                setCallTo("");
                setCallProject("");
                setCallFunction("");
                setCallSort("desc");
                setCallIds("");
                setCallPage(1);
                writeQuery(
                  {
                    call_search: null,
                    call_status: null,
                    call_category: "business",
                    call_model: null,
                    call_from: null,
                    call_to: null,
                    call_project: null,
                    call_function: null,
                    call_sort: null,
                    call_ids: null,
                    call_page: null,
                    source: null,
                  },
                  false,
                );
              }}
            >
              清除组合筛选
            </button>
          </div>
          <DataBoundary
            status={callsStatus}
            error={callsPagePoll.error}
            updatedAt={callsPagePoll.data?.server_time}
            onRetry={() => void callsPagePoll.refresh()}
            emptyLabel="当前筛选下没有调用记录"
          >
            <div className="monitor-table-wrap">
              <table className="ledger monitor-ledger calls-ledger">
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>类别 / 调用目的</th>
                    <th>模型</th>
                    <th>状态</th>
                    <th>HTTP</th>
                    <th>延迟</th>
                    <th>错误 / 下一步</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {callsPagePoll.data?.items.map((call) => (
                    <tr key={call.id}>
                      <td className="mono">{fmtTime(call.ts)}</td>
                      <td>
                        <small>{CALL_CATEGORY_LABELS[call.category]}</small>
                        <br />
                        {callPurpose(call)}
                      </td>
                      <td>
                        {call.model_label || call.model || "未记录模型"}
                        <details>
                          <summary>原始 ID</summary>
                          <code>{call.model}</code>
                        </details>
                      </td>
                      <td>
                        <span
                          className={`stamp ${stampClass(call.effective_status)}`}
                          title={`原始状态：${call.status}`}
                        >
                          {callStatusLabel(call.effective_status)}
                        </span>
                      </td>
                      <td>
                        {call.http_status
                          ? `HTTP ${call.http_status}`
                          : "未返回"}
                      </td>
                      <td>{(call.latency_ms / 1000).toFixed(1)}s</td>
                      <td className="monitor-error-cell">
                        {call.error ||
                          (call.run_id ? "可查看关联 Run" : "无需处理")}
                      </td>
                      <td>
                        <button
                          className="btn small"
                          onClick={() => {
                            setSelectedCall(call);
                            setSelectedCallId(call.id);
                            setObjectLoadError("");
                            writeQuery({ call_id: String(call.id) });
                          }}
                          aria-label={`查看调用 ${call.id} 的脱敏详情`}
                        >
                          {features.call_detail_v2 ? "查看详情" : "详情已停用"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </DataBoundary>
          {callsPagePoll.data && (
            <Pagination
              page={callPage}
              pageSize={callPageSize}
              total={callsPagePoll.data.total}
              pageCount={callsPagePoll.data.page_count}
              onPage={(value) => {
                setCallPage(value);
                writeQuery({ call_page: String(value) }, false);
              }}
              onPageSize={(value) => {
                setCallPageSize(value);
                setCallPage(1);
                writeQuery({ call_page_size: String(value), call_page: null });
              }}
            />
          )}
        </section>
      )}
      {activeSection === "models" && (
        <DataBoundary
          status={healthStatus === "ready-data" ? catalogStatus : healthStatus}
          error={healthPoll.error || catalogPoll.error}
          onRetry={() =>
            void Promise.all([healthPoll.refresh(), catalogPoll.refresh()])
          }
          emptyLabel="模型库为空"
        >
          <ModelCenter
            health={healthPoll.data}
            catalog={catalogPoll.data}
            settings={settingsPoll.data}
            refreshHealth={healthPoll.refresh}
            refreshCatalog={catalogPoll.refresh}
            refreshSettings={settingsPoll.refresh}
            toast={toast}
          />
        </DataBoundary>
      )}
      {activeSection === "settings" && (
        <SettingsPanel
          state={settingsPoll.data}
          loading={settingsPoll.loading}
          error={settingsPoll.error}
          refresh={settingsPoll.refresh}
          toast={toast}
          registerGuard={registerNavigationGuard}
          editable={features.settings_edit_v2}
        />
      )}
      {selectedCall && features.call_detail_v2 && (
        <CallDrawer
          call={selectedCall}
          onClose={() => {
            setSelectedCall(null);
            setSelectedCallId(0);
            writeQuery({ call_id: null }, false);
          }}
          onRun={openRun}
        />
      )}
      {selectedJob && (
        <JobDrawer
          job={selectedJob}
          onClose={() => {
            setSelectedJob(null);
            setSelectedJobId("");
            writeQuery({ job_id: null }, false);
          }}
          onChanged={() =>
            void Promise.all([
              jobsPagePoll.refresh(),
              jobsSummaryPoll.refresh(),
              api
                .get(
                  `/system/jobs/${encodeURIComponent(selectedJob.id)}?source=auto`,
                )
                .then((item) => setSelectedJob(item as Job)),
            ])
          }
          onRun={openRun}
        />
      )}
    </div>
  );
}
