import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type { Call, CallDetail } from "../../api";
import JsonViewer from "../../components/JsonViewer";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import { callStatusLabel, fmtTime, track } from "./shared";

export default function CallDrawer({
  call,
  projectId,
  onClose,
}: {
  call: Call | CallDetail;
  projectId?: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<CallDetail | null>(
    "request_json_size" in call ? call : null,
  );
  const [tab, setTab] = useState<"input" | "output">("input");
  const [error, setError] = useState("");
  const drawerRef = useFocusTrap(true, onClose);
  const load = useCallback(async () => {
    setError("");
    try {
      const next = (await api.getCallDetail(call.id, projectId)) as CallDetail;
      setDetail(next);
      track(
        "call_detail",
        {
          size_bucket:
            next.request_json_size + next.response_json_size > 100000
              ? "large"
              : "normal",
        },
        String(call.id),
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }, [call.id, projectId]);
  useEffect(() => {
    if ("request_json_size" in call) setDetail(call);
    else void load();
  }, [call, load]);
  const outputRaw = detail?.response_json || (
    detail?.error ? JSON.stringify({ error: detail.error }, null, 2) : undefined
  );
  return (
    <div
      className="monitor-drawer-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.currentTarget === e.target) onClose();
      }}
    >
      <aside
        className="monitor-drawer call-drawer call-io-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="call-title"
        ref={(node) => {
          drawerRef.current = node;
        }}
      >
        <header>
          <div>
            <span className="eyebrow">单次模型调用</span>
            <h3 id="call-title">
              {call.model_label || call.model || "未记录模型"}
            </h3>
          </div>
          <button onClick={onClose} aria-label="关闭调用详情">
            ×
          </button>
        </header>
        <div className="call-io-summary">
          <div>
            <span>状态</span>
            <b>{callStatusLabel(call.effective_status)}</b>
          </div>
          <div>
            <span>耗时</span>
            <b>{(call.latency_ms / 1000).toFixed(1)} 秒</b>
          </div>
          <div><span>开始时间</span><b>{fmtTime(call.ts)}</b></div>
          <div>
            <span>结束时间</span>
            <b>{fmtTime(call.ts + call.latency_ms / 1000)}</b>
          </div>
        </div>
        {error && (
          <div className="monitor-state error" role="alert">
            <b>调用详情加载失败</b>
            <span>当前列表摘要仍保留，发送内容和返回内容尚不可查看。</span>
            <details className="monitor-error-details">
              <summary>查看错误详情</summary>
              <pre>{error}</pre>
            </details>
            <button onClick={load}>重试</button>
          </div>
        )}
        {!detail && !error && (
          <div className="monitor-loading">正在加载模型输入输出…</div>
        )}
        {detail && (
          <div className="call-io-workspace">
            <nav className="call-io-tabs" aria-label="模型调用数据">
              <button
                type="button"
                className={tab === "input" ? "active" : ""}
                aria-current={tab === "input" ? "page" : undefined}
                onClick={() => setTab("input")}
              >
                输入
              </button>
              <button
                type="button"
                className={tab === "output" ? "active" : ""}
                aria-current={tab === "output" ? "page" : undefined}
                onClick={() => setTab("output")}
              >
                输出
              </button>
            </nav>
            <div className="call-io-json">
              {(tab === "input" ? detail.request_json : outputRaw) ? (
                <JsonViewer
                  raw={tab === "input" ? detail.request_json : outputRaw}
                  collapsed={false}
                  maxHeight="calc(100vh - 280px)"
                />
              ) : (
                <div className="empty">本次调用没有记录{tab === "input" ? "输入" : "输出"}</div>
              )}
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}
