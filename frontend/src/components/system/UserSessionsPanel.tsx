import { useEffect, useState } from "react";
import {
  ApiError, issueServiceSession, listUserSessions, listUsers, revokeUserSession,
  type IssuedServiceSession, type UserRow, type UserSessionRow,
} from "../../api";

const KIND_LABELS: Record<UserSessionRow["kind"], string> = {
  interactive: "交互式", service: "服务凭证",
};

/** 管理员查看某账号的活跃会话并强制下线（EP-03 第二阶段会话策略）。被踢账号
 *  的下一次请求会看到具体原因（"管理员已强制下线此会话"），不是莫名其妙被
 *  登出——见 app.auth.session_policy 模块文档。第二轮新增服务会话签发：供
 *  回归/驱动脚本等自动化使用，豁免空闲超时/并发上限，必须给一个有限的
 *  有效期天数（不支持无限期），列表里用「服务凭证」标签与交互式会话区分。 */
export default function UserSessionsPanel() {
  const [users, setUsers] = useState<UserRow[]>([]);
  const [userId, setUserId] = useState("");
  const [sessions, setSessions] = useState<UserSessionRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ttlDays, setTtlDays] = useState("30");
  const [issued, setIssued] = useState<IssuedServiceSession | null>(null);

  useEffect(() => {
    listUsers().then((d) => setUsers(d.items)).catch(() => undefined);
  }, []);

  const load = (id: string) => {
    if (!id) {
      setSessions(null);
      return;
    }
    setError(null);
    listUserSessions(id)
      .then((d) => setSessions(d.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : "加载失败"));
  };

  const onPickUser = (id: string) => {
    setUserId(id);
    setIssued(null);
    load(id);
  };

  const revoke = async (session: UserSessionRow) => {
    const noun = session.kind === "service" ? "这枚服务凭证" : "这个会话";
    if (!window.confirm(`确认强制下线${noun}？对方下一次操作会立即被登出。`)) return;
    setBusy(true);
    setError(null);
    try {
      await revokeUserSession(userId, session.id);
      load(userId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "下线失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const issue = async () => {
    const days = Number(ttlDays);
    if (!Number.isFinite(days) || days <= 0) {
      setError("有效期天数必须是正数");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await issueServiceSession(userId, days);
      setIssued(result);
      load(userId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "签发失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div className="card-heading-row"><h3>活跃会话</h3></div>
      {error && <p className="field-error" role="alert">{error}</p>}
      <div className="login-field">
        <label className="f">账号</label>
        <select value={userId} onChange={(e) => onPickUser(e.target.value)}>
          <option value="">选择一个账号…</option>
          {users.map((u) => <option key={u.id} value={u.id}>{u.display_name}（{u.username}）</option>)}
        </select>
      </div>

      {userId && (
        <div className="login-field">
          <label className="f">签发服务会话（供自动化脚本用，不支持无限期）</label>
          <input type="number" min={1} max={400} value={ttlDays} disabled={busy}
            onChange={(e) => setTtlDays(e.target.value)} style={{ maxWidth: 120 }} />
          <span className="hint">天</span>
          <button type="button" className="btn small primary" disabled={busy} onClick={() => void issue()}>
            签发
          </button>
        </div>
      )}
      {issued && (
        <div className="empty" role="status">
          <p>服务会话已签发，令牌只显示这一次，请立即复制（例如替换 data/regression_session_token.txt）：</p>
          <code>{issued.session_token}</code>
          <p className="hint">到期时间：{new Date(issued.expires_at * 1000).toLocaleString()}</p>
          <button type="button" className="btn small ghost" onClick={() => setIssued(null)}>知道了</button>
        </div>
      )}

      {userId && sessions === null && <p className="hint">正在加载…</p>}
      {userId && sessions?.length === 0 && <p className="hint">该账号当前没有活跃会话。</p>}
      {sessions && sessions.length > 0 && (
        <table className="data-table">
          <thead><tr><th>类型</th><th>登录时间</th><th>最近活跃</th><th>IP</th><th>设备</th><th>操作</th></tr></thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.id}>
                <td>{KIND_LABELS[s.kind]}</td>
                <td>{new Date(s.created_at * 1000).toLocaleString()}</td>
                <td>{new Date(s.last_seen_at * 1000).toLocaleString()}</td>
                <td>{s.ip ?? "—"}</td>
                <td className="hint">{s.user_agent ?? "—"}</td>
                <td><button type="button" className="btn small danger" disabled={busy} onClick={() => void revoke(s)}>强制下线</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
