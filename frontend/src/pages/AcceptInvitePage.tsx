import { useEffect, useState } from "react";
import { acceptInvitation, ApiError, previewInvitation, type InvitationPreview } from "../api";

const STATUS_MESSAGES: Record<Exclude<InvitationPreview["status"], "pending">, string> = {
  expired: "这个邀请链接已过期，请联系管理员重新生成。",
  revoked: "这个邀请链接已被撤销，请联系管理员重新生成。",
  accepted: "这个邀请链接已经被使用过了，如果不是你本人操作，请联系管理员。",
};

function tokenFromPath(): string {
  const parts = window.location.pathname.split("/").filter(Boolean);
  return parts[0] === "invite" ? decodeURIComponent(parts[1] ?? "") : "";
}

/** 邀请接受页（EP-03 第二阶段）：公开路由，不经过登录闸门（见 App.tsx 里
 *  对 `/invite/` 前缀的拦截）。设口令 → 建账号 → 直接登录，成功后整页刷新
 *  回工作台首页，让 AuthGate 重新读取内存里刚写入的会话令牌。 */
export default function AcceptInvitePage() {
  const [token] = useState(tokenFromPath);
  const [preview, setPreview] = useState<InvitationPreview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [violations, setViolations] = useState<string[]>([]);

  useEffect(() => {
    if (!token) {
      setLoadError("邀请链接无效：缺少凭证。");
      return;
    }
    previewInvitation(token)
      .then(setPreview)
      .catch((err) => setLoadError(err instanceof ApiError ? err.message : "邀请链接无效或已失效。"));
  }, [token]);

  const submit = async () => {
    if (password !== confirm) {
      setError("两次输入的口令不一致");
      return;
    }
    setBusy(true);
    setError(null);
    setViolations([]);
    try {
      await acceptInvitation(token, password);
      window.location.href = "/";
    } catch (err) {
      if (err instanceof ApiError) {
        const detail = err.detail as { violations?: string[] } | undefined;
        setError(err.message);
        setViolations(Array.isArray(detail?.violations) ? detail.violations : []);
      } else {
        setError("接受邀请失败，请稍后重试");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-loading">
      <div className="card" style={{ maxWidth: 420, margin: "10vh auto" }}>
        <h1>接受邀请</h1>
        {loadError && <p className="field-error" role="alert">{loadError}</p>}
        {!loadError && !preview && <p className="hint">正在校验邀请链接…</p>}
        {preview && preview.status !== "pending" && (
          <p className="field-error" role="alert">{STATUS_MESSAGES[preview.status]}</p>
        )}
        {preview && preview.status === "pending" && (
          <>
            <p className="hint">
              {preview.org_name ? `${preview.org_name} · ` : ""}
              邀请账号「{preview.username}」{preview.team_name ? `加入团队「${preview.team_name}」（角色：${preview.role_name}）` : ""}
            </p>
            <div className="login-field">
              <label className="f" htmlFor="invite-password">设置口令</label>
              <input id="invite-password" type="password" value={password} disabled={busy}
                onChange={(e) => setPassword(e.target.value)} />
            </div>
            <div className="login-field">
              <label className="f" htmlFor="invite-password-confirm">确认口令</label>
              <input id="invite-password-confirm" type="password" value={confirm} disabled={busy}
                onChange={(e) => setConfirm(e.target.value)} />
            </div>
            {error && <p className="field-error" role="alert">{error}</p>}
            {violations.length > 0 && (
              <ul className="field-error">
                {violations.map((v) => <li key={v}>{v}</li>)}
              </ul>
            )}
            <button type="button" className="btn primary" disabled={busy} onClick={() => void submit()}>
              设置口令并登录
            </button>
          </>
        )}
      </div>
    </div>
  );
}
