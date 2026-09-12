import type { PurposeBindingRow, PurposeStatus } from "../../../api";

/** 用途绑定：按 purpose 展示优先级链（priority 0 = 主用，1..n = fallback），
 *  支持调整顺序（上移/下移，交换两个槽位的候选模型）与启用停用某个槽位
 *  （EP-05 §8 第 3 条）。缺 priority=0 绑定、正在用备用顶着这两类状态除了
 *  顶部横幅外，这里对应行也带醒目标记——横幅可能被滚动划走，行内标记不会。 */
export default function PurposeBindings({
  purposes,
  busyKey,
  onSwap,
  onToggleEnabled,
}: {
  purposes: PurposeStatus[];
  busyKey: string | null;
  onSwap: (purpose: string, a: PurposeBindingRow, b: PurposeBindingRow) => void;
  onToggleEnabled: (purpose: string, binding: PurposeBindingRow) => void;
}) {
  if (!purposes.length) return <p className="model-ops-empty">还没有任何用途绑定。</p>;
  return (
    <div className="model-ops-purposes">
      {purposes.map((p) => (
        <div
          key={p.purpose}
          className={`model-ops-purpose-row ${p.missing_priority_zero ? "model-ops-purpose-missing" : ""} ${p.fallback_active ? "model-ops-purpose-fallback" : ""}`}
        >
          <div className="model-ops-purpose-head">
            <code>{p.purpose}</code>
            {p.missing_priority_zero && (
              <span className="model-ops-flag model-ops-flag-danger">缺主用绑定</span>
            )}
            {p.fallback_active && (
              <span className="model-ops-flag model-ops-flag-warning">
                正在用第 {p.active_priority} 优先级顶着 · {p.reason_label}
              </span>
            )}
          </div>
          <ol className="model-ops-chain">
            {p.bindings.map((binding, index) => (
              <li key={binding.id} className={binding.enabled ? "" : "model-ops-chain-disabled"}>
                <span className="model-ops-chain-priority">
                  {binding.priority === 0 ? "主用" : `备用 ${binding.priority}`}
                </span>
                <span>{binding.label}</span>
                {binding.model_id === p.active_model_id && (
                  <span className="model-ops-flag model-ops-flag-active">当前生效</span>
                )}
                <button
                  type="button"
                  className="btn ghost small"
                  disabled={index === 0 || busyKey === `${p.purpose}:${binding.priority}`}
                  aria-label={`把「${binding.label}」上移一位`}
                  onClick={() => onSwap(p.purpose, binding, p.bindings[index - 1])}
                >
                  ↑
                </button>
                <button
                  type="button"
                  className="btn ghost small"
                  disabled={index === p.bindings.length - 1 || busyKey === `${p.purpose}:${binding.priority}`}
                  aria-label={`把「${binding.label}」下移一位`}
                  onClick={() => onSwap(p.purpose, binding, p.bindings[index + 1])}
                >
                  ↓
                </button>
                <button
                  type="button"
                  className="btn ghost small"
                  disabled={busyKey === `${p.purpose}:${binding.priority}`}
                  onClick={() => onToggleEnabled(p.purpose, binding)}
                >
                  {binding.enabled ? "停用" : "启用"}
                </button>
              </li>
            ))}
          </ol>
        </div>
      ))}
    </div>
  );
}
