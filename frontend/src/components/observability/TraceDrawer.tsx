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

export type TraceNodeRole =
  | "task"
  | "business_stage"
  | "model_processing"
  | "program_processing";

export interface TraceNode {
  id: string;
  parent_id: string | null;
  kind: "run" | "stage" | "step" | "job" | "call";
  node_role?: TraceNodeRole;
  name: string;
  subtitle: string;
  status: string;
  sequence?: number | null;
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
  run: "总任务",
  stage: "业务环节",
  step: "业务步骤",
  job: "异步任务",
  call: "处理记录",
};
const ROLE_LABELS: Record<TraceNodeRole, string> = {
  task: "总任务",
  business_stage: "业务环节",
  model_processing: "模型处理",
  program_processing: "程序处理",
};
const LEGACY_NODE_LABELS: Record<string, string> = {
  character_discovery: "识别剧本角色",
  screenplay: "生成剧本",
  storyboard: "生成分镜",
  video_generation: "生成镜头视频",
  scene_references: "生成场景参考图",
  character_references: "生成人物参考图",
  character_bible: "生成人物设定",
  scene_bible: "生成场景设定",
  val422_metric: "记录结构校验指标",
};

export function traceNodeRole(node: TraceNode): TraceNodeRole {
  if (node.node_role) return node.node_role;
  if (node.kind === "run") return "task";
  if (node.kind === "stage" || node.kind === "step") return "business_stage";
  if (node.kind === "job") return "program_processing";
  return /metric|normalization|compile|recompile|cache|repair_required/i.test(node.name)
    ? "program_processing"
    : "model_processing";
}

function legacyNodeName(node: TraceNode, parentName?: string) {
  const mapped = LEGACY_NODE_LABELS[node.name];
  if (mapped) return mapped;
  const scene = node.name.match(/^storyboard_scene_(\d+)\.iteration$/);
  if (scene) return `生成第${scene[1]}个场景分镜`;
  const shot = node.name.match(/^storyboard_shot_(\d+)\.iteration$/);
  if (shot) return `生成第${shot[1]}镜分镜`;
  if (node.name === "screenplay.iteration") return "执行剧本生成";
  if (["文本模型调用", "模型调用"].includes(node.name))
    return parentName ? `为“${parentName}”生成业务内容` : "生成业务内容";
  if (/^[A-Za-z0-9_.:-]+$/.test(node.name)) {
    const role = traceNodeRole(node);
    if (parentName) {
      return role === "model_processing"
        ? `生成“${parentName}”所需内容`
        : `处理“${parentName}”相关数据`;
    }
    return `业务名称待配置（${node.name}）`;
  }
  return node.name;
}

export function traceDisplayNames(nodes: TraceNode[]) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const names = new Map<string, string>();
  const resolve = (node: TraceNode, visiting = new Set<string>()): string => {
    const known = names.get(node.id);
    if (known) return known;
    if (visiting.has(node.id)) return legacyNodeName(node);
    visiting.add(node.id);
    const parent = node.parent_id ? byId.get(node.parent_id) : undefined;
    const name = legacyNodeName(node, parent ? resolve(parent, visiting) : undefined);
    visiting.delete(node.id);
    names.set(node.id, name);
    return name;
  };
  nodes.forEach((node) => resolve(node));
  return names;
}

function traceDisplaySubtitle(node: TraceNode) {
  if (node.subtitle && !/[A-Za-z_]/.test(node.subtitle)) return node.subtitle;
  const role = traceNodeRole(node);
  if (role === "task") return "汇总全部业务环节";
  if (role === "business_stage") return "组织模型与程序处理";
  if (role === "model_processing") return "通过文本生成模型";
  return "通过本地业务规则";
}

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

export interface TraceNodeSummary {
  total: number;
  stages: number;
  models: number;
  programs: number;
}

