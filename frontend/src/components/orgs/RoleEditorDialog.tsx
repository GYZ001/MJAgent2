import { useEffect, useState } from "react";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import type { PermissionPoint, RoleRow } from "../../api";

/** 新建/编辑自定义角色的权限点勾选弹窗。内置模板（`role.builtin`）不传入
 *  `role` 走"新建"分支；传入非内置角色走"编辑权限点"分支——两种模式共用
 *  同一份勾选 UI，差异只在头部字段是否可编辑。 */
export default function RoleEditorDialog({
  role, catalog, templates, busy, onClose, onSubmit,
}: {
  role: RoleRow | null;
  catalog: PermissionPoint[];
  templates: RoleRow[];
  busy: boolean;
  onClose: () => void;
  onSubmit: (draft: { key: string; name: string; description: string; fromTemplate: string; permissionKeys: string[] }) => void;
}) {
  const trapRef = useFocusTrap(true, onClose);
  const readOnly = Boolean(role?.builtin);
  const [key, setKey] = useState(role?.key ?? "");
  const [name, setName] = useState(role?.name ?? "");
  const [description, setDescription] = useState(role?.description ?? "");
  const [fromTemplate, setFromTemplate] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set(role?.permission_keys ?? []));

  useEffect(() => {
    if (!fromTemplate) return;
    const template = templates.find((t) => t.key === fromTemplate);
    if (template) setSelected(new Set(template.permission_keys));
  }, [fromTemplate, templates]);

  const toggle = (permKey: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(permKey)) next.delete(permKey); else next.add(permKey);
      return next;
    });
  };

  const submit = () => {
    onSubmit({ key, name, description, fromTemplate, permissionKeys: [...selected] });
  };

  return (
    <div className="evidence-backdrop" role="presentation" onMouseDown={(e) => {
      if (e.currentTarget === e.target && !busy) onClose();
    }}>
      <section ref={trapRef} className="impact-dialog role-editor-dialog" role="dialog" aria-modal="true"
        aria-labelledby="role-editor-title">
        <h3 id="role-editor-title">
          {role ? `${readOnly ? "查看" : "编辑"}角色权限点：${role.name}` : "新建角色"}
        </h3>
        {readOnly && <p className="sub">内置角色模板不可修改权限点，仅供查看。</p>}

        {!role && (
          <div className="role-editor-fields">
            <input placeholder="角色 key（英文，如 editor）" value={key} disabled={busy}
              onChange={(e) => setKey(e.target.value)} />
            <input placeholder="角色名称" value={name} disabled={busy} onChange={(e) => setName(e.target.value)} />
            <input placeholder="说明（可选）" value={description} disabled={busy}
              onChange={(e) => setDescription(e.target.value)} />
            <select aria-label="从模板复制" value={fromTemplate} disabled={busy}
              onChange={(e) => setFromTemplate(e.target.value)}>
              <option value="">不使用模板，从空白开始</option>
              {templates.map((t) => <option key={t.key} value={t.key}>从「{t.name}」复制</option>)}
            </select>
          </div>
        )}

        <div className="role-permission-list">
          {catalog.map((point) => (
            <label key={point.key} className="role-permission-item">
              <input type="checkbox" checked={selected.has(point.key)} disabled={busy || readOnly}
                onChange={() => toggle(point.key)} />
              <span className="role-permission-title">{point.title}</span>
              <span className="stamp grey">{point.risk}</span>
            </label>
          ))}
        </div>

        <div className="dialog-actions">
          <button className="btn" type="button" disabled={busy} onClick={onClose}>
            {readOnly ? "关闭" : "取消"}
          </button>
          {!readOnly && (
            <button className="btn primary" type="button" disabled={busy} onClick={submit}>
              {busy ? "提交中…" : role ? "保存权限点" : "创建角色"}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
