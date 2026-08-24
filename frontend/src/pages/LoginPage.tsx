import { useId, useState, type FormEvent } from "react";
import { ApiError, login } from "../api";
import { useAuth } from "../auth/AuthContext";

/** 未登录时的全屏登录页；AuthProvider 的 status === 'anonymous' 时整个应用壳
 *  都不挂载，这里是唯一可交互的界面。视觉上复用现有 `.card` / `.btn` / `label.f`
 *  与朱砂配色，不另起一套设计语言。 */
export default function LoginPage() {
  const { refresh } = useAuth();
  const usernameId = useId();
  const passwordId = useId();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    const trimmedUsername = username.trim();
    if (!trimmedUsername || !password) {
      setError("请输入用户名和密码");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await login(trimmedUsername, password);
      await refresh();
    } catch (err) {
      setError(describeLoginError(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="login-shell">
      <form className="login-card card" onSubmit={submit} aria-busy={submitting || undefined}>
        <div className="login-brand">
          <span className="login-seal" aria-hidden="true">漫</span>
          <div className="login-brand-copy">
            <b>漫剧案头</b>
            <span>请登录后继续</span>
          </div>
        </div>
        <div className="login-field">
          <label className="f" htmlFor={usernameId}>用户名</label>
          <input
            id={usernameId}
            type="text"
            autoComplete="username"
            autoFocus
            disabled={submitting}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </div>
        <div className="login-field">
          <label className="f" htmlFor={passwordId}>密码</label>
          <input
            id={passwordId}
            type="password"
            autoComplete="current-password"
            disabled={submitting}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </div>
        {error && <p className="field-error" role="alert">{error}</p>}
        <button type="submit" className="btn primary login-submit" disabled={submitting}>
          {submitting ? "登录中…" : "登录"}
        </button>
      </form>
    </div>
  );
}

function describeLoginError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "凭证错误：用户名或密码不正确";
    if (err.status === 429) return "尝试过多，请稍后再试";
    if (err.status === 0) return err.message || "无法连接后端服务，请稍后重试";
    return err.message || "登录失败，请重试";
  }
  return "登录失败，请检查网络后重试";
}
