import type { PurposeStatus } from "../../../api";
import { formatTimestamp } from "./constants";

/** 顶部横幅：哪些用途正在用备用模型顶着、哪些用途压根没配主用模型。
 *  两类信号分开展示——前者是"配置本身没问题，运行时悄悄换了路"（EP-05 §8
 *  第 4 条，重点：主用 Key 过期等失败会被静默换路到下一优先级，可用性没问题
 *  但没人知道，且可能悄悄推高成本）；后者是"从一开始就没有主用绑定"（第 3
 *  条，后端启动自检已经会报，这里是界面上的可见位置）。两者都不显著时不渲染
 *  任何东西——空横幅本身就是噪音。 */
export default function FallbackBanner({ purposes }: { purposes: PurposeStatus[] }) {
  const fallingBack = purposes.filter((p) => p.fallback_active);
  const missingPrimary = purposes.filter((p) => p.missing_priority_zero);
  if (!fallingBack.length && !missingPrimary.length) return null;
  return (
    <div className="model-ops-banner" role="alert">
      {fallingBack.length > 0 && (
        <div className="model-ops-banner-row model-ops-banner-fallback">
          <span className="model-ops-banner-icon" aria-hidden="true">⚠</span>
          <div>
            <strong>{fallingBack.length} 个用途正在用备用模型顶着</strong>
            <ul>
              {fallingBack.map((p) => (
                <li key={p.purpose}>
                  <code>{p.purpose}</code> 正在用第 {p.active_priority} 优先级
                  「{p.active_label || p.active_model_id}」——原因：{p.reason_label || "未知"}，
                  自 {formatTimestamp(p.since)} 起
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {missingPrimary.length > 0 && (
        <div className="model-ops-banner-row model-ops-banner-missing">
          <span className="model-ops-banner-icon" aria-hidden="true">⛔</span>
          <div>
            <strong>{missingPrimary.length} 个用途缺少主用模型绑定</strong>
            <ul>
              {missingPrimary.map((p) => (
                <li key={p.purpose}>
                  <code>{p.purpose}</code> 还没有 priority=0 的绑定，该阶段会报"未配置模型"
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
