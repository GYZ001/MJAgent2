import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import type { Job, VideoModeAudit } from "../../api";
import JsonViewer from "../../components/JsonViewer";
import { useFocusTrap } from "../../hooks/useFocusTrap";
import ZeroCostRelease from "./ZeroCostRelease";
import {
  PROVIDER_RESUBMISSION_WARNING,
  fmtTime,
  isProviderCreateUnresolved,
  jobNextStep,
  jobStatusLabel,
  jobWorkLabel,
  monitorVideoModeLabel,
  track,
} from "./shared";

export default function JobDrawer({
  job,
  projectId,
  onClose,
  onChanged,
  onJumpToRun,
}: {
  job: Job;
  projectId?: string;
  onClose: () => void;
  onChanged: () => void;
  onJumpToRun?: (runId: string) => void;
}) {
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [confirmAction, setConfirmAction] = useState<
    "" | "retry" | "resume" | "cancel" | "confirm_new_submission"
  >("");
  const [copied, setCopied] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const drawerRef = useFocusTrap(true, onClose);
  const load = useCallback(async () => {
    setError("");
    try {
      setDetail(await api.getJobDetail(job.id, job.source, projectId));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [job.id, job.source, projectId]);
  useEffect(() => {
    void load();
  }, [load]);
  const runAction = async (
    action: "retry" | "resume" | "cancel" | "confirm_new_submission",
  ) => {
    setBusy(action);
    setError("");
    setActionMessage("");
    const endpointAction = action === "confirm_new_submission" ? "resume" : action;
    const allowNewSubmission = action === "confirm_new_submission";
    let confirmationRequired = false;
    try {
      if (projectId) {
        await api.runProjectObservabilityJobAction(
          projectId,
          job.id,
          job.source,
          endpointAction,
          endpointAction === "cancel" ? undefined : {
            expected_version: job.state_revision ?? 0,
            allow_new_submission: allowNewSubmission,
          },
        );
      } else if (job.source === "run") {
        await api.runRunAction(
          job.run_id || job.id,
          endpointAction,
          endpointAction === "cancel" ? undefined : {
            allow_new_submission: allowNewSubmission,
          },
        );
      } else if (endpointAction === "cancel") {
        await api.cancelJob(job.id);
      } else {
        await api.retrySystemJob(job.id, {
          expected_version: job.state_revision ?? 0,
          allow_new_submission: allowNewSubmission,
        });
      }
      track("job_action", { action: endpointAction, object_status: job.status }, job.id);
      setActionMessage(
        `${jobWorkLabel(job)}：${endpointAction === "cancel" ? "取消请求已接受" : allowNewSubmission ? "已确认重新提交，正在重新校验授权" : endpointAction === "resume" ? "恢复请求已接受，正在从检查点继续" : "重试请求已接受，正在排队"}`,
      );
      onChanged();
      await load();
    } catch (e) {
      if (
        e instanceof ApiError
        && e.code === "PROVIDER_HANDLE_UNCONFIRMED"
        && !allowNewSubmission
      ) {
        confirmationRequired = true;
        setConfirmAction("confirm_new_submission");
        setError("");
        return;
      }
      setError((e as Error).message);
    } finally {
      setBusy("");
      if (!confirmationRequired) setConfirmAction("");
    }
  };
  const sourceUrl = job.project_id
    ? job.episode_id
      ? `/projects/${encodeURIComponent(job.project_id)}/episodes/${encodeURIComponent(job.episode_id)}/${(job.workflow_type || job.kind) === "storyboard" ? "board" : (job.workflow_type || job.kind) === "screenplay" ? "script" : "wall"}`
      : `/projects/${encodeURIComponent(job.project_id)}/bible`
    : "";
  const canRetry = job.source !== "screenplay" && ["failed", "partial", "cancelled"].includes(job.status);
  const canResume = [
    "paused_external",
    "waiting_retry",
    "waiting_human",
  ].includes(job.status);
  const providerCreateUnresolved = isProviderCreateUnresolved(job);
  // "superseded" rows are deliberately excluded here even though the backend
  // now scopes cancel to the record's own ownership (833236f) and can no
  // longer kill the live successor by mistake: the real attempt is the
  // successor, not this row, so a "取消任务" button here would let a user
  // "cancel" a task and then watch it keep running anyway — confusing even
  // though harmless. The record's real state is already visible via its
  // status/label and error text; only the misleading action is withheld.
  const canCancel = job.source !== "screenplay" && ["running", "queued", "recovering"].includes(job.status);
  const modeAudit = (
    detail?.video_mode_audit && typeof detail.video_mode_audit === "object"
      ? detail.video_mode_audit
      : null
  ) as VideoModeAudit | null;
  return (
    <div
      className="monitor-drawer-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.currentTarget === e.target) onClose();
      }}
    >
      <aside
        className="monitor-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="job-title"
        ref={(node) => {
          drawerRef.current = node;
        }}
      >
        <header>
          <div>
            <span className="eyebrow">任务详情</span>
            <h3 id="job-title">{jobWorkLabel(job)}</h3>
          </div>
          <button onClick={onClose} aria-label="关闭任务详情">
            ×
          </button>
        </header>
        <div className="monitor-gate-summary">
          <b>{jobStatusLabel(job.status)}</b>
          <span>
            {job.project_name || "上下文未关联"} · {fmtTime(job.updated_at)}
          </span>
        </div>
        {job.error && (
          <div className="monitor-impact">
            <b>当前影响：</b>
            <span>{jobNextStep(job)}</span>
            {job.recovered_by_run_id && onJumpToRun && (
              <button
                type="button"
                className="monitor-name-button"
                onClick={() =>
                  onJumpToRun(job.recovered_tail_run_id || job.recovered_by_run_id!)
                }
              >
                查看接管记录 {job.recovered_tail_run_id || job.recovered_by_run_id} →
              </button>
            )}
            <details className="monitor-error-details">
              <summary>查看错误详情</summary>
              <pre>{job.error}</pre>
            </details>
          </div>
        )}
        {error && (
          <div className="monitor-state error" role="alert">
            <b>任务详情加载失败</b>
            <span>当前列表摘要仍保留，依赖完整详情的处理操作不会显示。</span>
            <details className="monitor-error-details">
              <summary>查看错误详情</summary>
              <pre>{error}</pre>
            </details>
            <button onClick={load}>重试</button>
          </div>
        )}
        {actionMessage && (
          <div className="monitor-state ready" role="status">
            {actionMessage}
          </div>
        )}
        {!detail && !error && (
          <div className="monitor-loading">正在加载任务详情…</div>
        )}
        {modeAudit && (
          <section className="monitor-impact">
            <b>视频生成方式</b>
            <span>
              {monitorVideoModeLabel(modeAudit.planned_mode)}
              {" → "}
              {monitorVideoModeLabel(modeAudit.actual_mode)}
            </span>
            {modeAudit.depends_on_shot_id && <span>等待依赖：上一镜采用视频或尾帧</span>}
            {modeAudit.video_input_intent && <span>视频参考意图：{modeAudit.video_input_intent}</span>}
            {modeAudit.degraded_reason && <span>降级原因：{modeAudit.degraded_reason}</span>}
            {modeAudit.stale && <span>当前结果已失效：{modeAudit.stale_reason || "上游采用版本已变化"}</span>}
            <details><summary>能力与计划版本</summary><code>{modeAudit.capability_snapshot_id || "未记录能力快照"}</code></details>
          </section>
        )}
        {detail && (
          <details className="job-technical-details">
            <summary>技术详情</summary>
            <JsonViewer data={detail} collapsed={false} maxHeight="50vh" />
          </details>
        )}
        <ZeroCostRelease job={job} onReleased={() => { onChanged(); void load(); }} />
        <div className="monitor-drawer-actions">
          <span role="status">{copied}</span>
          {job.reason_code && (
            <button
              onClick={async () => {
                await navigator.clipboard.writeText(job.reason_code || "");
                setCopied(`错误码 ${job.reason_code} 已复制`);
              }}
            >
              复制错误码
            </button>
          )}
          {sourceUrl && (
            <button
              onClick={() => {
                window.location.href = sourceUrl;
              }}
            >
              去源页面处理
            </button>
          )}
          {canRetry && !confirmAction && (
            <button
              disabled={!!busy}
              onClick={() => setConfirmAction("retry")}
            >
              重试
            </button>
          )}
          {canResume && !confirmAction && (
            <button
              disabled={!!busy}
              onClick={() => setConfirmAction("resume")}
            >
              {providerCreateUnresolved ? "核对并恢复原任务" : "从检查点恢复"}
            </button>
          )}
          {canCancel && !confirmAction && (
            <button
              className="danger"
              onClick={() => setConfirmAction("cancel")}
            >
              取消任务
            </button>
          )}
        </div>
        {confirmAction && (
          <div className="monitor-inline-confirm">
            {confirmAction === "cancel"
              ? "取消会中止当前任务。"
              : confirmAction === "confirm_new_submission"
                ? PROVIDER_RESUBMISSION_WARNING
              : confirmAction === "resume"
                ? providerCreateUnresolved
                  ? "系统会先查找原供应商任务并继续查询，不会在此步骤重新提交 create。"
                  : "恢复会从安全检查点继续，并占用新的模型生成时长。"
                : "重试会创建新的执行轮次，并占用新的模型生成时长。"}
            <button
              disabled={!!busy}
              onClick={() => void runAction(confirmAction)}
            >
              {busy
                ? "处理中…"
                : confirmAction === "cancel"
                  ? "确认取消"
                  : confirmAction === "confirm_new_submission"
                    ? "已核对，确认重新提交"
                  : confirmAction === "resume"
                    ? providerCreateUnresolved ? "仅恢复原任务" : "确认恢复"
                    : "确认重试"}
            </button>
            <button
              disabled={!!busy}
              onClick={() => setConfirmAction("")}
            >
              返回
            </button>
          </div>
        )}
        {!canRetry && job.status === "succeeded" && (
          <p className="hint">
            已完成任务不可重试；如需新版本请从源页面重新发起。
          </p>
        )}
      </aside>
    </div>
  );
}
