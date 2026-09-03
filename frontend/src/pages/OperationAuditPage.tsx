import { useEffect, useMemo, useState } from "react";
import { api, ApiError, type AuditEvent, type AuditFacets } from "../api";
import { AuditFilters, EMPTY_AUDIT_FILTERS, type AuditFilterState } from "./audit/AuditFilters";
import { AuditTable } from "./audit/AuditTable";
import { DEFAULT_TIME_PRESET, presetRange } from "./audit/auditLabels";
import "../styles/OperationAuditPage.css";

/** 操作审计——系统管理员专属，从任务失败/异常反查「谁触发的、经哪个入口、
 *  作用在什么对象上」。账号卡片的「查看操作记录」会带 ?user_id= 跳到本页，
 *  这里在初始化时把它读进筛选（CLAUDE.md「界面承诺必须与实际行为一致」）。 */

function initialUserIdFromUrl(): string {
  try {
    return new URLSearchParams(window.location.search).get("user_id") || "";
  } catch {
    return "";
  }
}

export default function OperationAuditPage() {
  const [filters, setFilters] = useState<AuditFilterState>(() => ({
    presetKey: DEFAULT_TIME_PRESET,
    ...EMPTY_AUDIT_FILTERS,
    userId: initialUserIdFromUrl(),
  }));
  const [qDraft, setQDraft] = useState("");
  const [facets, setFacets] = useState<AuditFacets | null>(null);
  const [items, setItems] = useState<AuditEvent[] | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const range = useMemo(() => presetRange(filters.presetKey), [filters.presetKey]);

  const loadFacets = () => {
    void api.getAuditFacets(range).then(setFacets).catch(() => undefined);
  };

  const loadEvents = async (cursor?: string) => {
    if (cursor) setLoadingMore(true);
    else setLoading(true);
    setError(null);
    try {
      const page = await api.listAuditEvents({
        since: range.since,
        user_id: filters.userId || undefined,
        event: filters.event || undefined,
        outcome: filters.outcome || undefined,
        source: filters.source || undefined,
        project_id: filters.projectId || undefined,
        q: filters.q || undefined,
        cursor,
      });
      setItems((prev) => (cursor ? [...(prev ?? []), ...page.items] : page.items));
      setNextCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  };

  useEffect(() => {
    loadFacets();
    void loadEvents();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.presetKey, filters.userId, filters.event, filters.outcome, filters.source, filters.projectId, filters.q]);

  const refresh = () => {
    loadFacets();
    void loadEvents();
  };

  return (
    <div className="operation-audit-page">
      <header className="desk-head">
        <h1>操作审计</h1>
        <p className="sub">按用户、事件与时间溯源用户触发了哪些任务，记录保留 365 天，到期自动清理。</p>
        <hr className="rule" />
      </header>

      <AuditFilters
        filters={filters}
        facets={facets}
        qDraft={qDraft}
        onPreset={(key) => setFilters((prev) => ({ ...prev, presetKey: key }))}
        onSelect={(key, value) => setFilters((prev) => ({ ...prev, [key]: value }))}
        onQDraftChange={setQDraft}
        onQSubmit={() => setFilters((prev) => ({ ...prev, q: qDraft }))}
        onRefresh={refresh}
        refreshing={loading}
      />

      {error && (
        <div className="empty query-error" role="alert">
          <strong>{items ? "刷新失败，仍显示上次数据" : "加载失败"}</strong>
          <p>{error}</p>
          <button type="button" className="btn" onClick={() => void loadEvents()}>重试</button>
        </div>
      )}

      {loading && !items && <p className="audit-muted">载入中…</p>}
      {items && !items.length && !error && <p className="audit-muted">当前筛选下没有操作记录。</p>}
      {items && items.length > 0 && <AuditTable items={items} />}

      {nextCursor && (
        <div className="audit-load-more">
          <button type="button" className="btn" disabled={loadingMore} onClick={() => void loadEvents(nextCursor)}>
            {loadingMore ? "加载中…" : "加载更多"}
          </button>
        </div>
      )}
    </div>
  );
}
