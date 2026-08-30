import { useEffect, useId, useState } from "react";
import {
  api, ApiError, deleteMyAccount, me as fetchMe,
  type DeletedUserRow, type UserRow, type UserTier,
} from "../api";
import {
  CreateAccountDialog, ResetPasswordDialog, SelfDeleteDialog, SoftDeleteConfirmDialog,
  type NewAccountDraft,
} from "../components/AccountAdminDialogs";
import { TIERS, TIER_HINTS, TIER_LABELS } from "../lib/tier";
import { AccountCard, DeletedAccountCard, formatTime } from "../components/AccountCard";
import "../styles/AccountAdminPage.css";

/** 账号管理——系统管理员专属，移动端优先：每个账号一张卡片，操作按钮直接铺
 *  在卡片里，不藏进横向滚动或「⋯」菜单。两类删除严格区分（CLAUDE.md「危险
 *  操作分级」）：管理员删他人账号是软删，30 天回收站可恢复，
 *  经一次确认弹窗执行（不要求打用户名——它可逆）；账号自删
 *  （仅对自己生效）立即级联清空全部项目且不可恢复，是本页唯一需要真正强确认
 *  （打对用户名）的操作，见 SelfDeleteDialog。 */

export default function AccountAdminPage() {
  const [users, setUsers] = useState<UserRow[] | null>(null);
  const [usersError, setUsersError] = useState<string | null>(null);
  const [deletedUsers, setDeletedUsers] = useState<DeletedUserRow[] | null>(null);
  const [deletedError, setDeletedError] = useState<string | null>(null);
  const [myId, setMyId] = useState<string | null>(null);
  const [myUsername, setMyUsername] = useState<string | null>(null);

  const [tab, setTab] = useState<"active" | "recycle">("active");
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ text: string; err: boolean } | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<UserRow | null>(null);
  // 管理员删他人账号会级联该账号名下全部项目，爆炸半径比删单个项目大一个量级，
  // 且卡片布局下按钮是换行排布的、窄屏误触概率更高——必须经确认。可逆（30 天
  // 回收站）所以不用像自删那样要求打对用户名，一次确认即可（CLAUDE.md 风险分级）。
  const [softDeleteTarget, setSoftDeleteTarget] = useState<UserRow | null>(null);
  type FailedNotice = { title: string; items: { project_id: string; error: string }[] } | null;
  const [failedNotice, setFailedNotice] = useState<FailedNotice>(null);
  const [selfDeleteBusy, setSelfDeleteBusy] = useState(false);
  const [selfDeleteInfo, setSelfDeleteInfo] = useState<{ message: string; projectCount: number } | null>(null);

  const loadUsers = async () => {
    setUsersError(null);
    try {
      setUsers((await api.listUsers()).items);
    } catch (err) {
      setUsersError(err instanceof Error ? err.message : String(err));
    }
  };
  const loadDeleted = async () => {
    setDeletedError(null);
    try {
      setDeletedUsers((await api.listDeletedUsers()).items);
    } catch (err) {
      setDeletedError(err instanceof Error ? err.message : String(err));
    }
  };

  useEffect(() => {
    void loadUsers();
    void loadDeleted();
    void fetchMe().then((r) => { setMyId(r.user.id); setMyUsername(r.user.username); }).catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const notify = (text: string, isErr = false) => {
    setToast({ text, err: isErr });
    window.setTimeout(() => setToast(null), isErr ? 6000 : 2600);
  };

  /** 忙碌态/异常处理共用一套：doneMsg 给固定文案的动作用；不传时调用方自己在
   *  fn 里按接口返回值拼 toast（加量包价格、删除失败清单都要读返回值）。
   *  返回是否成功：创建/重置密码弹窗靠它决定「关掉」还是「留在原地让用户改」。 */
  const runAction = async (fn: () => Promise<unknown>, doneMsg?: string) => {
    setBusy(true);
    try {
      await fn();
      if (doneMsg) notify(doneMsg);
      await Promise.all([loadUsers(), loadDeleted()]);
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
        username: draft.username, password: draft.password, display_name: draft.displayName || undefined,
        is_system_admin: draft.isSystemAdmin, tier: draft.tier, must_change_password: draft.mustChangePassword,
      }),
      `账号「${draft.username}」已创建`,
    );
    if (ok) setCreateOpen(false);
  };

  const saveDisplayName = (u: UserRow, name: string) =>
    void runAction(() => api.updateUser(u.id, { display_name: name }), `「${u.username}」的显示名已改为「${name}」`);

  const toggleStatus = (u: UserRow) => {
    const next = u.status === "active" ? "disabled" : "active";
    void runAction(() => api.updateUser(u.id, { status: next }), `账号「${u.username}」已${next === "active" ? "启用" : "禁用"}`);
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
    void runAction(() => api.updateUser(u.id, { tier }), `「${u.username}」的档位已改为 ${TIER_LABELS[tier]}`);
  };

  const resetQuota = (u: UserRow) =>
    void runAction(() => api.updateUser(u.id, { reset_quota_period: true }), `「${u.username}」的配额周期已重置`);

  const resetPassword = async (password: string, mustChangePassword: boolean) => {
    if (!resetTarget) return;
    const ok = await runAction(
      () => api.updateUser(resetTarget.id, { password, must_change_password: mustChangePassword }),
      `「${resetTarget.username}」的密码已重置`,
    );
    if (ok) setResetTarget(null);
  };

  const grantAddon = (u: UserRow, packages: number) => void runAction(async () => {
    const result = await api.grantVideoAddon(u.id, packages);
    const balanceMin = Math.round(result.addon_balance_s / 60);
    const packageMin = Math.round(result.package_seconds * result.packages / 60);
    notify(`已为「${u.username}」发放 ${result.packages} 包加量包（¥${result.price_cny} · 共 ${packageMin} 分钟），当前加量余额约 ${balanceMin} 分钟`);
  });

  const softDeleteUser = (u: UserRow) => void runAction(async () => {
    const result = await api.deleteUser(u.id);
    notify(`账号「${u.username}」已移入回收站，30 天内可恢复`);
    if (result.projects.failed.length) {
      setFailedNotice({ title: `账号「${u.username}」名下以下项目移入回收站失败，需要人工核对`, items: result.projects.failed });
    }
  });

  const restoreUser = (u: DeletedUserRow) => void runAction(async () => {
    const result = await api.restoreUser(u.id);
    notify(`账号「${u.username}」已恢复`);
    if (result.projects.failed.length) {
      setFailedNotice({ title: `账号「${u.username}」名下以下项目恢复失败，需要人工核对`, items: result.projects.failed });
    }
  });

  const openSelfDelete = async () => {
    setSelfDeleteBusy(true);
    try {
      await deleteMyAccount(false);
      notify("预检异常：未收到确认信息，请刷新后重试", true);
    } catch (err) {
      const detail = err instanceof ApiError
        ? (err.detail as { code?: string; message?: string; project_count?: number } | undefined)
        : undefined;
      if (err instanceof ApiError && err.status === 422 && detail?.code === "confirmation_required") {
        setSelfDeleteInfo({ message: detail.message ?? err.message, projectCount: detail.project_count ?? 0 });
      } else {
        notify(err instanceof ApiError ? err.message : String(err), true);
      }
    } finally {
      setSelfDeleteBusy(false);
    }
  };

  const confirmSelfDelete = async () => {
    setSelfDeleteBusy(true);
    try {
      await deleteMyAccount(true);
      setSelfDeleteInfo(null);
      notify("账号已彻底删除");
      // 会话已随账号级联删除失效，走整页刷新回到登录页——不复用应用壳里
      // AuthContext 的 logout()，避免本页额外依赖那层 Provider。
      window.setTimeout(() => window.location.reload(), 600);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        notify(`${err.message}；请到「观测台」核对供应商任务状态后重试`, true);
      } else {
        notify(err instanceof ApiError ? err.message : String(err), true);
      }
    } finally {
      setSelfDeleteBusy(false);
    }
  };

  const deletedCount = deletedUsers?.length ?? 0;

  return (
    <div className="account-admin">
      <header className="desk-head">
        <h1>账号管理</h1>
        <p className="sub">开户、启停账号、改档位、删除与恢复——只有系统管理员能看到这一页。</p>
        <hr className="rule" />
      </header>

      {failedNotice && (
        <div className="empty query-error account-admin-fail-notice" role="alert">
          <strong>{failedNotice.title}</strong>
          <ul>
            {failedNotice.items.map((f) => <li key={f.project_id}>项目 {f.project_id}：{f.error}</li>)}
          </ul>
          <button type="button" className="btn small" onClick={() => setFailedNotice(null)}>知道了</button>
        </div>
      )}

      <div className="account-admin-bar">
        <div className="account-admin-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={tab === "active"}
            className={`btn small ${tab === "active" ? "primary" : "ghost"}`} onClick={() => setTab("active")}>
            账号{users ? ` · ${users.length}` : ""}
          </button>
          <button type="button" role="tab" aria-selected={tab === "recycle"}
            className={`btn small ${tab === "recycle" ? "primary" : "ghost"}`} onClick={() => setTab("recycle")}>
            回收站{deletedCount > 0 ? ` · ${deletedCount}` : ""}
          </button>
        </div>
        <button type="button" className="btn primary" disabled={busy} onClick={() => setCreateOpen(true)}>创建账号</button>
      </div>

      {tab === "active" ? (
        <>
          {usersError && (
            <div className="empty query-error" role="alert">
              <strong>加载失败</strong>
              <p>{usersError}</p>
              <button type="button" className="btn" onClick={() => void loadUsers()}>重试</button>
            </div>
          )}
          <div className="account-admin-cards">
            {(users ?? []).map((u) => (
              <AccountCard key={u.id} user={u} isSelf={u.id === myId} busy={busy}
                onSaveDisplayName={saveDisplayName} onChangeTier={changeTier} onToggleAdmin={toggleAdmin}
                onResetPassword={setResetTarget} onResetQuota={resetQuota} onToggleStatus={toggleStatus}
                onSoftDelete={setSoftDeleteTarget} onSelfDeleteOpen={() => void openSelfDelete()} onGrantAddon={grantAddon} />
            ))}
          </div>
          {!users && !usersError && <p className="account-admin-muted">载入中…</p>}
          {users && !users.length && <p className="account-admin-muted">还没有账号，先「创建账号」。</p>}
        </>
      ) : (
        <>
          {deletedError && (
            <div className="empty query-error" role="alert">
              <strong>加载失败</strong>
              <p>{deletedError}</p>
              <button type="button" className="btn" onClick={() => void loadDeleted()}>重试</button>
            </div>
          )}
          <div className="account-admin-cards">
            {(deletedUsers ?? []).map((u) => (
              <DeletedAccountCard key={u.id} user={u} busy={busy} onRestore={restoreUser} />
            ))}
          </div>
          {!deletedUsers && !deletedError && <p className="account-admin-muted">载入中…</p>}
          {deletedUsers && !deletedUsers.length && <p className="account-admin-muted">回收站是空的。</p>}
        </>
      )}

      <p className="account-admin-tier-hint">
        {TIERS.map((t) => `${TIER_LABELS[t]}：${TIER_HINTS[t]}`).join("　·　")}　·　系统管理员不受以上任何档位限制。
      </p>

      {createOpen && <CreateAccountDialog busy={busy} onClose={() => setCreateOpen(false)} onSubmit={createAccount} />}
      {softDeleteTarget && (
        <SoftDeleteConfirmDialog
          username={softDeleteTarget.username} busy={busy}
          onCancel={() => setSoftDeleteTarget(null)}
          onConfirm={() => { const u = softDeleteTarget; setSoftDeleteTarget(null); softDeleteUser(u); }}
        />
      )}
      {resetTarget && (
        <ResetPasswordDialog user={resetTarget} busy={busy} onClose={() => setResetTarget(null)} onSubmit={resetPassword} />
      )}
      {selfDeleteInfo && myUsername && (
        <SelfDeleteDialog
          username={myUsername} message={selfDeleteInfo.message} projectCount={selfDeleteInfo.projectCount}
          busy={selfDeleteBusy} onClose={() => setSelfDeleteInfo(null)} onConfirm={() => void confirmSelfDelete()}
        />
      )}

      {toast && <div role="status" className={`toast ${toast.err ? "err" : ""}`}>{toast.text}</div>}
    </div>
  );
}
