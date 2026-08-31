import { useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import type { Job } from "../../api";

/** 「供应商任务尚未终态」拦住清空/重做时，界面此前没有任何入口能核对——这个
 * 组件就是那个入口：只对已证明零扣费的终态拒绝任务展示，本地二段式确认
 * （不带 confirm 只预览，确认才真正把预留结算为 0），不提供强制忽略式绕过——
 * 不满足条件的任务这里不会出现按钮，点击后台仍会重新核验一遍。
 * 从 JobDrawer 抽成独立文件是因为该文件已经踩着行数基线，没有余量再长。 */
export default function ZeroCostRelease({
  job,
  onReleased,
}: {
  job: Job;
  onReleased: () => void;
}) {
  const [eligible, setEligible] = useState<{ reason: string; amount: number } | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    setEligible(null);
    setConfirming(false);
    setDone(false);
    setError("");
    if (job.status !== "waiting_human") return;
    let cancelled = false;
    void api.getZeroCostCandidate(job.id).then(
      (result) => {
        if (!cancelled && result.eligible) {
          setEligible({ reason: result.reason, amount: result.reserved_amount_cny });
        }
      },
      () => undefined, // 查询失败就不展示按钮，不把不确定的判断当作可以释放
    );
    return () => {
      cancelled = true;
    };
  }, [job.id, job.status]);

  if (done) {
    return (
      <div className="monitor-state ready" role="status">
        预留已结算为 0 元，清空/重做阻塞已解除，可刷新后重试原操作。
      </div>
    );
  }
  if (!eligible) return null;

  return (
    <div className="monitor-impact">
      <b>供应商已终态拒绝，且未产生任何费用：</b>
      <span>{eligible.reason}（预留 ¥{eligible.amount.toFixed(2)}）</span>
      {error && <span role="alert">{error}</span>}
      {!confirming ? (
        <button type="button" onClick={() => setConfirming(true)}>
          释放此任务的预留预算
        </button>
      ) : (
        <div className="monitor-inline-confirm">
          确认后会把这笔预留结算为 0 元，并解除对该镜头清空/重做操作的阻塞，不可撤销。
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              setError("");
              try {
                await api.releaseZeroCostJobs([job.id], true);
                setDone(true);
                onReleased();
              } catch (e) {
                setError(e instanceof ApiError ? e.message : String(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "处理中…" : "确认释放"}
          </button>
          <button disabled={busy} onClick={() => setConfirming(false)}>
            返回
          </button>
        </div>
      )}
    </div>
  );
}
