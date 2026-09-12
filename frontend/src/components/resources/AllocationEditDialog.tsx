import { useEffect, useId, useState, type FormEvent } from "react";
import { ApiError, listTeams, putQuotaAllocation, type QuotaAllocation, type QuotaLimits, type QuotaPlan, type TeamRow } from "../../api";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import { RESOURCE_LABELS } from "./resourceLabels";

const DIMENSIONS: (keyof QuotaLimits)[] = [
  "projects", "concurrency", "token", "video_seconds", "image", "storage_bytes", "project_concurrency",
];

interface Props {
  allocation: QuotaAllocation | null;
  plans: QuotaPlan[];
  orgId: string;
  onClose: () => void;
  onSaved: () => void;
}

/** 新增/编辑一条组织级、团队级或用户级配额分配——PUT /api/system/quota/
 *  allocations/{scope_type}/{scope_id}。overrides 留空 = 继承所选策略的原值
 *  （不是"改成 0"或"不限"，见 app/quota_policy/plans.py::validate_limits_
 *  payload 的"缺键 vs 显式 null"区分——这里用空字符串表示"缺键"）。 */
export default function AllocationEditDialog({ allocation, plans, orgId, onClose, onSaved }: Props) {
  const titleId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [scopeType, setScopeType] = useState<"org" | "team" | "user">(allocation?.scope_type ?? "org");
  const [scopeId, setScopeId] = useState(allocation?.scope_id ?? (scopeType === "org" ? orgId : ""));
  const [planId, setPlanId] = useState(allocation?.plan_id ?? plans[0]?.id ?? "");
  const [overrides, setOverrides] = useState<Record<string, string>>(() => stringifyOverrides(allocation?.overrides));
  const [teams, setTeams] = useState<TeamRow[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (scopeType === "team") listTeams().then((r) => setTeams(r.items)).catch(() => setTeams([]));
  }, [scopeType]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!scopeId.trim() || !planId) {
      setError("scope 与策略不能为空");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const overridePayload = parseOverrides(overrides);
      await putQuotaAllocation(scopeType, scopeId.trim(), {
        plan_id: planId,
        overrides: Object.keys(overridePayload).length ? overridePayload : undefined,
      });
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={(e) => { if (e.currentTarget === e.target) onClose(); }}>
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <form onSubmit={submit}>
          <h3 id={titleId}>{allocation ? `编辑分配：${allocation.scope_id}` : "新增分配"}</h3>
          <div className="login-field">
            <label className="f">范围</label>
            <select value={scopeType} disabled={!!allocation || saving} onChange={(e) => setScopeType(e.target.value as typeof scopeType)}>
              <option value="org">组织</option>
              <option value="team">团队</option>
              <option value="user">用户</option>
            </select>
          </div>
          {scopeType === "team" ? (
            <div className="login-field">
              <label className="f">团队</label>
              <select value={scopeId} disabled={!!allocation || saving} onChange={(e) => setScopeId(e.target.value)}>
                <option value="">请选择</option>
                {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </div>
          ) : (
            <div className="login-field">
              <label className="f">{scopeType === "org" ? "组织 ID" : "用户 ID"}</label>
              <input value={scopeId} disabled={!!allocation || saving || scopeType === "org"} onChange={(e) => setScopeId(e.target.value)} />
            </div>
          )}
          <div className="login-field">
            <label className="f">策略</label>
            <select value={planId} disabled={saving} onChange={(e) => setPlanId(e.target.value)}>
              {plans.map((p) => <option key={p.id} value={p.id}>{p.name}{p.builtin ? "（内置）" : ""}</option>)}
            </select>
          </div>
          <fieldset>
            <legend>覆盖值（留空 = 继承所选策略）</legend>
            {DIMENSIONS.map((dim) => (
              <div className="login-field" key={dim}>
                <label className="f">{RESOURCE_LABELS[dim] || dim}</label>
                <input
                  type="text" inputMode="numeric" placeholder="不覆盖"
                  value={overrides[dim] ?? ""} disabled={saving}
                  onChange={(e) => setOverrides((prev) => ({ ...prev, [dim]: e.target.value }))}
                />
              </div>
            ))}
          </fieldset>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onClose} disabled={saving}>取消</button>
            <button type="submit" className="btn primary" disabled={saving}>{saving ? "保存中…" : "保存"}</button>
          </div>
        </form>
      </section>
    </div>
  );
}

function stringifyOverrides(overrides: QuotaLimits | undefined): Record<string, string> {
  const result: Record<string, string> = {};
  if (!overrides) return result;
  for (const dim of DIMENSIONS) {
    const value = overrides[dim];
    if (value !== undefined) result[dim] = value === null ? "null" : String(value);
  }
  return result;
}

function parseOverrides(overrides: Record<string, string>): QuotaLimits {
  const result: QuotaLimits = {};
  for (const dim of DIMENSIONS) {
    const raw = (overrides[dim] ?? "").trim();
    if (!raw) continue;
    (result as Record<string, number | null>)[dim] = raw === "null" ? null : Number(raw);
  }
  return result;
}
