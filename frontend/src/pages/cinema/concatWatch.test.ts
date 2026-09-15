import { describe, expect, it } from "vitest";

import { concatOutcome, isConcatAccepted } from "./concatWatch";

const base = { episode_id: "e", title: "t", episode_no: 1, shots_total: 1, shots_ready: 1, ready: true, final_video_url: null, shots: [] };

describe("concatWatch", () => {
  it("进行中→结束才算本轮完成；带错误即失败", () => {
    expect(concatOutcome({ ...base, concat_in_progress: true }, { ...base, concat_in_progress: false })).toEqual({ finished: true, error: null });
    expect(concatOutcome({ ...base, concat_in_progress: true }, { ...base, concat_in_progress: false, concat_last_error: "ffmpeg 失败" })).toEqual({ finished: true, error: "ffmpeg 失败" });
    expect(concatOutcome({ ...base, concat_in_progress: true }, { ...base, concat_in_progress: true }).finished).toBe(false);
    expect(concatOutcome({ ...base, concat_in_progress: false }, { ...base, concat_in_progress: false }).finished).toBe(false);
    expect(concatOutcome(null, { ...base }).finished).toBe(false);
  });

  it("识别 202 受理响应", () => {
    expect(isConcatAccepted({ status: "accepted", concat_in_progress: true })).toBe(true);
    expect(isConcatAccepted({ status: "succeeded", shots: 17 })).toBe(false);
    expect(isConcatAccepted(null)).toBe(false);
  });
});
