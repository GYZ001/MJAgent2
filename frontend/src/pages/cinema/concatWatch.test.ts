import { describe, expect, it } from "vitest";

import { concatOutcome, isConcatAccepted } from "./concatWatch";

const base = { episode_id: "e", title: "t", episode_no: 1, shots_total: 1, shots_ready: 1, ready: true, final_video_url: null, shots: [] };

describe("concatWatch", () => {
  it("仍挂着幂等键、不在后台执行、且 receipt 落到终态才算本轮完成；带错误即失败", () => {
    expect(concatOutcome({ ...base, concat_in_progress: false, concat_receipt: { status: "succeeded", error: null } }, true)).toEqual({ finished: true, error: null });
    expect(concatOutcome({ ...base, concat_in_progress: false, concat_last_error: "ffmpeg 失败", concat_receipt: { status: "failed", error: "ffmpeg 失败" } }, true)).toEqual({ finished: true, error: "ffmpeg 失败" });
    expect(concatOutcome({ ...base, concat_in_progress: true }, true).finished).toBe(false);
    expect(concatOutcome(null, true).finished).toBe(false);
  });

  it("没有未清理的幂等键时不重复收尾（避免对已清空的键反复弹 toast）", () => {
    expect(concatOutcome({ ...base, concat_in_progress: false, concat_receipt: { status: "succeeded", error: null } }, false).finished).toBe(false);
    expect(concatOutcome({ ...base, concat_in_progress: false, concat_last_error: "ffmpeg 失败", concat_receipt: { status: "failed", error: "ffmpeg 失败" } }, false).finished).toBe(false);
  });

  it("2026-10-01 生产事故回归：刷新页面后组件重新挂载，第一次轮询就能看到失败收尾——不要求先观测到 running=true", () => {
    // 旧实现用「上一次/这一次」比较边沿，组件重挂载后 previous 清零，第一帧就是
    // concat_in_progress=false，边沿被永久错过。新判据只看这一帧 + 幂等键是否还在。
    const freshMountFirstPoll = {
      ...base,
      concat_in_progress: false,
      concat_last_error: "合片发布前已采纳视频发生漂移",
      concat_receipt: { status: "failed", error: "合片发布前已采纳视频发生漂移" },
    };
    expect(concatOutcome(freshMountFirstPoll, true)).toEqual({ finished: true, error: "合片发布前已采纳视频发生漂移" });
  });

  it("2026-10-01 对抗式复查 #0/#1 回归：后端重启腰斩在途任务后，不得把'说不清'判成'成功'", () => {
    // concat_in_progress/concat_last_error 是进程内存，重启后都会变成 false/undefined；
    // 如果只看这两个字段 + 幂等键还在，会被误判成"本轮完成、无错误"，弹出虚假的
    // "合成完成"提示——但实际上旧成片原封未动。concat_receipt 读持久化的
    // concat_operation_receipts.status，重启后依然诚实地停在 'running'。
    const afterRestartFirstPoll = { ...base, concat_in_progress: false, concat_receipt: { status: "running", error: null } };
    expect(concatOutcome(afterRestartFirstPoll, true)).toEqual({ finished: true, error: null, unknown: true });

    // 连 receipt 都查不到（极老的遗留幂等键，功能上线前写入的）同样不能判成功。
    const noReceiptAtAll = { ...base, concat_in_progress: false };
    expect(concatOutcome(noReceiptAtAll, true)).toEqual({ finished: true, error: null, unknown: true });
  });

  it("识别 202 受理响应", () => {
    expect(isConcatAccepted({ status: "accepted", concat_in_progress: true })).toBe(true);
    expect(isConcatAccepted({ status: "succeeded", shots: 17 })).toBe(false);
    expect(isConcatAccepted(null)).toBe(false);
  });
});
