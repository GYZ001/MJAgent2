import { useId, useRef, useState } from "react";
import {
  api, ApiError, type ImportApplyResult, type ImportPreviewResult, type ImportRowResult,
} from "../api";
import { useFocusTrap } from "../hooks/useFocusTrap";

/** CSV 批量导入弹窗（EP-03 §4）：上传 -> 预检报告（四类逐行结果，不写库）
 *  -> 确认提交 -> 下载报告。随机初始口令只在「确认提交」的结果与下载的报告
 *  CSV 里各出现一次，界面上必须原样透传、不做二次脱敏（脱敏已经在后端做，
 *  这里显示的就是唯一能看到明文的地方）。 */

const ACTION_LABELS: Record<ImportRowResult["action"], string> = {
  create: "新建", update: "更新", skip: "跳过（无变化）", error: "错误",
};

function CountsBar({ counts }: { counts: { create: number; update: number; skip: number; error: number } }) {
  return (
    <p className="account-admin-tier-hint">
      新建 {counts.create}　·　更新 {counts.update}　·　跳过 {counts.skip}　·　错误 {counts.error}
    </p>
  );
}

function RowsTable({ rows }: { rows: ImportRowResult[] }) {
  return (
    <div className="import-dialog-table-wrap">
      <table className="import-dialog-table">
        <thead>
          <tr>
            <th>行号</th><th>用户名</th><th>结果</th><th>初始口令</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.line_no} className={r.action === "error" ? "import-row-error" : undefined}>
              <td>{r.line_no}</td>
              <td>{r.username || <em>（空）</em>}</td>
              <td>{ACTION_LABELS[r.action]}</td>
              <td>{r.initial_password ?? ""}</td>
              <td>{r.action === "error" ? `${r.column ? `[${r.column}] ` : ""}${r.reason ?? ""}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const titleId = useId();
  const trapRef = useFocusTrap(true, onClose);
  const fileRef = useRef<HTMLInputElement>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreviewResult | null>(null);
  const [applied, setApplied] = useState<ImportApplyResult | null>(null);

  const pickAndPreview = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) { setError("请先选择一个 CSV 文件"); return; }
    setBusy(true);
    setError(null);
    try {
      setApplied(null);
      setPreview(await api.importPreview(file));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const confirmApply = async () => {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      setApplied(await api.importApply(preview.batch_id));
      onImported();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const downloadReport = async () => {
    if (!applied) return;
    const blob = await api.importReportBlob(applied.batch_id);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `import-report-${applied.batch_id}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const result = applied ?? preview;

  return (
    <div className="evidence-backdrop" role="presentation"
      onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
      <section ref={trapRef} className="impact-dialog decision-dialog import-dialog" role="dialog"
        aria-modal="true" aria-labelledby={titleId}>
        <h3 id={titleId}>CSV 批量导入</h3>
        <p className="account-admin-muted">
          列：username（必填，幂等键），display_name / email / employee_no / team / role /
          tier_or_quota_plan（均可选，留空表示不改）。表头必需，顺序无关。支持 UTF-8 / GBK 编码；
          单批最多 1000 行。
        </p>

        {!applied && (
          <div className="login-field">
            <input ref={fileRef} type="file" accept=".csv,text/csv" disabled={busy} />
          </div>
        )}

        {error && <p className="field-error" role="alert">{error}</p>}

        {result && (
          <>
            <CountsBar counts={result.counts} />
            <RowsTable rows={result.rows} />
          </>
        )}

        {applied && (
          <p className="account-admin-tier-hint">
            上面「初始口令」列只在本次与紧接着的第一次报告下载里可见，请当场记录或下载报告。
          </p>
        )}

        <div className="dialog-actions">
          <button type="button" className="btn" onClick={onClose} disabled={busy}>关闭</button>
          {!preview && (
            <button type="button" className="btn primary" disabled={busy} onClick={() => void pickAndPreview()}>
              {busy ? "预检中…" : "上传并预检"}
            </button>
          )}
          {preview && !applied && (
            <>
              <button type="button" className="btn" disabled={busy} onClick={() => setPreview(null)}>
                重新选择文件
              </button>
              <button type="button" className="btn primary" disabled={busy} onClick={() => void confirmApply()}>
                {busy ? "提交中…" : `确认导入（新建 ${preview.counts.create} · 更新 ${preview.counts.update}）`}
              </button>
            </>
          )}
          {applied && (
            <button type="button" className="btn primary" onClick={() => void downloadReport()}>
              下载报告 CSV
            </button>
          )}
        </div>
      </section>
    </div>
  );
}
