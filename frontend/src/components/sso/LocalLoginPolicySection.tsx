import { useEffect, useState } from "react";
import {
  ApiError, getLocalLoginPolicy, setLocalLoginPolicy, type LocalLoginPolicy,
} from "../../api";

const POLICY_LABELS: Record<LocalLoginPolicy, string> = {
  enabled: "启用（默认，本地口令与 SSO 并存）",
  admin_only: "仅管理员可用本地口令（普通账号必须走 SSO）",
  disabled: "禁用本地口令（仅 break-glass 应急通道可用）",
};

/** 强制 SSO 开关（PRD EP-02 §6）。切到 `disabled` 前后端会真的走一遍连通性
 *  自检，失败就拒绝切换并把原因列出来——这是唯一一个"配错了就没人能登录"
 *  的开关，闸门是拒绝不是警告，前端不能吞掉失败原因只说"保存失败"。 */
export default function LocalLoginPolicySection() {
  const [policy, setPolicy] = useState<LocalLoginPolicy | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failures, setFailures] = useState<string[]>([]);

  useEffect(() => {
    getLocalLoginPolicy().then((data) => setPolicy(data.policy)).catch(() => setPolicy("enabled"));
  }, []);

  const apply = async (next: LocalLoginPolicy) => {
    if (saving || next === policy) return;
    setSaving(true);
    setError(null);
    setFailures([]);
    try {
      const data = await setLocalLoginPolicy(next);
      setPolicy(data.policy);
    } catch (err) {
      if (err instanceof ApiError) {
        const detail = err.detail as { message?: string; failures?: string[] } | undefined;
        setError(detail?.message || err.message);
        setFailures(Array.isArray(detail?.failures) ? detail.failures : []);
      } else {
        setError("保存失败，请检查网络后重试");
      }
    } finally {
      setSaving(false);
    }
  };

  if (policy === null) return <div className="card"><p className="hint">正在加载本地登录策略…</p></div>;

  return (
    <div className="card">
      <h3>本地口令登录策略</h3>
      <p className="hint">
        切到「禁用」前会先对所有已启用的 IdP 做一次真实连通性自检；任何一个不通过都会拒绝切换，
        本地口令登录在此之前保持可用。
      </p>
      <div className="login-field">
        <label className="f" htmlFor="local-login-policy-select">策略</label>
        <select
          id="local-login-policy-select"
          value={policy}
          disabled={saving}
          onChange={(event) => apply(event.target.value as LocalLoginPolicy)}
        >
          {(Object.keys(POLICY_LABELS) as LocalLoginPolicy[]).map((key) => (
            <option key={key} value={key}>{POLICY_LABELS[key]}</option>
          ))}
        </select>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {failures.length > 0 && (
        <ul className="field-error">
          {failures.map((item) => <li key={item}>{item}</li>)}
        </ul>
      )}
    </div>
  );
}
