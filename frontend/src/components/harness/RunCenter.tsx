import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  ArtifactEvidence,
  RunEvent,
  RunSummary,
  StepRun,
} from "../../api";
import { useFocusTrap } from "../../hooks/useFocusTrap";

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
  scene_bible: "场景圣经",
  scene_references: "场景参考图",
  episode_mapping: "分集规划",
  screenplay: "剧本生成",
  storyboard: "分镜生成",
  scene_generation: "关键帧生成",
  video_generation: "视频生成",
  episode_video_completion: "全片视频补齐",
  delivery: "交付包",
  delivery_package: "交付包生成",
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
function formatTime(value?: number | null) {
  return value ? new Date(value * 1000).toLocaleString() : "—";
}
function paramsFromLocation() {
  return new URLSearchParams(window.location.search);
}
function setUrlParams(patch: Record<string, string | null>) {
  const params = paramsFromLocation();
  Object.entries(patch).forEach(([key, value]) =>
    value ? params.set(key, value) : params.delete(key),
  );
  const next = `${window.location.pathname}?${params.toString()}`;
  window.history.pushState(
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
  onClose,
  onDone,
}: {
  gate: GateArtifact;
  onClose: () => void;
  onDone: () => void;
}) {
  const [evidence, setEvidence] = useState<ArtifactEvidence | null>(null);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState("");
  const drawerRef = useFocusTrap(true, onClose);
  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = (await api.get(
        `/artifacts/${encodeURIComponent(gate.id)}`,
      )) as ArtifactEvidence;
      setEvidence(next);
      setStale(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [gate.id]);
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
      await api.post(`/gates/${encodeURIComponent(gate.id)}/decision`, {
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
            <span className="eyebrow">HUMAN GATE</span>
            <h3 id="gate-title">{GATE_LABELS[gate.type] || "人工门禁"}</h3>
          </div>
          <button type="button" onClick={onClose} aria-label="关闭门禁面板">
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
                  : "自动门禁已通过，等待人工确认"}
              </b>
              <span>
                证据 v{evidence.version} · {evidence.trust_level} · 最后同步{" "}
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
                onChange={(e) => setReason(e.target.value)}
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
                onClick={() => void decide("reject")}
              >
                {busy === "reject" ? "提交中…" : "打回修改"}
              </button>
              <button
                type="button"
                className="btn primary small"
                disabled={
                  !reason.trim() || !!busy || stale || blockers.length > 0
                }
                title={
                  blockers.length
                    ? "存在 blocker 时不能直接批准，请先打回处理"
                    : ""
                }
                onClick={() => void decide("approve")}
              >
                {busy === "approve" ? "提交中…" : "批准并继续"}
              </button>
            </div>
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
}: {
  selectedRunId?: string | null;
  focusToken?: string;
  onSelect?: (id: string | null) => void;
}) {
  const initial = paramsFromLocation();
  const [search, setSearch] = useState(initial.get("run_search") || "");
  const [status, setStatus] = useState(initial.get("run_status") || "");
  const [workflow, setWorkflow] = useState(initial.get("run_workflow") || "");
  const [project, setProject] = useState(initial.get("run_project") || "");
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
  const [actionBusy, setActionBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const [confirmCancel, setConfirmCancel] = useState(false);
  const requestSeq = useRef(0);
  const selectedRowRef = useRef<HTMLButtonElement | null>(null);

  const queryPath = useMemo(() => {
    const p = new URLSearchParams({
      page: String(page),
      page_size: String(pageSize),
    });
    if (search.trim()) p.set("search", search.trim());
    if (status) p.set("status", status);
    if (workflow) p.set("workflow", workflow);
    if (project.trim()) p.set("project_id", project.trim());
    if (episode.trim()) p.set("episode_no", episode.trim());
    if (fromTime) p.set("from_ts", String(new Date(fromTime).getTime() / 1000));
    if (toTime) p.set("to_ts", String(new Date(toTime).getTime() / 1000));
    p.set("sort", sort);
    if (includeHistory) p.set("include_history", "true");
    return `/runs/query?${p}`;
  }, [
    episode,
    fromTime,
    includeHistory,
    page,
    pageSize,
    project,
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
        setRuns(next);
        setListError("");
        setLoading(false);
        if (!selected && !selectedRunId && next.items[0]) {
          setSelected(next.items[0].id);
          onSelect?.(next.items[0].id);
        }
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
    [onSelect, page, queryPath, runs, selected, selectedRunId],
  );
  const refreshGates = useCallback(async () => {
    try {
      setGates((await api.get("/gates?limit=100")) as GateArtifact[]);
      setGateError("");
    } catch (e) {
      setGateError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void refreshRuns();
    const timer = window.setInterval(() => void refreshRuns(true), 4000);
    return () => window.clearInterval(timer);
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
    const timer = window.setInterval(refreshGates, 5000);
    return () => window.clearInterval(timer);
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
      setProject(params.get("run_project") || "");
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
  }, []);
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      setSteps([]);
      setEvents([]);
      return;
    }
    const sequence = ++requestSeq.current;
    setDetailError("");
    Promise.all([
      api.get(`/runs/${encodeURIComponent(selected)}`),
      api.get(`/runs/${encodeURIComponent(selected)}/steps`),
      api.get(`/runs/${encodeURIComponent(selected)}/events?limit=100`),
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
  }, [selected]);
  useEffect(() => {
    if (!focusToken || !selectedRowRef.current) return;
    selectedRowRef.current.scrollIntoView({
      block: "center",
      behavior: "smooth",
    });
    selectedRowRef.current.focus();
    selectedRowRef.current.classList.remove("deep-linked");
    requestAnimationFrame(() =>
      selectedRowRef.current?.classList.add("deep-linked"),
    );
  }, [focusToken, selected, runs]);

  const choose = (id: string) => {
    setSelected(id);
    setUrlParams({ run_id: id, focus: String(Date.now()) });
    onSelect?.(id);
  };
  const updateFilter = (patch: Record<string, string | null>) => {
    setUrlParams(patch);
    setPage(1);
  };
  const runAction = async (action: "cancel" | "resume" | "retry") => {
    if (!selected || actionBusy) return;
    setActionBusy(action);
    setActionError("");
    setActionMessage("");
    try {
      const result = await api.post(
        `/runs/${encodeURIComponent(selected)}/${action}`,
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
          `/runs/${encodeURIComponent(selected)}`,
        )) as RunItem;
        setDetail(nextDetail);
      }
      setActionMessage(
        `${businessName(current?.workflow_type)} ${action === "cancel" ? "取消" : action === "resume" ? "恢复" : "重试"}请求已由服务端接受`,
      );
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setActionBusy("");
      setConfirmCancel(false);
    }
  };
  const current =
    detail || runs?.items.find((run) => run.id === selected) || null;
  const workflows = Object.keys(WORKFLOW_LABELS);
  return (
    <section className="card run-center" aria-busy={loading}>
      <div className="run-center-head">
        <div>
          <span className="eyebrow">ACTIONABLE RUNS</span>
          <h3>运行中心</h3>
          <p>默认聚焦运行中、待人工、失败与可恢复任务；成功历史可按需查询。</p>
        </div>
        <span className="stamp gold">
          {runs ? `共 ${runs.total} 次` : "读取中"}
        </span>
      </div>
      <div className="monitor-toolbar">
        <label className="monitor-search">
          <span>搜索</span>
          <input
            value={search}
            aria-label="搜索运行"
            placeholder="Run、项目、集数或错误"
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
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              updateFilter({
                run_status: e.target.value || null,
                run_page: null,
              });
            }}
          >
            <option value="">默认待办</option>
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
        <label>
          <span>项目 ID</span>
          <input
            value={project}
            onChange={(e) => {
              setProject(e.target.value);
              updateFilter({
                run_project: e.target.value || null,
                run_page: null,
              });
            }}
          />
        </label>
        <label>
          <span>集数</span>
          <input
            type="number"
            min="1"
            value={episode}
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
            value={fromTime}
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
            value={toTime}
            onChange={(e) => {
              setToTime(e.target.value);
              updateFilter({ run_to: e.target.value || null, run_page: null });
            }}
          />
        </label>
        <label>
          <span>排序</span>
          <select
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
          查询成功历史
        </label>
        <button
          className="monitor-clear"
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
          清除组合筛选
        </button>
      </div>
      <div className="gate-queue">
        <div className="gate-queue-head">
          <b>人工门禁队列</b>
          <span>{gates ? `${gates.length} 项待处理` : "加载中"}</span>
        </div>
        {gateError && (
          <div className="monitor-state error" role="alert">
            门禁加载失败：{gateError}
            <button onClick={refreshGates}>重试</button>
          </div>
        )}
        {gates && !gates.length && (
          <span className="gate-empty">
            查询成功：当前没有待人工决定的核心产物
          </span>
        )}
        {gates?.map((item) => (
          <button
            type="button"
            className="gate-item gate-item-button"
            key={`${item.id}-${item.version}`}
            onClick={() => setOpenGate(item)}
            aria-label={`处理${GATE_LABELS[item.type] || item.type}：${item.project_name || item.scope_id}`}
          >
            <div>
              <b>{GATE_LABELS[item.type] || "其他门禁"}</b>
              <span>
                {item.project_name || "上下文未关联"}
                {item.episode_no
                  ? ` · 第${item.episode_no}集 ${item.episode_title || ""}`
                  : ""}
              </span>
            </div>
            <span>查看证据并决策 →</span>
          </button>
        ))}
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
          <div className="run-center-grid">
            <div className="run-list">
              {runs.items.map((run) => (
                <button
                  ref={run.id === selected ? selectedRowRef : undefined}
                  type="button"
                  key={run.id}
                  className={run.id === selected ? "active" : ""}
                  onClick={() => choose(run.id)}
                  aria-pressed={run.id === selected}
                >
                  <b>{businessName(run.workflow_type)}</b>
                  <span>
                    {STATUS_LABELS[run.status] || "其他 / 内部状态"} ·{" "}
                    {formatTime(run.updated_at)}
                  </span>
                  <small>
                    {run.project_name || "上下文未关联"}
                    {run.episode_no ? ` · 第${run.episode_no}集` : ""}
                    {run.failure_message
                      ? ` · ${run.failure_message.slice(0, 100)}`
                      : ""}
                  </small>
                </button>
              ))}
            </div>
            <div className="run-detail">
              {detailError && (
                <div className="monitor-state error" role="alert">
                  目标 Run 无法定位：{detailError}。不会自动改选其他 Run。
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
                    <code>{current.id}</code>
                    <span>
                      {current.project_name ||
                        `${current.scope_type}:${current.scope_id}`}{" "}
                      · ¥{Number(current.cost_cny || 0).toFixed(2)}
                    </span>
                  </div>
                  {current.failure_message && (
                    <div className="monitor-impact">
                      <b>当前影响：</b>
                      {current.failure_message}
                      <details>
                        <summary>技术详情</summary>
                        <code>
                          {current.failure_code || "未记录错误码"} ·{" "}
                          {current.current_step_key || "未记录步骤"}
                        </code>
                      </details>
                    </div>
                  )}
                  <div className="monitor-run-actions">
                    {current.status === "RUNNING" && !confirmCancel && (
                      <button
                        className="danger"
                        onClick={() => setConfirmCancel(true)}
                      >
                        取消运行
                      </button>
                    )}
                    {current.status === "RUNNING" && confirmCancel && (
                      <span className="monitor-inline-confirm">
                        取消可能中止长任务并保留已产生费用。
                        <button
                          onClick={() => void runAction("cancel")}
                          disabled={!!actionBusy}
                        >
                          {actionBusy ? "处理中…" : "确认取消"}
                        </button>
                        <button onClick={() => setConfirmCancel(false)}>
                          返回
                        </button>
                      </span>
                    )}
                    {[
                      "PAUSED_EXTERNAL",
                      "PAUSED_BUDGET",
                      "WAITING_RETRY",
                      "WAITING_HUMAN",
                      "WAITING_AUTHORIZATION",
                    ].includes(current.status) && (
                      <button
                        onClick={() => void runAction("resume")}
                        disabled={!!actionBusy}
                      >
                        {actionBusy ? "处理中…" : "从检查点恢复"}
                      </button>
                    )}
                    {["FAILED", "PARTIAL", "CANCELLED"].includes(
                      current.status,
                    ) && (
                      <button
                        onClick={() => void runAction("retry")}
                        disabled={!!actionBusy}
                      >
                        {actionBusy ? "处理中…" : "受控重试"}
                      </button>
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
                            {STATUS_LABELS[step.status] || "其他 / 内部状态"} ·{" "}
                            {(step.latency_ms / 1000).toFixed(1)} 秒
                          </span>
                          {(step.error_message || step.exit_reason) && (
                            <small>
                              {step.error_message || step.exit_reason}
                            </small>
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
          </div>
          <div className="monitor-pagination">
            <span>共 {runs.total} 条真实记录</span>
            <label>
              每页
              <select
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
          onClose={() => setOpenGate(null)}
          onDone={() => void Promise.all([refreshGates(), refreshRuns(true)])}
        />
      )}
    </section>
  );
}
