import { useId, useState, type FormEvent } from "react";
import { ApiError, changePassword } from "../api";
import { useAuth } from "../auth/AuthContext";
import AuthAside from "../components/AuthAside";

/** 首次登录强制改密。
 *
 *  管理员开户时设的是一个初始密码，且这个密码经手了管理员——不换掉的话，
 *  「谁做了什么」的审计从第一天起就不可信。后端在 users.must_change_password
 *  上置位、改密成功后清零，这里是它唯一的强制点：置位期间应用壳完全不挂载，
 *  用户除了改密和登出没有别的路可走。
 *
 *  注意这**不是**安全边界，只是流程约束：真正的授权判定全在后端。绕过这一页
 *  直接打 API 是可能的，但那需要用户自己有意为之，而威胁模型里要防的是
 *  「初始密码被沿用」，不是「用户主动跳过自己的改密」。 */
export default function ForcePasswordChangePage() {
  const { user, refresh, logout } = useAuth();
  const oldId = useId();
  const nextId = useId();
  const confirmId = useId();
  const [oldPassword, setOldPassword] = useState("");
  const [nextPassword, setNextPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    if (nextPassword.length < 8) {
      setError("新密码至少 8 位");
      return;
    }
    if (nextPassword !== confirmPassword) {
      setError("两次输入的新密码不一致");
      return;
    }
    if (nextPassword === oldPassword) {
      setError("新密码不能与当前密码相同");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await changePassword(oldPassword, nextPassword);
      // 改密会吊销该账号其它会话并签发一枚新的；refresh 后
      // must_change_password 归零，应用壳随即挂载。
      await refresh();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? (err.status === 401 || err.status === 422 ? "当前密码不正确，或新密码不符合要求" : err.message)
          : "改密失败，请稍后重试",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-shell">
      <AuthAside />
      <main className="auth-main">
        <form className="auth-panel" onSubmit={submit} aria-busy={submitting || undefined}>
          <header>
            <h1>请先修改密码</h1>
            <p>
              {user?.display_name || user?.username} 当前用的是管理员设置的初始密码。
              换成只有你知道的密码后才能进入系统。
            </p>
          </header>

          <div className="login-field">
            <label className="f" htmlFor={oldId}>当前密码</label>
            <input id={oldId} type="password" autoComplete="current-password" value={oldPassword}
              autoFocus disabled={submitting} onChange={(e) => setOldPassword(e.target.value)} />
          </div>
          <div className="login-field">
            <label className="f" htmlFor={nextId}>新密码（至少 8 位）</label>
            <input id={nextId} type="password" autoComplete="new-password" value={nextPassword}
              disabled={submitting} onChange={(e) => setNextPassword(e.target.value)} />
          </div>
          <div className="login-field">
            <label className="f" htmlFor={confirmId}>确认新密码</label>
            <input id={confirmId} type="password" autoComplete="new-password" value={confirmPassword}
              disabled={submitting} onChange={(e) => setConfirmPassword(e.target.value)} />
          </div>

          {error && <p className="field-error" role="alert">{error}</p>}

          <button type="submit" className="btn primary auth-submit" disabled={submitting}>
            {submitting ? "提交中…" : "修改密码并进入"}
          </button>
          <button type="button" className="btn ghost auth-alt" disabled={submitting}
            onClick={() => void logout()}>
            换个账号登录
          </button>
        </form>
      </main>
    </div>
  );
}
