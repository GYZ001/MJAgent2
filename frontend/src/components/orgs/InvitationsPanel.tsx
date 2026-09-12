import { useEffect, useState } from "react";
import {
  ApiError, createInvitation, listInvitations, listRoles, listTeams, revokeInvitation,
  type CreatedInvitation, type InvitationRow, type RoleRow, type TeamRow,
} from "../../api";

const STATUS_LABELS: Record<InvitationRow["status"], string> = {
  pending: "待接受", accepted: "已接受", revoked: "已撤销", expired: "已过期",
};

/** 邀请链接管理：生成一次性邀请、复制链接、撤销、查看状态（EP-03 第二阶段）。
 *  明文 token 只在签发响应里出现一次——刷新页面/重新拉列表后再也看不到，
 *  这里用 `justCreated` 单独持有那一份，用完（关闭提示）即丢弃。 */
export default function InvitationsPanel() {
  const [items, setItems] = useState<InvitationRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [teams, setTeams] = useState<TeamRow[]>([]);
  const [roles, setRoles] = useState<RoleRow[]>([]);
  const [form, setForm] = useState({ username: "", displayName: "", email: "", teamId: "", roleId: "" });
  const [busy, setBusy] = useState(false);
  const [justCreated, setJustCreated] = useState<CreatedInvitation | null>(null);
  const [copyHint, setCopyHint] = useState<string | null>(null);

  const reload = () => {
    listInvitations()
      .then((data) => setItems(data.items))
      .catch((err) => setError(err instanceof ApiError ? err.message : "加载失败"));
  };
  useEffect(() => {
    reload();
    listTeams().then((d) => setTeams(d.items)).catch(() => undefined);
    listRoles().then((d) => setRoles(d.items)).catch(() => undefined);
  }, []);

  const inviteUrl = (token: string) => `${window.location.origin}/invite/${token}`;

  const copyLink = async (token: string) => {
    try {
      await navigator.clipboard.writeText(inviteUrl(token));
      setCopyHint("链接已复制");
    } catch {
      setCopyHint(inviteUrl(token)); // 剪贴板不可用时把链接直接显示出来，手动复制
    }
    window.setTimeout(() => setCopyHint(null), 4000);
  };

  const submit = async () => {
    if (!form.username.trim()) {
      setError("username 不能为空");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createInvitation({
        username: form.username.trim(),
        display_name: form.displayName.trim() || undefined,
        email: form.email.trim() || undefined,
        team_id: form.teamId || undefined,
        role_id: form.roleId || undefined,
      });
      setJustCreated(created);
      setForm({ username: "", displayName: "", email: "", teamId: "", roleId: "" });
      reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "签发失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (item: InvitationRow) => {
    if (!window.confirm(`确认撤销「${item.username}」的邀请？`)) return;
    setBusy(true);
    setError(null);
    try {
      await revokeInvitation(item.id);
      reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "撤销失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card">
      <div className="card-heading-row">
        <h3>邀请链接</h3>
      </div>
      {error && <p className="field-error" role="alert">{error}</p>}
      {copyHint && <p className="hint" role="status">{copyHint}</p>}
      {justCreated && (
        <div className="empty" role="status">
          <p>邀请已生成，链接只显示这一次，请立即复制发给「{justCreated.username}」：</p>
          <code>{inviteUrl(justCreated.token)}</code>
          <div className="dialog-actions">
            <button type="button" className="btn small" onClick={() => void copyLink(justCreated.token)}>复制链接</button>
            <button type="button" className="btn small ghost" onClick={() => setJustCreated(null)}>知道了</button>
          </div>
        </div>
      )}

      <div className="login-field">
        <label className="f">用户名</label>
        <input value={form.username} disabled={busy}
          onChange={(e) => setForm({ ...form, username: e.target.value })} />
      </div>
      <div className="login-field">
        <label className="f">显示名（可选）</label>
        <input value={form.displayName} disabled={busy}
          onChange={(e) => setForm({ ...form, displayName: e.target.value })} />
      </div>
      <div className="login-field">
        <label className="f">邮箱（可选）</label>
        <input value={form.email} disabled={busy}
          onChange={(e) => setForm({ ...form, email: e.target.value })} />
      </div>
      <div className="login-field">
        <label className="f">团队 + 角色（可选，需同时选择）</label>
        <select value={form.teamId} disabled={busy} onChange={(e) => setForm({ ...form, teamId: e.target.value })}>
          <option value="">不预置团队</option>
          {teams.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </select>
        <select value={form.roleId} disabled={busy} onChange={(e) => setForm({ ...form, roleId: e.target.value })}>
          <option value="">不预置角色</option>
          {roles.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
        </select>
      </div>
      <button type="button" className="btn small primary" disabled={busy} onClick={() => void submit()}>生成邀请链接</button>

      <table className="data-table">
        <thead>
          <tr><th>用户名</th><th>团队/角色</th><th>状态</th><th>过期时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          {(items ?? []).map((item) => (
            <tr key={item.id}>
              <td>{item.username}</td>
              <td>{item.team_name ? `${item.team_name} / ${item.role_name}` : "—"}</td>
              <td>{STATUS_LABELS[item.status]}</td>
              <td>{new Date(item.expires_at * 1000).toLocaleString()}</td>
              <td>
                {item.status === "pending" && (
                  <button type="button" className="btn small danger" disabled={busy} onClick={() => void revoke(item)}>撤销</button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {items && items.length === 0 && <p className="hint">还没有邀请记录。</p>}
    </div>
  );
}
