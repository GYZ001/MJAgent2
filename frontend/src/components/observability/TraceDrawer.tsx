import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../../api";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import JsonViewer from "../JsonViewer";

export type TraceTargetType = "runs" | "jobs" | "calls";

export interface TraceTarget {
  type: TraceTargetType;
  id: string;
  title: string;
  source?: string;
}

export interface TraceNode {
  id: string;
  parent_id: string | null;
  kind: "run" | "step" | "job" | "call";
  name: string;
  subtitle: string;
  status: string;
  started_at?: number | null;
  finished_at?: number | null;
  latency_ms: number;
}

interface TraceView {
  source: { type: TraceTargetType; id: string };
  scope: { type: "project"; project_id: string; project_name: string };
  run_id?: string | null;
  title: string;
  status: string;
  started_at?: number | null;
  finished_at?: number | null;
  latency_ms?: number;
  cost_cny?: number;
  selected_node_id: string;
  nodes: TraceNode[];
  server_time: number;
}

interface TraceNodeDetail extends TraceNode {
  input: unknown;
  output: unknown;
  metadata: unknown;
}

type DetailTab = "input" | "output" | "metadata";

const STATUS_LABELS: Record<string, string> = {
  CREATED: "待启动",
  PENDING: "待执行",
  RUNNING: "运行中",
  WAITING_RETRY: "等待重试",
  WAITING_HUMAN: "等待人工",
  WAITING_AUTHORIZATION: "等待授权",
  PAUSED_BUDGET: "预算暂停",
  PAUSED_EXTERNAL: "外部中断",
  SUCCEEDED: "成功",
  SUCCESS: "成功",
  OK: "成功",
  FAILED: "失败",
  PARTIAL: "部分完成",
  CANCELLED: "已取消",
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  cancelled: "已取消",
};

const KIND_LABELS: Record<TraceNode["kind"], string> = {
  run: "运行",
  step: "步骤",
  job: "任务",
  call: "模型调用",
};

function statusLabel(status: string) {
  return STATUS_LABELS[status] || "状态待确认";
}

function formatTime(value?: number | null) {
  return value
    ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false })
    : "—";
}