export function traceNodeSummaries(nodes: TraceNode[]) {
  const children = new Map<string, TraceNode[]>();
  for (const node of nodes) {
    if (!node.parent_id) continue;
    const group = children.get(node.parent_id) || [];
    group.push(node);
    children.set(node.parent_id, group);
  }
  const summaries = new Map<string, TraceNodeSummary>();
  const visit = (nodeId: string, visiting = new Set<string>()): TraceNodeSummary => {
    if (summaries.has(nodeId)) return summaries.get(nodeId)!;
    if (visiting.has(nodeId))
      return { total: 0, stages: 0, models: 0, programs: 0 };
    visiting.add(nodeId);
    const result = { total: 0, stages: 0, models: 0, programs: 0 };
    for (const child of children.get(nodeId) || []) {
      result.total += 1;
      const role = traceNodeRole(child);
      if (role === "business_stage") result.stages += 1;
      if (role === "model_processing") result.models += 1;
      if (role === "program_processing") result.programs += 1;
      const nested = visit(child.id, visiting);
      result.total += nested.total;
      result.stages += nested.stages;
      result.models += nested.models;
      result.programs += nested.programs;
    }
    visiting.delete(nodeId);
    summaries.set(nodeId, result);
    return result;
  };
  nodes.forEach((node) => visit(node.id));
  return summaries;
}

export function traceInitialExpandedIds(nodes: TraceNode[], selectedId: string) {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const parentIds = new Set(
    nodes
      .map((node) => node.parent_id)
      .filter((nodeId): nodeId is string => Boolean(nodeId)),
  );
  const expanded = new Set(
    nodes
      .filter((node) => (
        parentIds.has(node.id)
        && ["task", "business_stage"].includes(traceNodeRole(node))
      ))
      .map((node) => node.id),
  );
  let current = byId.get(selectedId);
  while (current?.parent_id) {
    expanded.add(current.parent_id);
    current = byId.get(current.parent_id);
  }
  return expanded;
}

