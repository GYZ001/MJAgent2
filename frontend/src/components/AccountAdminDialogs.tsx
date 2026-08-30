import { useId, useState, type FormEvent } from "react";
import type { UserRow, UserTier } from "../api";
import { useFocusTrap } from "../hooks/useFocusTrap";
import { TIERS, TIER_LABELS } from "../lib/tier";

/** 创建账号 + 重置密码两个表单弹窗，从 AccountAdminPage 拆出来控制单文件行数。
 *  两者都是「填数据再提交」，不是「确认危险动作」——用户已明确要求破坏性操作
 *  不加确认弹窗，这两个不在那个范畴内：没有实际输入内容，后端就没法执行。 */

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

/** 重置密码：改完对方当前登录会立即失效，需要用新密码重新登录。明文显示以便
 *  当面交接，与创建账号的初始密码同一取舍。 */
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
