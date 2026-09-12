import { useEffect, useState } from "react";
import { ApiError, listQuotaAllocations, listQuotaPlans, type QuotaAllocation, type QuotaPlan } from "../../api";
import AllocationEditDialog from "./AllocationEditDialog";
import { RESOURCE_LABELS, SCOPE_LABELS, formatResourceValue } from "./resourceLabels";

const METER_RESOURCES = ["token", "video_seconds", "image", "storage_bytes"] as const;

/** 分配总览：谁配了多少策略、用了多少、还剩多少（EP-04 §7），逐行给每个已
 *  配置维度画一条用量条——超过 80%/95% 时颜色跟着变（与 AlertBanner 同一套
 *  阈值，双重信号：横幅是"刚跨过的那一刻"，这里是"任何时候打开页面都看得
 *  见的持续状态"）。 */
export default function AllocationsSection({ orgId }: { orgId: string | null }) {
  const [allocations, setAllocations] = useState<QuotaAllocation[] | null>(null);
  const [plans, setPlans] = useState<QuotaPlan[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<QuotaAllocation | "new" | null>(null);

  const reload = () => {
    if (!orgId) return;
    Promise.all([listQuotaAllocations(orgId), listQuotaPlans(orgId)])
      .then(([allocResp, planResp]) => {
        setAllocations(allocResp.items);
        setPlans(planResp.items);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "加载失败"));
  };
  useEffect(reload, [orgId]);

  if (!orgId) return null;

  const planById = new Map(plans.map((p) => [p.id, p]));

  return (
    <section className="card">
      <div className="card-heading-row">
        <h3>配额分配</h3>
        <div className="card-heading-actions">
          <button type="button" className="btn small primary" onClick={() => setEditing("new")}>新增分配</button>
        </div>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {allocations === null && <p className="hint">正在加载…</p>}
      {allocations?.length === 0 && <p className="hint">本组织还没有配置任何组织/团队/用户级分配，全部账号按各自档位默认额度生效。</p>}
      {allocations?.map((item) => (
        <AllocationRow key={item.id} item={item} plan={planById.get(item.plan_id)} onEdit={() => setEditing(item)} />
      ))}
      {editing !== null && (
        <AllocationEditDialog
          allocation={editing === "new" ? null : editing}
          plans={plans}
          orgId={orgId}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); reload(); }}
        />
      )}
    </section>
  );
}

function AllocationRow({ item, plan, onEdit }: { item: QuotaAllocation; plan: QuotaPlan | undefined; onEdit: () => void }) {
  const usage = item.usage || {};
  return (
    <div className="resource-alloc-row">
      <span className="resource-scope-tag">{SCOPE_LABELS[item.scope_type] || item.scope_type}</span>
      <div>
        <div>{item.scope_id}</div>
        <small className="hint">{plan ? plan.name : item.plan_id}</small>
      </div>
      <div className="resource-stat-grid" style={{ margin: 0 }}>
        {METER_RESOURCES.map((resource) => {
          const limit = effectiveLimit(plan, item, resource);
          const used = usage[resource] ?? 0;
          if (limit == null && !used) return null;
          return <ResourceMeterMini key={resource} resource={resource} used={used} limit={limit} />;
        })}
      </div>
      <button type="button" className="btn small" onClick={onEdit}>编辑</button>
    </div>
  );
}

function effectiveLimit(plan: QuotaPlan | undefined, item: QuotaAllocation, resource: string): number | null {
  const overrideValue = item.overrides ? (item.overrides as Record<string, number | null | undefined>)[resource] : undefined;
  if (overrideValue !== undefined) return overrideValue;
  if (!plan) return null;
  const planValue = (plan.limits as Record<string, number | null | undefined>)[resource];
  return planValue === undefined ? null : planValue;
}

function ResourceMeterMini({ resource, used, limit }: { resource: string; used: number; limit: number | null }) {
  const ratio = limit ? Math.min(1, used / limit) : 0;
  const level = ratio >= 0.95 ? "critical" : ratio >= 0.8 ? "warn" : "";
  return (
    <div className="resource-stat-tile" style={{ padding: "6px 10px" }}>
      <div className="r-label">{RESOURCE_LABELS[resource] || resource}</div>
      <div className="r-value" style={{ fontSize: 13 }}>
        {formatResourceValue(resource, used)}
        {limit != null && ` / ${formatResourceValue(resource, limit)}`}
      </div>
      {limit != null && (
        <div className="resource-meter"><div className={`resource-meter-fill ${level}`} style={{ width: `${ratio * 100}%` }} /></div>
      )}
    </div>
  );
}