function formatDuration(value?: number | null) {
  const milliseconds = Math.max(0, Number(value || 0));
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)} 秒`;
  return `${Math.floor(milliseconds / 60_000)} 分 ${Math.round((milliseconds % 60_000) / 1000)} 秒`;
}

export function traceRoots(nodes: TraceNode[]) {
  const ids = new Set(nodes.map((node) => node.id));
  return nodes.filter((node) => !node.parent_id || !ids.has(node.parent_id));
}

function statusTone(status: string) {
  const normalized = status.toLowerCase();
  if (["succeeded", "success", "ok", "recovered"].includes(normalized))
    return "success";
  if (["failed", "partial", "cancelled", "paused_external"].includes(normalized))
    return "failed";
  if (["running", "created", "pending", "queued"].includes(normalized))
    return "running";
  return "waiting";
}

function TraceTreeNode({
  node,
  childrenByParent,
  selectedId,
  onSelect,
  depth = 0,
}: {
  node: TraceNode;
  childrenByParent: Map<string, TraceNode[]>;
  selectedId: string;
  onSelect: (node: TraceNode) => void;
  depth?: number;
}) {
  const children = childrenByParent.get(node.id) || [];
  return (
    <li>
      <button
        type="button"
        className={node.id === selectedId ? "active" : ""}
        style={{ paddingLeft: `${12 + depth * 18}px` }}
        aria-current={node.id === selectedId ? "true" : undefined}
        onClick={() => onSelect(node)}
      >
        <span className={`trace-node-status ${statusTone(node.status)}`} aria-hidden="true" />
        <span className="trace-node-copy">
          <b>{node.name}</b>
          <small>
            {KIND_LABELS[node.kind]} · {node.subtitle}
          </small>
        </span>
        <span className="trace-node-duration">{formatDuration(node.latency_ms)}</span>
      </button>
      {children.length > 0 && (
        <ul>
          {children.map((child) => (
            <TraceTreeNode
              key={child.id}
              node={child}
              childrenByParent={childrenByParent}
              selectedId={selectedId}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function TraceDrawer({
  projectId,
  target,
  onClose,
}: {
  projectId: string;
  target: TraceTarget;
  onClose: () => void;
}) {
  const [trace, setTrace] = useState<TraceView | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<TraceNodeDetail | null>(null);
  const [tab, setTab] = useState<DetailTab>("input");
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [detailError, setDetailError] = useState("");
  const detailSequence = useRef(0);
  const drawerRef = useFocusTrap(true, onClose);
  const sourceQuery = target.source
    ? `?source=${encodeURIComponent(target.source)}`
    : "";
  const basePath = `/projects/${encodeURIComponent(projectId)}/observability/traces/${target.type}/${encodeURIComponent(target.id)}`;

  const loadNode = useCallback(
    async (nodeId: string) => {
      const sequence = ++detailSequence.current;
      setSelectedId(nodeId);
      setDetailLoading(true);
      setDetailError("");
      try {
        const next = (await api.get(
          `${basePath}/nodes/${encodeURIComponent(nodeId)}${sourceQuery}`,
        )) as TraceNodeDetail;
        if (detailSequence.current === sequence) setDetail(next);
      } catch (reason) {
        if (detailSequence.current === sequence) {
          setDetail(null);
          setDetailError((reason as Error).message);
        }
      } finally {
        if (detailSequence.current === sequence) setDetailLoading(false);
      }
    },
    [basePath, sourceQuery],
  );

  const loadTrace = useCallback(
    async (manual = false) => {
      if (manual) setRefreshing(true);
      else setLoading(true);
      setError("");
      try {
        const next = (await api.get(`${basePath}${sourceQuery}`)) as TraceView;
        if (next.scope.project_id !== projectId) {
          throw new Error("链路响应的项目范围与当前页面不一致");
        }
        setTrace(next);
        const nextSelected = next.nodes.some((node) => node.id === selectedId)
          ? selectedId
          : next.selected_node_id;
        await loadNode(nextSelected);
      } catch (reason) {
        setError((reason as Error).message);
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [basePath, loadNode, projectId, selectedId, sourceQuery],
  );

  useEffect(() => {
    setTrace(null);
    setDetail(null);
    setSelectedId("");
    setTab("input");
    void loadTrace();
    // The target path is the lifecycle boundary; selection changes must not reload the tree.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [basePath, sourceQuery]);

  const childrenByParent = useMemo(() => {
    const groups = new Map<string, TraceNode[]>();
    for (const node of trace?.nodes || []) {
      if (!node.parent_id) continue;
      const group = groups.get(node.parent_id) || [];
      group.push(node);
      groups.set(node.parent_id, group);
    }
    return groups;
  }, [trace?.nodes]);
  const roots = useMemo(() => traceRoots(trace?.nodes || []), [trace?.nodes]);
  const selectedNode = trace?.nodes.find((node) => node.id === selectedId);
  const detailValue =
    tab === "input" ? detail?.input : tab === "output" ? detail?.output : detail?.metadata;

  return (
    <div
      className="monitor-drawer-backdrop trace-drawer-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <aside
        className="monitor-drawer trace-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="trace-drawer-title"
        ref={(node) => {
          drawerRef.current = node;
        }}
      >
        <header>
          <div>
            <span className="eyebrow">链路详情</span>
            <h3 id="trace-drawer-title">{trace?.title || target.title}</h3>
          </div>
          <div className="trace-drawer-head-actions">
            <button
              type="button"
              className="monitor-refresh"
              disabled={refreshing}
              onClick={() => void loadTrace(true)}
            >
              <span aria-hidden="true">↻</span>
              {refreshing ? "刷新中…" : "刷新链路"}
            </button>
            <button type="button" onClick={onClose} aria-label="关闭链路详情">
              ×
            </button>
          </div>
        </header>

        {error && (
          <div className="monitor-state error" role="alert">
            链路加载失败：{error}
            <button type="button" onClick={() => void loadTrace()}>
              重试
            </button>
          </div>
        )}
        {loading && !trace && (
          <div className="monitor-loading" role="status">
            正在加载调用树…
          </div>
        )}
        {trace && (
          <>
            <div className="trace-summary">
              <span>
                <b>{statusLabel(trace.status)}</b>
                状态
              </span>
              <span>
                <b>{formatDuration(trace.latency_ms)}</b>
                总耗时
              </span>
              <span>
                <b>{trace.nodes.length}</b>
                链路节点
              </span>
              <span>
                <b>¥ {Number(trace.cost_cny || 0).toFixed(2)}</b>
                已记录费用
              </span>
              <span>
                <b>{formatTime(trace.started_at)}</b>
                开始时间
              </span>
            </div>
            <div className="trace-workspace">
              <section className="trace-tree-panel" aria-label="调用树">
                <div className="trace-panel-title">
                  <b>调用树</b>
                  <span>{trace.nodes.length} 个节点</span>
                </div>
                <ul className="trace-tree">
                  {roots.map((node) => (
                    <TraceTreeNode
                      key={node.id}
                      node={node}
                      childrenByParent={childrenByParent}
                      selectedId={selectedId}
                      onSelect={(next) => void loadNode(next.id)}
                    />
                  ))}
                </ul>
              </section>
              <section className="trace-detail-panel" aria-live="polite">
                <div className="trace-detail-head">
                  <div>
                    <span>{selectedNode ? KIND_LABELS[selectedNode.kind] : "节点"}</span>
                    <h4>{selectedNode?.name || "请选择链路节点"}</h4>
                  </div>
                  {selectedNode && (
                    <dl>
                      <div>
                        <dt>状态</dt>
                        <dd>{statusLabel(selectedNode.status)}</dd>
                      </div>
                      <div>
                        <dt>耗时</dt>
                        <dd>{formatDuration(selectedNode.latency_ms)}</dd>
                      </div>
                    </dl>
                  )}
                </div>
                <nav className="trace-detail-tabs" aria-label="节点详情类型">
                  {([
                    ["input", "输入"],
                    ["output", "输出"],
                    ["metadata", "元数据"],
                  ] as Array<[DetailTab, string]>).map(([key, label]) => (
                    <button
                      type="button"
                      key={key}
                      className={tab === key ? "active" : ""}
                      aria-current={tab === key ? "page" : undefined}
                      onClick={() => setTab(key)}
                    >
                      {label}
                    </button>
                  ))}
                </nav>
                {detailLoading && (
                  <div className="monitor-loading" role="status">
                    正在加载节点{tab === "input" ? "输入" : tab === "output" ? "输出" : "元数据"}…
                  </div>
                )}
                {detailError && (
                  <div className="monitor-state error" role="alert">
                    节点详情加载失败：{detailError}
                    <button type="button" onClick={() => void loadNode(selectedId)}>
                      重试
                    </button>
                  </div>
                )}
                {detail && !detailLoading && (
                  <div className="trace-json">
                    <JsonViewer data={detailValue} collapsed={false} maxHeight="calc(100vh - 340px)" />
                  </div>
                )}
              </section>
            </div>
          </>
        )}
      </aside>
    </div>
  );
}
