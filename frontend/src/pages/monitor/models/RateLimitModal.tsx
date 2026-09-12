import { useState } from "react";
import { useFocusTrap } from "../../../hooks/useFocusTrap";
import type { HealthRow } from "./ModelHealthTable";

export interface RateLimitDraft {
  rpm: string;
  tpm: string;
  concurrency: string;
}

function toDraft(row: HealthRow, current?: { rpm?: number; tpm?: number; concurrency?: number }): RateLimitDraft {
  return {
    rpm: current?.rpm ? String(current.rpm) : "",
    tpm: current?.tpm ? String(current.tpm) : "",
    concurrency: current?.concurrency ? String(current.concurrency) : "",
  };
}

/** 每凭据 RPM/TPM/并发配置入口（EP-05 §7/§8 第 5、6 条）：后端早就支持
 *  `models.rate_limit_json`，此前没有 UI 能拧这个开关。刻意在弹窗里把"进程内
 *  限速、多进程部署下不是全局限速"写清楚——不能让管理员以为这是集群级限速。 */
export default function RateLimitModal({
  row,
  initial,
  saving,
  onClose,
  onSave,
}: {
  row: HealthRow;
  initial?: { rpm?: number; tpm?: number; concurrency?: number };
  saving: boolean;
  onClose: () => void;
  onSave: (draft: { rpm: number; tpm: number; concurrency: number }) => void;
}) {
  const [draft, setDraft] = useState<RateLimitDraft>(() => toDraft(row, initial));
  const modalRef = useFocusTrap(true, onClose);
  const toInt = (value: string) => {
    const n = Number.parseInt(value, 10);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  };
  return (
    <div className="model-modal-backdrop" role="presentation">
      <section
        className="model-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rate-limit-title"
        ref={(node) => {
          modalRef.current = node;
        }}
      >
        <div className="model-modal-head">
          <div>
            <span className="eyebrow">限速配置</span>
            <h2 id="rate-limit-title">{row.label} 的限速</h2>
            <p className="model-ops-honesty-note">
              这是进程内限速（令牌桶 + 信号量），只在当前后端进程生效；多进程部署下总吞吐是
              「进程数 × 这里配的上限」，不是集群级限速。0 或留空表示该维度不限。
            </p>
          </div>
          <button className="model-modal-close" onClick={onClose} aria-label="关闭限速配置">×</button>
        </div>
        <div className="model-form-grid">
          <label className="model-form-field">
            <span>RPM（每分钟请求数）</span>
            <input
              inputMode="numeric"
              value={draft.rpm}
              onChange={(e) => setDraft({ ...draft, rpm: e.target.value })}
            />
          </label>
          <label className="model-form-field">
            <span>TPM（每分钟 token 数）</span>
            <input
              inputMode="numeric"
              value={draft.tpm}
              onChange={(e) => setDraft({ ...draft, tpm: e.target.value })}
            />
          </label>
          <label className="model-form-field">
            <span>并发上限</span>
            <input
              inputMode="numeric"
              value={draft.concurrency}
              onChange={(e) => setDraft({ ...draft, concurrency: e.target.value })}
            />
          </label>
        </div>
        <div className="model-modal-actions">
          <button
            className="btn primary small"
            disabled={saving}
            onClick={() =>
              onSave({ rpm: toInt(draft.rpm), tpm: toInt(draft.tpm), concurrency: toInt(draft.concurrency) })
            }
          >
            {saving ? "保存中…" : "保存限速"}
          </button>
        </div>
      </section>
    </div>
  );
}