export function traceNodeOrder(left: TraceNode, right: TraceNode) {
  const leftSequence = Number(left.sequence);
  const rightSequence = Number(right.sequence);
  const leftHasSequence = Number.isFinite(leftSequence) && leftSequence > 0;
  const rightHasSequence = Number.isFinite(rightSequence) && rightSequence > 0;
  if (leftHasSequence !== rightHasSequence) return leftHasSequence ? -1 : 1;
  if (leftHasSequence && leftSequence !== rightSequence)
    return leftSequence - rightSequence;
  const leftStarted = Number(left.started_at);
  const rightStarted = Number(right.started_at);
  const leftHasTime = Number.isFinite(leftStarted) && leftStarted > 0;
  const rightHasTime = Number.isFinite(rightStarted) && rightStarted > 0;
  if (leftHasTime !== rightHasTime) return leftHasTime ? -1 : 1;
  if (leftHasTime && leftStarted !== rightStarted) return leftStarted - rightStarted;
  return left.id.localeCompare(right.id, "zh-CN");
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
  displayNames,
  summaries,
  expandedIds,
  selectedId,
  onSelect,
  onToggle,
  depth = 0,
}: {
  node: TraceNode;
  childrenByParent: Map<string, TraceNode[]>;
  displayNames: Map<string, string>;
  summaries: Map<string, TraceNodeSummary>;
  expandedIds: Set<string>;
  selectedId: string;
  onSelect: (node: TraceNode) => void;
  onToggle: (nodeId: string) => void;
  depth?: number;
}) {
  const children = childrenByParent.get(node.id) || [];
  const role = traceNodeRole(node);
  const displayName = displayNames.get(node.id) || node.name;
  const expanded = expandedIds.has(node.id);
  const summary = summaries.get(node.id);
  const processingSummary =
    summary && (summary.models || summary.programs)
      ? `${expanded ? "包含" : "已合并"}：模型处理 ${summary.models} 项 · 程序处理 ${summary.programs} 项`
      : summary?.stages
        ? `${expanded ? "包含" : "已合并"}：${summary.stages} 个业务环节`
        : "";
  return (
    <li className={`trace-node-${role}`}>
      <div
        className={`trace-node-row${node.id === selectedId ? " active" : ""}`}
        style={{ paddingLeft: `${8 + depth * 18}px` }}
      >
        {children.length > 0 ? (
          <button
            type="button"
            className="trace-node-toggle"
            aria-label={`${expanded ? "折叠" : "展开"}${displayName}下的处理节点`}
            aria-expanded={expanded}
            onClick={() => onToggle(node.id)}
          >
            {expanded ? "⌄" : "›"}
          </button>
        ) : (
          <span className="trace-node-toggle-placeholder" aria-hidden="true" />
        )}
        <button
          type="button"
          className="trace-node-main"
          aria-current={node.id === selectedId ? "true" : undefined}
          onClick={() => onSelect(node)}
        >
          <span className={`trace-node-status ${statusTone(node.status)}`} aria-hidden="true" />
          <span className="trace-node-copy">
            <b>{displayName}</b>
            <small>
              {ROLE_LABELS[role]} · {processingSummary || traceDisplaySubtitle(node)}
            </small>
          </span>
          <span className="trace-node-duration">{formatDuration(node.latency_ms)}</span>
        </button>
      </div>
      {children.length > 0 && expanded && (
        <ul>
          {children.map((child) => (
            <TraceTreeNode
              key={child.id}
              node={child}
              childrenByParent={childrenByParent}
              displayNames={displayNames}
              summaries={summaries}
              expandedIds={expandedIds}
              selectedId={selectedId}
              onSelect={onSelect}
              onToggle={onToggle}
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
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());
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
        const validIds = new Set(next.nodes.map((node) => node.id));
        const requiredExpanded = traceInitialExpandedIds(
          next.nodes,
          nextSelected,
        );
        setExpandedIds((current) => new Set([
          ...[...current].filter((id) => validIds.has(id)),
          ...requiredExpanded,
        ]));
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
    setExpandedIds(new Set());
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
    groups.forEach((group) => group.sort(traceNodeOrder));
    return groups;
  }, [trace?.nodes]);
  const roots = useMemo(
    () => traceRoots(trace?.nodes || []).sort(traceNodeOrder),
    [trace?.nodes],
  );
  const displayNames = useMemo(
    () => traceDisplayNames(trace?.nodes || []),
    [trace?.nodes],
  );
  const summaries = useMemo(
    () => traceNodeSummaries(trace?.nodes || []),
    [trace?.nodes],
  );
  const selectedNode = trace?.nodes.find((node) => node.id === selectedId);
  const detailValue =
    tab === "input" ? detail?.input : tab === "output" ? detail?.output : detail?.metadata;
  const toggleNode = (nodeId: string) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(nodeId)) next.delete(nodeId);
      else next.add(nodeId);
      return next;
    });
  };

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
            <nav className="trace-pane-switch" aria-label="链路视图切换">
              <button
                type="button"
                className={mobilePane === "tree" ? "active" : ""}
                aria-current={mobilePane === "tree" ? "page" : undefined}
                onClick={() => setMobilePane("tree")}
              >
                调用树
              </button>
              <button
                type="button"
                className={mobilePane === "detail" ? "active" : ""}
                aria-current={mobilePane === "detail" ? "page" : undefined}
                onClick={() => setMobilePane("detail")}
              >
                节点详情
              </button>
            </nav>
            <div className="trace-workspace" data-mobile-pane={mobilePane}>
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
                      displayNames={displayNames}
                      summaries={summaries}
                      expandedIds={expandedIds}
                      selectedId={selectedId}
                      onSelect={(next) => {
                        setMobilePane("detail");
                        void loadNode(next.id);
                      }}
                      onToggle={toggleNode}
                    />
                  ))}
                </ul>
              </section>
              <section className="trace-detail-panel" aria-live="polite">
                <div className="trace-detail-head">
                  <div>
                    <span>
                      {selectedNode
                        ? ROLE_LABELS[traceNodeRole(selectedNode)] || KIND_LABELS[selectedNode.kind]
                        : "节点"}
                    </span>
                    <h4>
                      {selectedNode
                        ? displayNames.get(selectedNode.id) || selectedNode.name
                        : "请选择链路节点"}
                    </h4>
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
                <div className="trace-raw-notice" role="note">
                  当前展示项目观测账本中的完整原始数据，未做文字替换、星号遮罩或字段省略。
                </div>
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
                    <JsonViewer
                      key={`${selectedId}:${tab}`}
                      data={detailValue}
                      collapsed={false}
                      expandAll
                      maxHeight="calc(100vh - 340px)"
                    />
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
