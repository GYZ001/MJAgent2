import type { AuditEvent, AuditEventDetail } from "../../api";
import { fmtTime } from "../monitor/shared";

/** 单行展开后的详情——列表接口不带 user_agent/args（避免每行都拖一份重载荷），
 *  这两项只在真正展开时才由 AuditTable 另发一次详情请求补上。 */
export function AuditRowDetail({
  item, detail,
}: {
  item: AuditEvent;
  detail: AuditEventDetail | "loading" | "error" | undefined;
}) {
  return (
    <div className="audit-row-detail">
      <dl className="audit-row-facts">
        <div><dt>请求</dt><dd>{item.method && item.path ? `${item.method} ${item.path}` : "—"}</dd></div>
        <div><dt>HTTP 状态</dt><dd>{item.http_status ?? "—"}</dd></div>
        <div><dt>耗时</dt><dd>{item.duration_ms != null ? `${item.duration_ms} ms` : "—"}</dd></div>
        <div><dt>IP</dt><dd>{item.ip || "—"}</dd></div>
        <div><dt>error_id</dt><dd>{item.error_id || "—"}</dd></div>
        <div><dt>error_code</dt><dd>{item.error_code || "—"}</dd></div>
        <div><dt>摘要</dt><dd>{item.summary || "—"}</dd></div>
      </dl>
      {detail === "loading" && <p role="status">正在加载详情…</p>}
      {detail === "error" && <p role="alert">详情加载失败</p>}
      {detail && detail !== "loading" && detail !== "error" && (
        <div className="audit-row-args">
          <p className="audit-row-meta">
            User-Agent：{detail.user_agent || "—"} · 事件时间：{fmtTime(item.ts)}
          </p>
          <pre>{JSON.stringify(detail.args, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}
