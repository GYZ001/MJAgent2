import { useEffect, useState } from "react";
import { ApiError, getSettings, updateSettings, type SettingSchema } from "../../api";

/** 密码/会话策略的 7 个设置键（EP-03 第二阶段），全部沿用
 *  `app.monitoring.SETTINGS_SCHEMA` 既有的通用设置读写接口，不新建配置体系
 *  ——与 app/system_api.py 的 GET/PUT /settings 是同一份契约，这里只是过滤出
 *  这 7 个键单独成一个聚焦的表单，不与监制房那一大堆并发/水位设置混在一起。 */
const KEYS = [
  "password_min_length", "password_classes", "password_max_age_days", "password_history_size",
  "session_idle_timeout_min", "session_max_age_hours", "session_max_concurrent",
] as const;

export default function SecurityPolicySection() {
  const [schema, setSchema] = useState<Record<string, SettingSchema> | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [version, setVersion] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = () => {
    getSettings(true)
      .then((data) => {
        setSchema(data.schema);
        setVersion(data.version);
        const next: Record<string, string> = {};
        for (const key of KEYS) next[key] = data.values[key] ?? String(data.schema[key]?.default ?? "");
        setValues(next);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "加载失败"));
  };
  useEffect(load, []);

  const save = async () => {
    if (version === null) return;
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const result = await updateSettings({ version, patch: values });
      setVersion(result.version);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2600);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  if (!schema) return <div className="card"><p className="hint">正在加载密码/会话策略…</p></div>;

  return (
    <div className="card">
      <h3>密码与会话策略</h3>
      <p className="hint">改动对新的口令/会话立即生效；已存在的会话按新策略在下一次请求时重新判定。</p>
      {error && <p className="field-error" role="alert">{error}</p>}
      {saved && <p className="hint" role="status">已保存</p>}
      {KEYS.map((key) => {
        const spec = schema[key];
        if (!spec) return null;
        const id = `security-policy-${key}`;
        return (
          <div className="login-field" key={key}>
            <label className="f" htmlFor={id}>{spec.label}{spec.unit ? `（${spec.unit}）` : ""}</label>
            <input
              id={id} type="number" min={spec.min} max={spec.max} step={spec.step}
              value={values[key] ?? ""} disabled={busy}
              onChange={(e) => setValues({ ...values, [key]: e.target.value })}
            />
            {spec.description && <span className="hint">{spec.description}</span>}
          </div>
        );
      })}
      <button type="button" className="btn primary" disabled={busy} onClick={() => void save()}>保存策略</button>
    </div>
  );
}
