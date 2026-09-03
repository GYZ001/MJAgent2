import type { AuditFacets } from "../../api";
import { outcomeLabel, sourceLabel, TIME_PRESETS } from "./auditLabels";

/** 审计页筛选条状态——由 OperationAuditPage 持有并下发，本组件只负责展示与
 *  转发事件，不自己发请求（复用 MonitorPage 的既有分工：筛选状态归页面）。 */
export interface AuditFilterState {
  presetKey: string;
  userId: string;
  event: string;
  outcome: string;
  source: string;
  projectId: string;
  q: string;
}

export const EMPTY_AUDIT_FILTERS: Omit<AuditFilterState, "presetKey"> = {
  userId: "", event: "", outcome: "", source: "", projectId: "", q: "",
};

type SelectKey = "userId" | "event" | "outcome" | "source" | "projectId";

export function AuditFilters({
  filters, facets, qDraft, onPreset, onSelect, onQDraftChange, onQSubmit, onRefresh, refreshing,
}: {
  filters: AuditFilterState;
  facets: AuditFacets | null;
  qDraft: string;
  onPreset: (key: string) => void;
  onSelect: (key: SelectKey, value: string) => void;
  onQDraftChange: (value: string) => void;
  onQSubmit: () => void;
  onRefresh: () => void;
  refreshing: boolean;
}) {
  return (
    <div className="audit-filters">
      <div className="audit-presets" role="group" aria-label="时间范围">
        {TIME_PRESETS.map((preset) => (
          <button
            key={preset.key}
            type="button"
            className={`btn small ${filters.presetKey === preset.key ? "primary" : "ghost"}`}
            aria-pressed={filters.presetKey === preset.key}
            onClick={() => onPreset(preset.key)}
          >
            {preset.label}
          </button>
        ))}
      </div>
      <div className="audit-toolbar">
        <label>
          <span>用户</span>
          <select aria-label="按用户筛选" value={filters.userId} onChange={(e) => onSelect("userId", e.target.value)}>
            <option value="">全部用户</option>
            {(facets?.users ?? []).map((u) => (
              <option key={u.user_id} value={u.user_id}>{u.username}（{u.count}）</option>
            ))}
          </select>
        </label>
        <label>
          <span>事件</span>
          <select aria-label="按事件筛选" value={filters.event} onChange={(e) => onSelect("event", e.target.value)}>
            <option value="">全部事件</option>
            {(facets?.events ?? []).map((item) => (
              <option key={item.event} value={item.event}>
                {item.event_label ? `${item.event_label}（${item.event}）` : item.event}（{item.count}）
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>结果</span>
          <select aria-label="按结果筛选" value={filters.outcome} onChange={(e) => onSelect("outcome", e.target.value)}>
            <option value="">全部结果</option>
            {(facets?.outcomes ?? []).map((item) => (
              <option key={item.outcome} value={item.outcome}>{outcomeLabel(item.outcome)}（{item.count}）</option>
            ))}
          </select>
        </label>
        <label>
          <span>来源</span>
          <select aria-label="按来源筛选" value={filters.source} onChange={(e) => onSelect("source", e.target.value)}>
            <option value="">全部来源</option>
            {(facets?.sources ?? []).map((item) => (
              <option key={item.source} value={item.source}>{sourceLabel(item.source)}（{item.count}）</option>
            ))}
          </select>
        </label>
        <label>
          <span>所属空间</span>
          <select aria-label="按所属空间筛选" value={filters.projectId} onChange={(e) => onSelect("projectId", e.target.value)}>
            <option value="">全部空间</option>
            {(facets?.projects ?? []).map((item) => (
              <option key={item.project_id} value={item.project_id}>
                {item.project_name || item.project_id}（{item.count}）
              </option>
            ))}
          </select>
        </label>
        <label className="audit-search">
          <span>搜索</span>
          <input
            aria-label="搜索操作对象或摘要"
            value={qDraft}
            placeholder="搜索操作对象、摘要"
            onChange={(e) => onQDraftChange(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") onQSubmit(); }}
          />
        </label>
        <button type="button" className="btn small" onClick={onQSubmit}>搜索</button>
        <button type="button" className="audit-refresh" disabled={refreshing} onClick={onRefresh}>
          <span aria-hidden="true">↻</span>
          {refreshing ? "刷新中…" : "刷新"}
        </button>
      </div>
    </div>
  );
}
