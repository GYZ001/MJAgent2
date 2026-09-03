import { Fragment, useState } from "react";
import { api, type AuditEvent, type AuditEventDetail } from "../../api";
import { fmtTime } from "../monitor/shared";
import { identityLabel, outcomeLabel, sourceLabel } from "./auditLabels";
import { AuditRowDetail } from "./AuditRowDetail";

function outcomeClass(outcome: string): string {
  if (outcome === "ok") return "green";
  if (outcome === "failed" || outcome === "error") return "red";
  return "gold";
}

/** 事件列表表格，行内「+」展开详情（懒加载，展开时才补拉 args）。cursor 分页
 *  的「加载更多」由 OperationAuditPage 负责，本组件只渲染当前已加载的 items。 */
export function AuditTable({ items }: { items: AuditEvent[] }) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [details, setDetails] = useState<Record<string, AuditEventDetail | "loading" | "error">>({});

  const toggle = (item: AuditEvent) => {
    if (expandedId === item.id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(item.id);
    if (!details[item.id]) {
      setDetails((prev) => ({ ...prev, [item.id]: "loading" }));
      api.getAuditEvent(item.id)
        .then((detail) => setDetails((prev) => ({ ...prev, [item.id]: detail })))
        .catch(() => setDetails((prev) => ({ ...prev, [item.id]: "error" })));
    }
  };

  return (
    <div className="audit-table-wrap">
      <table className="ledger audit-ledger">
        <thead>
          <tr>
            <th aria-hidden="true" />
            <th>事件</th>
            <th>结果</th>
            <th>用户名</th>
            <th>身份</th>
            <th>事件源</th>
            <th>操作对象</th>
            <th>所属空间</th>
            <th>事件时间</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const expanded = expandedId === item.id;
            return (
              <Fragment key={item.id}>
                <tr>
                  <td>
                    <button
                      type="button"
                      className="audit-expand-toggle"
                      aria-expanded={expanded}
                      aria-label={expanded ? `收起${item.event_label || item.event}的详情` : `展开${item.event_label || item.event}的详情`}
                      onClick={() => toggle(item)}
                    >
                      {expanded ? "−" : "+"}
                    </button>
                  </td>
                  <td>
                    <b>{item.event_label || item.event}</b>
                    {item.event_label && <div className="audit-event-code mono">{item.event}</div>}
                  </td>
                  <td><span className={`stamp ${outcomeClass(item.outcome)}`}>{outcomeLabel(item.outcome)}</span></td>
                  <td>{item.username || "未登录"}</td>
                  <td>{identityLabel(item.is_system_admin)}</td>
                  <td>{sourceLabel(item.source)}</td>
                  <td>{item.target || "—"}</td>
                  <td>{item.project_name || item.project_id || "—"}</td>
                  <td className="mono">{fmtTime(item.ts)}</td>
                </tr>
                {expanded && (
                  <tr className="audit-detail-row">
                    <td />
                    <td colSpan={8}>
                      <AuditRowDetail item={item} detail={details[item.id]} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
