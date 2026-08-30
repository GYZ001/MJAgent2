import { useEffect, useState } from "react";
import { api, ApiError, type UserRow, type UserTier } from "../api";
import { CreateAccountDialog, ResetPasswordDialog, type NewAccountDraft } from "../components/AccountAdminDialogs";
import { TIERS, TIER_LABELS, TIER_HINTS } from "../lib/tier";
import "../styles/AccountAdminPage.css";

/** 账号管理——系统管理员专属，是「管理员开户、无自助注册」在产品里的唯一
 *  落地入口。上一轮把「团队/工作空间」模型退场时误删了承载这个功能的
 *  TeamAdminPage（那页其实一页两用：团队 + 账号），本页只找回账号那一半，
 *  团队/角色概念不再存在，不重建。
 *
 *  破坏性操作（停用、改管理员身份、重置密码/配额）用户已明确要求不加确认
 *  弹窗——点一下直接执行；后端两阶段批准令牌（见 api/client.ts 的
 *  approval_token 重放）已经兜底一次拦截，前端不用再叠一层。 */

function formatTime(epochSeconds: number | null): string {
  if (!epochSeconds) return "从未";
  return new Date(epochSeconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

export default function AccountAdminPage() {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ text: string; err: boolean } | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<UserRow | null>(null);

  const load = async () => {
    setError(null);
    try {
      const res = await api.listUsers();
      setUsers(res.items);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => { void load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const notify = (text: string, isErr = false) => {
    setToast({ text, err: isErr });
    window.setTimeout(() => setToast(null), isErr ? 6000 : 2600);
  };

  /** 返回是否成功：创建/重置密码弹窗靠它决定「关掉」还是「留在原地让用户改」。 */
  const runAction = async (fn: () => Promise<unknown>, doneMsg: string) => {
    setBusy(true);
    try {
      await fn();
      notify(doneMsg);
      await load();
      return true;
    } catch (err) {
      notify(err instanceof ApiError ? err.message : String(err), true);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const createAccount = async (draft: NewAccountDraft) => {
    const ok = await runAction(
      () => api.createUser({
        username: draft.username,
        password: draft.password,
        display_name: draft.displayName || undefined,
        is_system_admin: draft.isSystemAdmin,
        tier: draft.tier,
        must_change_password: draft.mustChangePassword,
      }),
      `账号「${draft.username}」已创建`,
    );
    if (ok) setCreateOpen(false);
  };

  const toggleStatus = (u: UserRow) => {
    const next = u.status === "active" ? "disabled" : "active";
    void runAction(
      () => api.updateUser(u.id, { status: next }),
      `账号「${u.username}」已${next === "active" ? "启用" : "禁用"}`,
    );
  };

  const toggleAdmin = (u: UserRow) => {
    const next = !u.is_system_admin;
    void runAction(
      () => api.updateUser(u.id, { is_system_admin: next }),
      next ? `「${u.username}」已设为系统管理员` : `已取消「${u.username}」的系统管理员身份`,
    );
  };

  const changeTier = (u: UserRow, tier: UserTier) => {
    if (tier === u.tier) return;
    void runAction(
      () => api.updateUser(u.id, { tier }),
      `「${u.username}」的档位已改为 ${TIER_LABELS[tier]}`,
    );
  };

  const resetQuota = (u: UserRow) => {
    void runAction(
      () => api.updateUser(u.id, { reset_quota_period: true }),
      `「${u.username}」的配额周期已重置`,
    );
  };

  const resetPassword = async (password: string, mustChangePassword: boolean) => {
    if (!resetTarget) return;
    const ok = await runAction(
      () => api.updateUser(resetTarget.id, { password, must_change_password: mustChangePassword }),
      `「${resetTarget.username}」的密码已重置`,
    );
    if (ok) setResetTarget(null);
  };

  const count = users?.length ?? 0;

  return (
    <div className="account-admin">
      <header className="desk-head">
        <h1>账号管理</h1>
        <p className="sub">开户、启停账号、改档位——只有系统管理员能看到这一页。</p>
        <hr className="rule" />
      </header>

      {error && (
        <div className="empty query-error" role="alert">
          <strong>加载失败</strong>
          <p>{error}</p>
          <button type="button" className="btn" onClick={() => void load()}>重试</button>
        </div>
      )}

      <div className="account-admin-bar">
        <span className="account-admin-bar-count">{users ? `共 ${count} 个账号` : ""}</span>
        <button type="button" className="btn primary" disabled={busy} onClick={() => setCreateOpen(true)}>
          创建账号
        </button>
      </div>

      <section className="card">
        <table className="account-admin-table">
          <thead>
            <tr>
              <th>账号</th><th>状态</th><th>系统管理员</th><th>档位</th><th>创建时间</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            {(users ?? []).map((u) => (
              <tr key={u.id} className={u.status === "disabled" ? "account-admin-row-disabled" : undefined}>
                <td>
                  <div className="account-admin-identity">
                    <b>{u.username}</b>
                    <span>{u.display_name || "—"}</span>
                  </div>
                </td>
                <td>
                  <span className={`account-admin-state ${u.status === "disabled" ? "disabled" : ""}`}>
                    {u.status === "active" ? "启用中" : "已禁用"}
                  </span>
                </td>
                <td>
                  {u.is_system_admin
                    ? <span className="account-admin-tag">系统管理员</span>
                    : "否"}
                </td>
                <td>
                  {u.is_system_admin ? (
                    <span className="account-admin-tier-unlimited" title="系统管理员不受档位限制">不限（管理员）</span>
                  ) : (
                    <div className="account-admin-tier">
                      <select value={u.tier} disabled={busy}
                        title={TIER_HINTS[u.tier]}
                        onChange={(event) => changeTier(u, event.target.value as UserTier)}>
                        {TIERS.map((t) => <option key={t} value={t}>{TIER_LABELS[t]}</option>)}
                      </select>
                    </div>
                  )}
                </td>
                <td className="account-admin-time">{formatTime(u.created_at)}</td>
                <td>
                  <div className="account-admin-actions">
                    <button type="button" className="btn small" disabled={busy}
                      onClick={() => setResetTarget(u)}>重置密码</button>
                    <button type="button" className="btn small ghost" disabled={busy}
                      onClick={() => toggleAdmin(u)}>
                      {u.is_system_admin ? "取消管理员" : "设为管理员"}
                    </button>
                    <button type="button" className="btn small" disabled={busy}
                      onClick={() => resetQuota(u)}>重置配额周期</button>
                    <button type="button" className={`btn small ${u.status === "active" ? "danger ghost" : ""}`}
                      disabled={busy} onClick={() => toggleStatus(u)}>
                      {u.status === "active" ? "禁用" : "启用"}
                    </button>
                  </div>
                </td>
              </tr>
            ))}
            {!users && !error && <tr><td colSpan={6} className="account-admin-muted">载入中…</td></tr>}
            {users && !users.length && (
              <tr><td colSpan={6} className="account-admin-muted">还没有账号，先「创建账号」。</td></tr>
            )}
          </tbody>
        </table>
        <p className="account-admin-tier-hint">
          {TIERS.map((t) => `${TIER_LABELS[t]}：${TIER_HINTS[t]}`).join("　·　")}
          　·　系统管理员不受以上任何档位限制。
        </p>
      </section>

      {createOpen && (
        <CreateAccountDialog busy={busy} onClose={() => setCreateOpen(false)} onSubmit={createAccount} />
      )}
      {resetTarget && (
        <ResetPasswordDialog
          user={resetTarget}
          busy={busy}
          onClose={() => setResetTarget(null)}
          onSubmit={resetPassword}
        />
      )}

      {toast && <div role="status" className={`toast ${toast.err ? "err" : ""}`}>{toast.text}</div>}
    </div>
  );
}
