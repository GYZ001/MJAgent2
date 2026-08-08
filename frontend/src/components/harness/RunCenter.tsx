import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ArtifactEvidence,
  RunEvent,
  RunSummary,
  StepRun,
} from "../../api";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import { statusLabel } from "../../lib/statusLabels";
import TraceDrawer from "../observability/TraceDrawer";

interface RunItem extends RunSummary {
  project_id?: string | null;
  project_name?: string | null;
  episode_id?: string | null;
  episode_no?: number | null;
  episode_title?: string | null;
  shot_id?: string | null;
  shot_no?: number | null;
}
interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  page_count: number;
  server_time: number;
  query_ms?: number;
  scope?: { type: "project"; project_id: string; project_name: string };
}
interface GateArtifact {
  id: string;
  type: string;
  scope_type: string;
  scope_id: string;
  version: number;
  trust_level: string;
  created_at: number;
  project_id?: string | null;
  project_name?: string | null;
  episode_no?: number | null;
  episode_title?: string | null;
  run_id?: string | null;
}

const STATUS_LABELS: Record<string, string> = {
  CREATED: "待启动",
  RUNNING: "运行中",
  WAITING_RETRY: "等待重试",
  WAITING_AUTHORIZATION: "等待授权",
  WAITING_HUMAN: "等待人工",
  PAUSED_BUDGET: "预算暂停",
  PAUSED_EXTERNAL: "外部中断",
  SUCCEEDED: "已完成",
  PARTIAL: "部分完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};
const WORKFLOW_LABELS: Record<string, string> = {
  character_bible: "人物谱生成",
  character_references: "人物定妆照",
  scene_bible: "场景设定",
  scene_references: "场景参考图",
  episode_mapping: "分集规划",
  screenplay: "剧本生成",
  storyboard: "分镜生成",
  scene_generation: "关键帧生成",
  video_generation: "视频生成",
  episode_video_completion: "全片视频补齐",
  delivery: "交付",
  delivery_package: "交付候选生成",
};
const STEP_LABELS: Record<string, string> = {
  generate: "生成内容",
  validate: "校验内容",
  evaluate: "质量评估",
  repair: "定向修复",
  screenplay: "生成剧本",
  storyboard: "生成分镜",
  build_delivery_snapshot: "生成交付快照",
  apply_delivery_gate: "应用交付决定",
  character_references: "生成人物参考图",
};
const GATE_LABELS: Record<string, string> = {
  character_bible: "人物谱定稿",
  episode_screenplay: "剧本定稿",
  storyboard: "分镜确认",
  delivery_package: "交付审核",
};

function businessName(raw?: string | null, map = WORKFLOW_LABELS) {
  if (!raw) return "未命名流程";
  return map[raw] || "其他业务步骤";
}
function runStampClass(status: string) {
  if (status === "SUCCEEDED") return "green";
  if (["FAILED", "PARTIAL", "PAUSED_BUDGET", "PAUSED_EXTERNAL"].includes(status))
    return "red";
  if (status === "CANCELLED") return "grey";
  return "gold";
}
export function runFailureGuidance(status?: string | null) {
  if (status === "PAUSED_BUDGET") return "预算不足，查看范围和已耗费用后再决定是否恢复。";
  if (status === "PAUSED_EXTERNAL") return "外部服务中断，可查看原因并从安全检查点恢复。";
  if (status === "WAITING_RETRY") return "系统正在等待自动重试，可查看失败原因。";
  if (status === "WAITING_HUMAN") return "需要人工确认后才能继续。";
  if (status === "PARTIAL") return "部分步骤未完成，可查看详情后受控重试。";
  return "运行未完成，可查看错误详情后重试或返回源页面修正。";
}
export function shouldFocusRunRow(
  focusToken: string | undefined,
  handledFocusToken: string,
  hasSelectedRow: boolean,
) {
  return Boolean(focusToken && focusToken !== handledFocusToken && hasSelectedRow);
}
function formatTime(value?: number | null) {
  return value ? new Date(value * 1000).toLocaleString() : "—";
}
function paramsFromLocation() {
  return new URLSearchParams(window.location.search);
}
function setUrlParams(
  patch: Record<string, string | null>,
  push = true,
) {
  const params = paramsFromLocation();
  Object.entries(patch).forEach(([key, value]) =>
    value ? params.set(key, value) : params.delete(key),
  );
  const next = `${window.location.pathname}?${params.toString()}`;
  window.history[push ? "pushState" : "replaceState"](
    {},
    "",
    next.endsWith("?") ? window.location.pathname : next,
  );
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
function sourcePath(item: GateArtifact) {
  if (!item.project_id) return "";
  const project = `/projects/${encodeURIComponent(item.project_id)}`;
  if (item.type === "character_bible") return `${project}/bible`;
  if (!item.episode_no || !item.scope_id) return project;
  const view =
    item.type === "episode_screenplay"
      ? "script"
      : item.type === "storyboard"
        ? "board"
        : "cinema";
  return `${project}/episodes/${encodeURIComponent(item.scope_id)}/${view}`;
}

function GateDrawer({
  gate,
  projectId,
  onClose,
  onDone,
}: {
  gate: GateArtifact;
  projectId?: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [evidence, setEvidence] = useState<ArtifactEvidence | null>(null);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState("");
  const [confirmDecision, setConfirmDecision] = useState<"approve" | "reject" | "">("");
  const drawerRef = useFocusTrap(true, onClose);
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = (await api.get(
        projectId
          ? `/projects/${encodeURIComponent(projectId)}/observability/artifacts/${encodeURIComponent(gate.id)}`
          : `/artifacts/${encodeURIComponent(gate.id)}`,
      )) as ArtifactEvidence;
      setEvidence(next);
      setStale(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [gate.id, projectId]);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (evidence && evidence.version !== gate.version) setStale(true);
  }, [gate.version, evidence]);
  const issues =
    evidence?.evaluations.flatMap((item) => item.issues || []) || [];
  const blockers = issues.filter((issue) => issue.severity === "blocker");
  const decide = async (decision: "approve" | "reject") => {
    if (!reason.trim() || busy) return;
    setBusy(decision);
    setError("");
    try {
      await api.post(projectId
        ? `/projects/${encodeURIComponent(projectId)}/observability/gates/${encodeURIComponent(gate.id)}/decision`
        : `/gates/${encodeURIComponent(gate.id)}/decision`, {
        decision,
        reason: reason.trim(),
        expected_version: evidence?.version ?? gate.version,
        idempotency_key: `${gate.id}:${decision}:${evidence?.version ?? gate.version}`,
      });
      track(
        "gate_action",
        { action: decision, object_status: evidence?.status || "unknown" },
        gate.id,
      );
      onDone();
      onClose();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
      setConfirmDecision("");
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
        className="monitor-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gate-title"
        ref={(node) => {
          drawerRef.current = node;
        }}
      >
        <header>
          <div>
            <span className="eyebrow">人工确认</span>
            <h3 id="gate-title">{GATE_LABELS[gate.type] || "人工确认"}</h3>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭人工确认面板">
            ×
          </button>
        </header>
        <p className="monitor-impact">
          处理结果会改变“{gate.project_name || "未关联项目"}
          {gate.episode_no ? ` · 第${gate.episode_no}集` : ""}”的后续制作状态。
        </p>
        {loading && (
          <div className="monitor-loading" role="status">
            正在加载证据与决策条件…
          </div>
        )}
        {stale && (
          <div className="monitor-state stale" role="status">
            证据已有新版本，当前内容可能过期。
            <button onClick={load}>刷新证据</button>
          </div>
        )}
        {error && (
          <div className="monitor-state error" role="alert">
            {error}
            <button onClick={load}>重试</button>
          </div>
        )}
        {evidence && (
          <>
            <div
              className={`monitor-gate-summary ${blockers.length ? "blocked" : "passed"}`}
            >
              <b>
                {blockers.length
                  ? `${blockers.length} 个阻塞问题需要决定`
                  : "自动检查已通过，等待人工确认"}
              </b>
              <span>
                证据第 {evidence.version} 版 · {statusLabel(evidence.trust_level)} · 最后同步{" "}
                {formatTime(gate.created_at)}
              </span>
            </div>
            <section>
              <h4>业务影响与建议</h4>
              {blockers.length ? (
                blockers.map((issue) => (
                  <div
                    className="monitor-issue"
                    key={`${issue.code}-${issue.subject}`}
                  >
                    <b>{issue.message}</b>
                    <span>
                      {issue.repair_hint || "建议打回源页面修正后重新提交。"}
                    </span>
                  </div>
                ))
              ) : (
                <p>未发现自动阻塞项；确认内容符合制作目标后可批准继续。</p>
              )}
            </section>
            <details>
              <summary>技术证据与评估记录</summary>
              <code>
                {evidence.id} · {evidence.content_hash}
              </code>
              {issues.map((issue) => (
                <pre key={`${issue.code}-${issue.subject}`}>
                  {issue.code}: {issue.message}
                </pre>
              ))}
            </details>
            <label className="monitor-decision-reason">
              <span>处理意见（必填）</span>
              <textarea
                value={reason}
                onChange={(e) => {
                  setReason(e.target.value);
                  setConfirmDecision("");
                }}
                placeholder="记录批准依据，或明确打回修改项"
              />
            </label>
            <div className="monitor-drawer-actions">
              {sourcePath(gate) && (
                <button
                  type="button"
                  onClick={() => {
                    window.location.href = sourcePath(gate);
                  }}
                >
                  去源页面编辑
                </button>
              )}
              <button
                type="button"
                className="danger"
                disabled={!reason.trim() || !!busy || stale}
                aria-label={!reason.trim()
                  ? "打回修改，暂不可用：请先填写处理意见"
                  : stale
                    ? "打回修改，暂不可用：证据已有新版本，请先刷新"
                    : busy
                      ? "打回修改，暂不可用：正在提交决定"
                      : "预览打回修改的影响"}
                onClick={() => setConfirmDecision("reject")}
              >
                {busy === "reject" ? "提交中…" : "打回修改"}
              </button>
              <button
                type="button"
                className="btn primary small"
                disabled={
                  !reason.trim() || !!busy || stale || blockers.length > 0
                }
                aria-label={!reason.trim()
                  ? "批准并继续，暂不可用：请先填写处理意见"
                  : stale
                    ? "批准并继续，暂不可用：证据已有新版本，请先刷新"
                    : blockers.length
                      ? `批准并继续，暂不可用：仍有 ${blockers.length} 个阻塞问题，请先打回处理`
                      : busy
                        ? "批准并继续，暂不可用：正在提交决定"
                        : "预览批准并继续的影响"}
                onClick={() => setConfirmDecision("approve")}
              >
                {busy === "approve" ? "提交中…" : "批准并继续"}
              </button>
            </div>
            {confirmDecision && (
              <div className="monitor-inline-confirm" role="group" aria-label="确认人工处理决定">
                <span>
                  {confirmDecision === "approve"
                    ? "批准后后续制作可继续，后续生成可能产生模型费用；当前证据、历史版本和审计记录会保留。"
                    : "打回后当前内容会保留并停止向下游推进；不会自动删除资产，也不会立即产生新的模型费用。"}
                </span>
                <button type="button" disabled={!!busy}
                  onClick={() => void decide(confirmDecision)}>
                  {busy ? "提交中…" : confirmDecision === "approve" ? "确认批准并继续" : "确认打回修改"}
                </button>
                <button type="button" disabled={!!busy}
                  onClick={() => setConfirmDecision("")}>返回检查</button>
              </div>
            )}
          </>
        )}
      </aside>
    </div>
  );
}

export default function RunCenter({
  selectedRunId,
  focusToken,
  onSelect,
  projectId,
}: {
  selectedRunId?: string | null;
  focusToken?: string;
  onSelect?: (id: string | null) => void;
  projectId?: string;
}) {
  const initial = paramsFromLocation();
  const [search, setSearch] = useState(initial.get("run_search") || "");
  const [status, setStatus] = useState(initial.get("run_status") || "");
  const [workflow, setWorkflow] = useState(initial.get("run_workflow") || "");
  const [project, setProject] = useState(projectId || initial.get("run_project") || "");
  const [episode, setEpisode] = useState(initial.get("run_episode") || "");
  const [fromTime, setFromTime] = useState(initial.get("run_from") || "");
  const [toTime, setToTime] = useState(initial.get("run_to") || "");
  const [sort, setSort] = useState(initial.get("run_sort") || "desc");
  const [includeHistory, setIncludeHistory] = useState(
    initial.get("run_history") === "1",
  );
  const [page, setPage] = useState(
    Math.max(1, Number(initial.get("run_page")) || 1),
  );
  const [pageSize, setPageSize] = useState(
    Math.max(1, Number(initial.get("run_page_size")) || 20),
  );
  const [runs, setRuns] = useState<Page<RunItem> | null>(null);
  const [listError, setListError] = useState("");
  const [pageNotice, setPageNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [selected, setSelected] = useState<string | null>(
    selectedRunId || initial.get("run_id"),
  );
  const [detail, setDetail] = useState<RunItem | null>(null);
  const [detailError, setDetailError] = useState("");
  const [steps, setSteps] = useState<StepRun[]>([]);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [gates, setGates] = useState<GateArtifact[] | null>(null);
  const [gateError, setGateError] = useState("");
  const [openGate, setOpenGate] = useState<GateArtifact | null>(null);
  const [traceRun, setTraceRun] = useState<RunItem | null>(null);
  const [actionBusy, setActionBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [confirmAction, setConfirmAction] = useState<
    "" | "cancel" | "resume" | "retry"
  >("");
  const requestSeq = useRef(0);
  const selectedRowRef = useRef<HTMLButtonElement | null>(null);
  const handledFocusTokenRef = useRef("");

  const queryPath = useMemo(() => {
    const p = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search.trim()) p.set("search", search.trim());
    if (status) p.set("status", status);
    if (workflow) p.set("workflow", workflow);
    if (!projectId && project.trim()) p.set("project_id", project.trim());
    if (episode.trim()) p.set("episode_no", episode.trim());
    if (fromTime) p.set("from_ts", String(new Date(fromTime).getTime() / 1000));
    if (toTime) p.set("to_ts", String(new Date(toTime).getTime() / 1000));
    p.set("sort", sort);
    if (includeHistory) p.set("include_history", "true");
    return projectId
      ? `/projects/${encodeURIComponent(projectId)}/observability/runs?${p}`
      : `/runs/query?${p}`;
  }, [
    episode,
    fromTime,
    includeHistory,
    page,
    pageSize,
    projectId ? "" : project,
    projectId,
    search,
    sort,
    status,
    toTime,
    workflow,
  ]);
  const refreshRuns = useCallback(
    async (background = false) => {
      if (!background && !runs) setLoading(true);
      try {
        const next = (await api.get(queryPath)) as Page<RunItem>;
        if (projectId && next.scope?.project_id !== projectId) {
          throw new Error("运行列表的项目范围与当前路由不一致，已拒绝渲染");
        }
        setRuns(next);
        setListError("");
        setLoading(false);
        if (page > next.page_count) {
          setPage(next.page_count);
          setUrlParams({ run_page: String(next.page_count) });
          setPageNotice(`运行数据已变化，已回到最后合法页 ${next.page_count}`);
        }
      } catch (e) {
        setListError((e as Error).message);
        setLoading(false);
      }
    },
    [page, queryPath, runs],
  );
  const refreshGates = useCallback(async () => {
    try {
      setGates((await api.get(projectId
        ? `/projects/${encodeURIComponent(projectId)}/observability/gates?limit=100`
        : "/gates?limit=100")) as GateArtifact[]);
      setGateError("");
    } catch (e) {
      setGateError((e as Error).message);
    }
  }, [projectId]);
  useEffect(() => {
    void refreshRuns();
  }, [queryPath]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!runs) return;
    track("query_result", {
      query_type: "runs",
      total: runs.total,
      page_size: runs.page_size,
      query_ms: runs.query_ms || 0,
    });
  }, [runs?.server_time]);
  useEffect(() => {
    void refreshGates();
  }, [refreshGates]);
  useEffect(() => {
    if (!openGate || !gates) return;
    const latest = gates.find((item) => item.id === openGate.id);
    if (latest && latest.version !== openGate.version) setOpenGate(latest);
  }, [gates, openGate]);
  useEffect(() => {
    if (!selectedRunId) return;
    setSelected(selectedRunId);
  }, [selectedRunId, focusToken]);
  useEffect(() => {
    const restoreFromUrl = () => {
      const params = paramsFromLocation();
      setSearch(params.get("run_search") || "");
      setStatus(params.get("run_status") || "");
      setWorkflow(params.get("run_workflow") || "");
      setProject(projectId || params.get("run_project") || "");
      setEpisode(params.get("run_episode") || "");
      setFromTime(params.get("run_from") || "");
      setToTime(params.get("run_to") || "");
      setSort(params.get("run_sort") || "desc");
      setIncludeHistory(params.get("run_history") === "1");
      setPage(Math.max(1, Number(params.get("run_page")) || 1));
      setPageSize(Math.max(1, Number(params.get("run_page_size")) || 20));
      setSelected(params.get("run_id"));
    };
    window.addEventListener("popstate", restoreFromUrl);
    return () => window.removeEventListener("popstate", restoreFromUrl);
  }, [projectId]);
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setSteps([]);
      setEvents([]);
      return;
    }
    const sequence = ++requestSeq.current;
    setDetailError("");
    const runBase = projectId
      ? `/projects/${encodeURIComponent(projectId)}/observability/runs`
      : "/runs";
    Promise.all([
      api.get(`${runBase}/${encodeURIComponent(selected)}`),
      api.get(`${runBase}/${encodeURIComponent(selected)}/steps`),
      api.get(`${runBase}/${encodeURIComponent(selected)}/events?limit=100`),
    ])
      .then(
        ([nextDetail, nextSteps, nextEvents]: [
          RunItem,
          StepRun[],
          RunEvent[],
        ]) => {
          if (requestSeq.current !== sequence) return;
          setDetail(nextDetail);
          setSteps(nextSteps);
          setEvents(nextEvents);
        },
      )
      .catch((e) => {
        if (requestSeq.current === sequence) {
          setDetail(null);
          setSteps([]);
          setEvents([]);
          setDetailError((e as Error).message);
        }
      });
  }, [projectId, selected]);
  useEffect(() => {
    if (!focusToken) {
      handledFocusTokenRef.current = "";
      return;
    }
    const selectedRow = selectedRowRef.current;
    if (!selectedRow || !shouldFocusRunRow(
      focusToken, handledFocusTokenRef.current, true,
    )) return;
    handledFocusTokenRef.current = focusToken;
    selectedRow.scrollIntoView({
      block: "center",
      behavior: "smooth",
    });
    selectedRow.focus();
    selectedRow.classList.remove("deep-linked");
    requestAnimationFrame(() =>
      selectedRowRef.current?.classList.add("deep-linked"),
    );
  }, [focusToken, selected, runs]);

  const choose = (id: string) => {
    setConfirmAction("");
    setSelected(id);
    setUrlParams({ run_id: id, focus: String(Date.now()) });
    onSelect?.(id);
  };
  const updateFilter = (patch: Record<string, string | null>) => {
    setUrlParams(patch, false);
    setPage(1);
  };
  const runAction = async (action: "cancel" | "resume" | "retry") => {
    if (!selected || actionBusy) return;
    setActionBusy(action);
    setActionError("");
    setActionMessage("");
    try {
      const result = await api.post(
        projectId
          ? `/projects/${encodeURIComponent(projectId)}/observability/runs/${encodeURIComponent(selected)}/${action}`
          : `/runs/${encodeURIComponent(selected)}/${action}`,
      );
      track(
        "job_action",
        { action, object_status: current?.status || "unknown" },
        selected,
      );
      const nextId = result.run?.id || result.run_id;
      if (nextId && nextId !== selected) choose(nextId);
      await refreshRuns(true);
      if (!nextId || nextId === selected) {
        const nextDetail = (await api.get(
          projectId
            ? `/projects/${encodeURIComponent(projectId)}/observability/runs/${encodeURIComponent(selected)}`
            : `/runs/${encodeURIComponent(selected)}`,
        )) as RunItem;
        setDetail(nextDetail);
      }
      setActionMessage(
        `${businessName(current?.workflow_type)} ${action === "cancel" ? "取消" : action === "resume" ? "恢复" : "重试"}请求已由系统接受`,
      );
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setActionBusy("");
      setConfirmAction("");
    }
  };
  const current =
    detail || runs?.items.find((run) => run.id === selected) || null;
  const workflows = Object.keys(WORKFLOW_LABELS);
  const filterCount = [
    search,
    status,
    workflow,
    projectId ? "" : project,
    episode,
    fromTime,
    toTime,
    sort !== "desc" ? sort : "",
    includeHistory ? "history" : "",
  ].filter(Boolean).length;
  const timeInvalid = Boolean(
    fromTime &&
      toTime &&
      new Date(fromTime).getTime() > new Date(toTime).getTime(),
  );
  const refreshAll = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await Promise.all([refreshRuns(true), refreshGates()]);
    } finally {
      setRefreshing(false);
    }
  };
  return (
    <section className="card run-center" aria-busy={loading}>
      <div className="monitor-section-head compact run-center-head">
        <div>
          <span className="eyebrow">可处理任务</span>
          <h2>运行中心</h2>
        </div>
        <div className="monitor-section-actions">
          <p>{runs ? `共 ${runs.total} 次运行` : "正在读取运行记录"}</p>
          <button
            type="button"
            className="monitor-refresh"
            disabled={refreshing}
            onClick={() => void refreshAll()}
          >
            <span aria-hidden="true">↻</span>
            {refreshing ? "刷新中…" : "刷新"}
          </button>
        </div>
      </div>
      <div className="monitor-toolbar">
        <label className="monitor-search">
          <span>搜索</span>
          <input
            value={search}
            aria-label="搜索运行"
            placeholder="运行编号、项目、集数或错误"
            onChange={(e) => {
              setSearch(e.target.value);
              updateFilter({
                run_search: e.target.value || null,
                run_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>状态</span>
          <select
            aria-label="按运行状态筛选"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              updateFilter({
                run_status: e.target.value || null,
                run_page: null,
              });
            }}
          >
            <option value="">默认：待处理与异常</option>
            {Object.entries(STATUS_LABELS).map(([key, label]) => (
              <option value={key} key={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>工作流</span>
          <select
            aria-label="按工作流筛选运行"
            value={workflow}
            onChange={(e) => {
              setWorkflow(e.target.value);
              updateFilter({
                run_workflow: e.target.value || null,
                run_page: null,
              });
            }}
          >
            <option value="">全部工作流</option>
            {workflows.map((key) => (
              <option value={key} key={key}>
                {businessName(key)}
              </option>
            ))}
          </select>
        </label>
        {projectId ? (
          <div className="monitor-scope-lock" role="status">
            <span>数据范围</span>
            <b>当前项目（不可跨项目切换）</b>
          </div>
        ) : (
          <label>
            <span>指定项目（高级筛选）</span>
            <input
              aria-label="按项目技术标识精确筛选运行"
              value={project}
              placeholder="输入项目技术标识（可选）"
              onChange={(e) => {
                setProject(e.target.value);
                updateFilter({
                  run_project: e.target.value || null,
                  run_page: null,
                });
              }}
            />
          </label>
        )}
        <label>
          <span>集数</span>
          <input
            type="number"
            aria-label="按集数筛选运行"
            min="1"
            value={episode}
            placeholder="如 1"
            onChange={(e) => {
              setEpisode(e.target.value);
              updateFilter({
                run_episode: e.target.value || null,
                run_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>开始时间</span>
          <input
            type="datetime-local"
            aria-label="运行开始时间下限"
            value={fromTime}
            max={toTime || undefined}
            aria-invalid={timeInvalid}
            onChange={(e) => {
              setFromTime(e.target.value);
              updateFilter({
                run_from: e.target.value || null,
                run_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>结束时间</span>
          <input
            type="datetime-local"
            aria-label="运行结束时间上限"
            value={toTime}
            min={fromTime || undefined}
            aria-invalid={timeInvalid}
            onChange={(e) => {
              setToTime(e.target.value);
              updateFilter({ run_to: e.target.value || null, run_page: null });
            }}
          />
        </label>
        <label>
          <span>排序</span>
          <select
            aria-label="运行排序方式"
            value={sort}
            onChange={(e) => {
              setSort(e.target.value);
              updateFilter({ run_sort: e.target.value, run_page: null });
            }}
          >
            <option value="desc">最新优先</option>
            <option value="asc">最早优先</option>
          </select>
        </label>
        <label className="monitor-check">
          <input
            type="checkbox"
            checked={includeHistory}
            onChange={(e) => {
              setIncludeHistory(e.target.checked);
              updateFilter({
                run_history: e.target.checked ? "1" : null,
                run_page: null,
              });
            }}
          />
          包含已完成历史
        </label>
        <button
          type="button"
          className="monitor-clear"
          disabled={filterCount === 0}
          aria-label={
            filterCount
              ? `清除 ${filterCount} 项运行筛选`
              : "当前没有运行筛选可清除"
          }
          onClick={() => {
            setSearch("");
            setStatus("");
            setWorkflow("");
            setProject("");
            setEpisode("");
            setFromTime("");
            setToTime("");
            setSort("desc");
            setIncludeHistory(false);
            setPage(1);
            setUrlParams({
              run_search: null,
              run_status: null,
              run_workflow: null,
              run_project: null,
              run_episode: null,
              run_from: null,
              run_to: null,
              run_sort: null,
              run_history: null,
              run_page: null,
            });
          }}
        >
          {filterCount ? `清除筛选（${filterCount}）` : "清除筛选"}
        </button>
      </div>
      {timeInvalid && (
        <p className="monitor-filter-error" role="alert">
          开始时间不能晚于结束时间，请调整时间范围。
        </p>
      )}
      <div className="gate-queue">
        <div className="gate-queue-head">
          <b>待人工确认</b>
          <span>{gates ? `${gates.length} 项待处理` : "加载中"}</span>
        </div>
        {gateError && (
          <div className="monitor-state error" role="alert">
            人工确认项加载失败：{gateError}
            <button onClick={refreshGates}>重试</button>
          </div>
        )}
        {gates && !gates.length && (
          <span className="gate-empty">
            查询成功：当前没有待人工决定的核心产物
          </span>
        )}
        {gates?.map((item) => {
          const scope = item.episode_no
            ? `第${item.episode_no}集 ${item.episode_title || ""}`.trim()
            : item.project_name
              ? "项目级内容"
              : "未关联具体范围";
          return (
            <button
              type="button"
              className="gate-item gate-item-button"
              key={`${item.id}-${item.version}`}
              onClick={() => setOpenGate(item)}
              aria-label={`处理${GATE_LABELS[item.type] || "其他人工确认"}：${item.project_name || "未关联项目"}，${scope}，提交于 ${formatTime(item.created_at)}`}
            >
              <div>
                <b>{GATE_LABELS[item.type] || "其他人工确认"}</b>
                <span>
                  {item.project_name || "未关联项目"} · {scope} · 提交于{" "}
                  {formatTime(item.created_at)}
                </span>
              </div>
              <span>查看证据并决策 →</span>
            </button>
          );
        })}
      </div>
      {listError && (
        <div
          className={`monitor-state ${runs ? "stale" : "error"}`}
          role="alert"
        >
          {runs
            ? `运行列表可能过期：${listError}`
            : `运行列表加载失败：${listError}`}
          <button onClick={() => void refreshRuns()}>重试</button>
        </div>
      )}
      {pageNotice && (
        <div className="monitor-state ready" role="status">
          {pageNotice}
          <button onClick={() => setPageNotice("")}>知道了</button>
        </div>
      )}
      {loading && !runs && (
        <div className="monitor-loading" role="status">
          正在加载运行列表，不以空数据代替…
        </div>
      )}
      {runs && runs.total === 0 && (
        <div className="empty" style={{ padding: 26 }}>
          查询成功：当前筛选下没有运行记录
        </div>
      )}
      {runs && runs.total > 0 && (
        <>
          <div className="monitor-table-wrap">
            <table className="ledger monitor-ledger runs-ledger">
              <thead>
                <tr>
                  <th>更新时间</th>
                  <th>项目</th>
                  <th>运行名称</th>
                  <th>状态</th>
                  <th>当前步骤 / 影响</th>
                  <th>费用</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {runs.items.map((run) => (
                  <tr key={run.id} className={run.id === selected ? "selected" : ""}>
                    <td className="mono">{formatTime(run.updated_at)}</td>
                    <td>{run.project_name || "上下文未关联"}</td>
                    <td>
                      <button
                        ref={run.id === selected ? selectedRowRef : undefined}
                        type="button"
                        className={`run-name-button${run.id === traceRun?.id ? " active" : ""}`}
                        onClick={() => {
                          if (projectId) setTraceRun(run);
                          else choose(run.id);
                        }}
                        aria-haspopup={projectId ? "dialog" : undefined}
                      >
                        {businessName(run.workflow_type)}
                        {run.shot_no != null ? ` · 镜${run.shot_no}` : ""}
                      </button>
                      <small className="monitor-cell-sub">
                        {run.episode_no ? `第${run.episode_no}集` : "项目级运行"}
                      </small>
                    </td>
                    <td>
                      <span className={`stamp ${runStampClass(run.status)}`}>
                        {STATUS_LABELS[run.status] || "状态待确认"}
                      </span>
                    </td>
                    <td className="monitor-error-cell">
                      <span>
                        {businessName(run.current_step_key, STEP_LABELS)}
                      </span>
                      {run.failure_message && (
                        <>
                          <small className="monitor-cell-sub">
                            {runFailureGuidance(run.status)}
                          </small>
                          <details className="monitor-error-details">
                            <summary>错误详情</summary>
                            <pre>{run.failure_message}</pre>
                          </details>
                        </>
                      )}
                    </td>
                    <td>¥ {Number(run.cost_cny || 0).toFixed(2)}</td>
                    <td>
                      <button
                        type="button"
                        className="btn small"
                        onClick={() => choose(run.id)}
                        aria-label={`查看${businessName(run.workflow_type)}详情`}
                      >
                        详情 / 处理
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {selected && (
            <div className="run-detail">
              {detailError && (
                <div className="monitor-state error" role="alert">
                  目标运行任务无法定位：{detailError}。不会自动改选其他任务。
                  <button
                    onClick={() => {
                      setSelected(null);
                      onSelect?.(null);
                    }}
                  >
                    返回列表
                  </button>
                </div>
              )}
              {current && (
                <>
                  <div className="run-detail-summary">
                    <b>{businessName(current.workflow_type)}</b>
                    <span>
                      {current.project_name ||
                        "上下文未关联"}{" "}
                      {current.episode_no
                        ? `· 第${current.episode_no}集 `
                        : ""}
                      {current.shot_no != null ? `· 镜${current.shot_no} ` : ""}
                      · 已记录费用 ¥
                      {Number(current.cost_cny || 0).toFixed(2)}
                    </span>
                    <details>
                      <summary>技术标识</summary>
                      <code>{current.id}</code>
                      <small>{current.scope_type}:{current.scope_id}</small>
                    </details>
                  </div>
                  {current.failure_message && (
                    <div className="monitor-impact">
                      <b>当前影响：</b>
                      <span>{runFailureGuidance(current.status)}</span>
                      <details className="monitor-error-details">
                        <summary>查看错误详情</summary>
                        <pre>{current.failure_message}</pre>
                        <code>
                          {current.failure_code || "未记录错误码"} ·{" "}
                          {current.current_step_key || "未记录步骤"}
                        </code>
                      </details>
                    </div>
                  )}
                  <div className="monitor-run-actions">
                    {current.status === "RUNNING" && !confirmAction && (
                      <button
                        className="danger"
                        aria-label={actionBusy ? "取消运行，暂不可用：正在处理上一项操作" : "取消当前运行"}
                        onClick={() => setConfirmAction("cancel")}
                      >
                        取消运行
                      </button>
                    )}
                    {[
                      "PAUSED_EXTERNAL",
                      "PAUSED_BUDGET",
                      "WAITING_RETRY",
                      "WAITING_HUMAN",
                      "WAITING_AUTHORIZATION",
                    ].includes(current.status) &&
                      !confirmAction && (
                      <button
                        onClick={() => setConfirmAction("resume")}
                        disabled={!!actionBusy}
                        aria-label={actionBusy ? "从检查点恢复，暂不可用：正在处理上一项操作" : "从安全检查点恢复运行"}
                      >
                        从检查点恢复
                      </button>
                    )}
                    {["FAILED", "PARTIAL", "CANCELLED"].includes(
                      current.status,
                    ) &&
                      !confirmAction && (
                      <button
                        onClick={() => setConfirmAction("retry")}
                        disabled={!!actionBusy}
                        aria-label={actionBusy ? "受控重试，暂不可用：正在处理上一项操作" : "受控重试当前运行"}
                      >
                        受控重试
                      </button>
                    )}
                    {confirmAction && (
                      <span className="monitor-inline-confirm">
                        {confirmAction === "cancel"
                          ? "取消会中止当前任务，已产生的上游费用仍会保留。"
                          : confirmAction === "resume"
                            ? "恢复会从安全检查点继续，并可能产生新的模型费用。"
                            : "重试会创建新的执行轮次，并可能产生新的模型费用。"}
                        <button
                          onClick={() => void runAction(confirmAction)}
                          disabled={!!actionBusy}
                        >
                          {actionBusy
                            ? "处理中…"
                            : confirmAction === "cancel"
                              ? "确认取消"
                              : confirmAction === "resume"
                                ? "确认恢复"
                                : "确认重试"}
                        </button>
                        <button
                          disabled={!!actionBusy}
                          onClick={() => setConfirmAction("")}
                        >
                          返回
                        </button>
                      </span>
                    )}
                  </div>
                  {actionError && (
                    <div className="monitor-state error" role="alert">
                      {actionError}
                    </div>
                  )}
                  {actionMessage && (
                    <div className="monitor-state ready" role="status">
                      {actionMessage}
                    </div>
                  )}
                  <div className="run-timeline">
                    {steps.map((step) => (
                      <div
                        className={`run-step ${step.status.toLowerCase()}`}
                        key={step.id}
                      >
                        <span className="run-step-dot" />
                        <div>
                          <b>{businessName(step.step_key, STEP_LABELS)}</b>
                          <span>
                            {STATUS_LABELS[step.status] || "状态待确认"} ·{" "}
                            {(step.latency_ms / 1000).toFixed(1)} 秒
                          </span>
                          {(step.error_message || step.exit_reason) && (
                            <details className="monitor-error-details">
                              <summary>查看步骤错误</summary>
                              <pre>{step.error_message || step.exit_reason}</pre>
                            </details>
                          )}
                          <details>
                            <summary>技术标识</summary>
                            <code>
                              {step.step_key} · {step.id}
                            </code>
                          </details>
                        </div>
                      </div>
                    ))}
                  </div>
                  {!!events.length && (
                    <details className="run-events">
                      <summary>审计事件（{events.length}）</summary>
                      {events
                        .slice()
                        .reverse()
                        .map((event) => (
                          <div key={event.id}>
                            <time>{formatTime(event.ts)}</time>
                            <span>{event.message}</span>
                          </div>
                        ))}
                    </details>
                  )}
                </>
              )}
            </div>
          )}
          <div className="monitor-pagination">
            <span>共 {runs.total} 条真实记录</span>
            <label>
              每页
              <select
                aria-label={`每页显示运行记录数，当前 ${pageSize} 条`}
                value={pageSize}
                onChange={(e) => {
                  const size = Number(e.target.value);
                  setPageSize(size);
                  setPage(1);
                  setUrlParams({ run_page_size: String(size), run_page: null });
                }}
              >
                <option>20</option>
                <option>40</option>
                <option>80</option>
              </select>
            </label>
            <button
              disabled={page <= 1}
              aria-label={page <= 1 ? "上一页，暂不可用：当前已是第一页" : "上一页"}
              onClick={() => {
                setPage(page - 1);
                setUrlParams({ run_page: String(page - 1) });
              }}
            >
              上一页
            </button>
            <b>
              {page} / {runs.page_count}
            </b>
            <button
              disabled={page >= runs.page_count}
              aria-label={page >= runs.page_count ? "下一页，暂不可用：当前已是最后一页" : "下一页"}
              onClick={() => {
                setPage(page + 1);
                setUrlParams({ run_page: String(page + 1) });
              }}
            >
              下一页
            </button>
          </div>
        </>
      )}
      {openGate && (
        <GateDrawer
          gate={openGate}
          projectId={projectId}
          onClose={() => setOpenGate(null)}
          onDone={() => void Promise.all([refreshGates(), refreshRuns(true)])}
        />
      )}
      {traceRun && projectId && (
        <TraceDrawer
          projectId={projectId}
          target={{
            type: "runs",
            id: traceRun.id,
            title: businessName(traceRun.workflow_type),
          }}
          onClose={() => setTraceRun(null)}
        />
      )}
    </section>
  );
}
