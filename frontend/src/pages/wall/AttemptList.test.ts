import { describe, expect, it } from "vitest";

import { attemptAdoptability } from "./AttemptList";

describe("attemptAdoptability", () => {
  it("有可播放视频的版本都能采纳——人工采纳是最高优先级，字幕闸门只提示不拦", () => {
    expect(attemptAdoptability({ id: "v2", status: "succeeded", video_url: "/media/v2.mp4" }, "v1")).toEqual({ adoptable: true, reason: "" });
    expect(attemptAdoptability({ id: "v3", status: "waiting_human", video_url: "/media/v3.mp4" }, "v1").adoptable).toBe(true);
  });

  it("已是采纳版本 / 没有视频文件的不采纳", () => {
    expect(attemptAdoptability({ id: "v1", status: "succeeded", video_url: "/media/v1.mp4" }, "v1").reason).toBe("已是采纳版本");
    expect(attemptAdoptability({ id: "v5", status: "failed", video_url: undefined }, "v1").reason).toContain("没有可播放的视频");
    expect(attemptAdoptability({ id: "v6", status: "running", video_url: "" }, "v1").adoptable).toBe(false);
  });
});
