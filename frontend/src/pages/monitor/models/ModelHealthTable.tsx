import type { ModelCredentialSummary, ModelHealthItem } from "../../../api";
import {
  formatLatencyMs,
  formatPercent,
  formatTimestamp,
  healthStateLabel,
} from "./constants";

export interface HealthRow extends ModelHealthItem {
  credential?: ModelCredentialSummary;
}

/** 模型列表：状态/健康度/近 24h 调用与失败率/p50/p95/启停 + 凭据只显示
 *  掩码与指纹（EP-05 §8 第 1、2 条）。轮换入口复用既有 CredentialModal
 *  （见 ModelOpsPanel 里的 onRotate），本表不持有任何明文。 */
export default function ModelHealthTable({
  rows,
  busyModelId,
  onToggleEnabled,
  onRotate,
  onEditRateLimit,
}: {
  rows: HealthRow[];
  busyModelId: string | null;
  onToggleEnabled: (row: HealthRow) => void;
  onRotate: (row: HealthRow, trigger: HTMLElement) => void;
  onEditRateLimit: (row: HealthRow) => void;
}) {
  if (!rows.length) return <p className="model-ops-empty">模型库为空，先在上方「添加模型」。</p>;
  return (
    <table className="model-ops-table" aria-label="模型健康度与凭据">
      <thead>
        <tr>
          <th>模型</th>
          <th>状态</th>
          <th>近 {rows[0]?.window_hours ?? 24}h 调用</th>
          <th>失败率</th>
          <th>p50 / p95</th>
          <th>凭据</th>
          <th>启用</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.model_id} className={`model-ops-state-${row.state}`}>
            <td>
              <div>{row.label}</div>
              <div className="model-ops-muted">{row.provider} · {row.kinds.join("/")}</div>
            </td>
            <td>
              <span className={`model-ops-state-badge model-ops-state-${row.state}`}>
                {healthStateLabel(row.state)}
              </span>
              {row.state === "circuit_open" && row.last_error_code && (
                <div className="model-ops-muted">原因：{row.last_error_code}</div>
              )}
            </td>
            <td>{row.calls_window}（{row.failures_window} 次失败）</td>
            <td>{formatPercent(row.failure_rate_window)}</td>
            <td>{formatLatencyMs(row.p50_latency_ms_window)} / {formatLatencyMs(row.p95_latency_ms_window)}</td>
            <td>
              {row.credential ? (
                <div>
                  <div>{row.credential.masked_key || "已配置（无法显示）"}</div>
                  <div className="model-ops-muted">
                    指纹 {row.credential.key_fingerprint} · 轮换于 {formatTimestamp(row.credential.rotated_at)}
                  </div>
                </div>
              ) : (
                <span className="model-ops-muted">未配置凭据</span>
              )}
              <button
                type="button"
                className="btn ghost small"
                onClick={(event) => onRotate(row, event.currentTarget)}
              >
                轮换
              </button>
              <button
                type="button"
                className="btn ghost small"
                onClick={() => onEditRateLimit(row)}
              >
                限速
              </button>
            </td>
            <td>
              <button
                type="button"
                className="btn ghost small"
                disabled={busyModelId === row.model_id}
                onClick={() => onToggleEnabled(row)}
              >
                {row.enabled ? "停用" : "启用"}
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
