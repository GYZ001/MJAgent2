import { useId, useState, type FormEvent } from "react";
import type { UserRow, UserTier } from "../api";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { TIERS, TIER_LABELS } from "../lib/tier";

/** 账号管理的三个弹窗，从 AccountAdminPage 拆出来控制单文件行数：创建账号、
 *  重置密码、自删账号确认。前两个是「填数据再提交」——用户已明确要求禁用/
 *  设管理员/重置配额这类破坏性操作不加确认弹窗，点一下直接执行，这两个不在
 *  那个范畴内：没有实际输入内容，后端就没法执行。管理员软删他人账号沿用同一
 *  条既有约定（30 天回收站本身就是保护机制，见 Studio.tsx 对项目删除的同款
 *  处理），也不在本文件弹窗之列。第三个 SelfDeleteDialog 是例外：自删不可
 *  恢复且立即级联清空，需要真正的强确认。 */

export interface NewAccountDraft {
  username: string;
  password: string;
  displayName: string;
  isSystemAdmin: boolean;
  tier: UserTier;
  mustChangePassword: boolean;
}

/** 初始密码由管理员定，用户首次登录会被强制改密（若勾选了「强制改密」），
 *  所以这里明文显示，便于当面交接。 */
export function CreateAccountDialog({
  busy,
  onClose,
  onSubmit,
}: {
  busy: boolean;
  onClose: () => void;
  onSubmit: (draft: NewAccountDraft) => void;
}) {
  const titleId = useId();
  const usernameId = useId();
  const displayNameId = useId();
  const passwordId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [draft, setDraft] = useState<NewAccountDraft>({
    username: "",
    password: "",
    displayName: "",
    isSystemAdmin: false,
    tier: "free",
    mustChangePassword: true,
  });
  const [error, setError] = useState<string | null>(null);
  const patch = (next: Partial<NewAccountDraft>) => {
    setDraft((current) => ({ ...current, ...next }));
    setError(null);
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const username = draft.username.trim();
    if (!username) {
      setError("用户名不能为空");
      return;
    }
    if (draft.password.length < 8) {
      setError("初始密码至少 8 位");
      return;
    }
    onSubmit({ ...draft, username, displayName: draft.displayName.trim() });
  };

  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <form onSubmit={submit}>
          <h3 id={titleId}>创建账号</h3>
          <div className="login-field">
            <label className="f" htmlFor={usernameId}>用户名</label>
            <input id={usernameId} value={draft.username} autoFocus disabled={busy}
              autoComplete="off" onChange={(event) => patch({ username: event.target.value })} />
          </div>
          <div className="login-field">
            <label className="f" htmlFor={displayNameId}>显示名（可选）</label>
            <input id={displayNameId} value={draft.displayName} disabled={busy}
              autoComplete="off" onChange={(event) => patch({ displayName: event.target.value })} />
          </div>
          <div className="login-field">
            <label className="f" htmlFor={passwordId}>初始密码（至少 8 位）</label>
            <input id={passwordId} type="text" value={draft.password} disabled={busy}
              autoComplete="off" onChange={(event) => patch({ password: event.target.value })} />
          </div>
          <div className="account-admin-dialog-row">
            <div className="login-field">
              <label className="f">档位</label>
              <select value={draft.tier} disabled={busy || draft.isSystemAdmin}
                onChange={(event) => patch({ tier: event.target.value as UserTier })}>
                {TIERS.map((t) => <option key={t} value={t}>{TIER_LABELS[t]}</option>)}
              </select>
            </div>
            <div className="login-field account-admin-checkbox-field">
              <label>
                <input type="checkbox" checked={draft.isSystemAdmin} disabled={busy}
                  onChange={(event) => patch({ isSystemAdmin: event.target.checked })} />
                {" "}设为系统管理员
              </label>
              <label>
                <input type="checkbox" checked={draft.mustChangePassword} disabled={busy}
                  onChange={(event) => patch({ mustChangePassword: event.target.checked })} />
                {" "}首次登录强制改密
              </label>
            </div>
          </div>
          {draft.isSystemAdmin && (
            <p className="account-admin-tier-hint">系统管理员不受档位限制，上面的档位设置对该账号不生效。</p>
          )}
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="btn primary" disabled={busy}>
              {busy ? "创建中…" : "创建账号"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

/** 自删账号确认：不可恢复、立即级联清空全部项目，没有回收站兜底
 *  （app/domain/account_deletion.py：自删是 fail-closed 全有全无）。比管理员
 *  软删（30 天可恢复，点一下直接执行）确认强得多——要求打对当前用户名才能
 *  点亮删除键，是本页里唯一一处「打字确认」的操作。 */
export function SelfDeleteDialog({
  username,
  message,
  projectCount,
  busy,
  onClose,
  onConfirm,
}: {
  username: string;
  message: string;
  projectCount: number;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const titleId = useId();
  const confirmId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [typed, setTyped] = useState("");
  const ready = typed.trim() === username;

  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section ref={trapRef} className="impact-dialog decision-dialog account-admin-danger-dialog"
        role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>彻底删除我的账号</h3>
        <p className="account-admin-danger-text">{message}</p>
        <p className="account-admin-danger-text">
          将立即彻底删除 <b>{projectCount}</b> 个项目的全部数据（数据库与磁盘产物），
          且无人可代为恢复——这与「移入回收站」不是同一件事。
        </p>
        <div className="login-field">
          <label className="f" htmlFor={confirmId}>输入用户名「{username}」以确认</label>
          <input id={confirmId} value={typed} autoFocus disabled={busy}
            autoComplete="off" onChange={(event) => setTyped(event.target.value)} />
        </div>
        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
          <button type="button" className="btn danger" disabled={busy || !ready} onClick={onConfirm}>
            {busy ? "删除中…" : "彻底删除，不可恢复"}
          </button>
        </div>
      </section>
    </div>
  );
}

/** 重置密码：改完对方当前登录会立即失效，需要用新密码重新登录。明文显示以便
 *  当面交接，与创建账号的初始密码同一取舍。 */
export function SoftDeleteConfirmDialog(
  { username, busy, onCancel, onConfirm }:
  { username: string; busy: boolean; onCancel: () => void; onConfirm: () => void },
) {
  return (
    <div className="dialog-backdrop" role="dialog" aria-modal="true" aria-label="确认删除账号">
      <div className="dialog">
        <h3>删除账号「{username}」？</h3>
        <p className="dialog-hint">
          该账号会被停用，名下<b>全部</b>项目一并移入回收站。30 天内可在「回收站」标签页恢复，
          到期后自动彻底清理。
        </p>
        <div className="dialog-actions">
          <button type="button" className="btn ghost" disabled={busy} onClick={onCancel}>取消</button>
          <button type="button" className="btn danger" disabled={busy} onClick={onConfirm}>
            移入回收站
          </button>
        </div>
      </div>
    </div>
  );
}

export function ResetPasswordDialog({
  user,
  busy,
  onClose,
  onSubmit,
}: {
  user: UserRow;
  busy: boolean;
  onClose: () => void;
  onSubmit: (password: string, mustChangePassword: boolean) => void;
}) {
  const titleId = useId();
  const passwordId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const [password, setPassword] = useState("");
  const [mustChange, setMustChange] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    if (password.length < 8) {
      setError("密码至少 8 位");
      return;
    }
    onSubmit(password, mustChange);
  };

  return (
    <div
      className="evidence-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <section ref={trapRef} className="impact-dialog decision-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <form onSubmit={submit}>
          <h3 id={titleId}>重置「{user.username}」的密码</h3>
          <p className="account-admin-muted">
            改完他当前的登录会立即失效，需要用新密码重新登录。明文显示以便当面交接。
          </p>
          <div className="login-field">
            <label className="f" htmlFor={passwordId}>新密码（至少 8 位）</label>
            <input id={passwordId} type="text" value={password} autoFocus disabled={busy}
              autoComplete="off"
              onChange={(event) => { setPassword(event.target.value); setError(null); }} />
          </div>
          <div className="login-field account-admin-checkbox-field">
            <label>
              <input type="checkbox" checked={mustChange} disabled={busy}
                onChange={(event) => setMustChange(event.target.checked)} />
              {" "}下次登录强制改密
            </label>
          </div>
          {error && <p className="field-error" role="alert">{error}</p>}
          <div className="dialog-actions">
            <button type="button" className="btn" onClick={onClose} disabled={busy}>取消</button>
            <button type="submit" className="btn danger" disabled={busy}>
              {busy ? "重置中…" : "重置密码"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
