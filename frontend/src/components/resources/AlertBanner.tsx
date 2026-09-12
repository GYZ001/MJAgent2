import { useEffect, useState } from "react";
import { ApiError, listQuotaAlerts, type QuotaAlert } from "../../api";
import { RESOURCE_LABELS, SCOPE_LABELS } from "./resourceLabels";

/** 80%/95% 预警横幅：最近触发的预警，95% 判「严重」、80% 判「预警」（EP-04
 *  §7）。颜色永远配 icon + 文字，不单靠颜色区分（dataviz 技能的状态色硬约
 *  束）——同一预警只在这个周期第一次跨阈值时出现（后端 UNIQUE 兜底不重复
 *  写入，这里只是如实展示已经写入的记录，不做二次去重判断）。 */
export default function AlertBanner({ orgId }: { orgId: string | null }) {
  const [alerts, setAlerts] = useState<QuotaAlert[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!orgId) return;
    listQuotaAlerts(orgId)
      .then((data) => setAlerts(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : "预警加载失败"));
  }, [orgId]);

  if (!orgId || error || !alerts || alerts.length === 0) return null;

  const critical = alerts.filter((a) => a.threshold >= 0.95);
  const warn = alerts.filter((a) => a.threshold < 0.95);
  const level = critical.length > 0 ? "level-critical" : "level-warn";
  const shown = [...critical, ...warn].slice(0, 8);

  return (
    <div className={`resource-alert-banner ${level}`} role="alert">
      {shown.map((alert) => (
        <div key={alert.id} className={`resource-alert-row ${alert.threshold >= 0.95 ? "critical" : "warn"}`}>
          <span className="resource-alert-icon" aria-hidden="true">{alert.threshold >= 0.95 ? "●" : "▲"}</span>
          <span>
            {SCOPE_LABELS[alert.scope_type] || alert.scope_type}「{alert.scope_id}」
            {RESOURCE_LABELS[alert.resource] || alert.resource} 已用满
            {Math.round(alert.threshold * 100)}%
            {alert.threshold >= 0.95 ? "（严重，即将不可用）" : "（预警，请关注）"}
          </span>
        </div>
      ))}
      {alerts.length > shown.length && (
        <div className="resource-alert-row"><span /><span>另有 {alerts.length - shown.length} 条预警未展示</span></div>
      )}
    </div>
  );
}
