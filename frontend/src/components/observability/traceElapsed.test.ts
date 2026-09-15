import { describe, expect, it } from "vitest";

import { durationLabel, elapsedMs, formatDuration, isUnfinished } from "./traceElapsed";

const STARTED = 1_789_461_238; // 2026-09-15 16:13:58 CST，用户等了八分钟的那次映射包生成

describe("traceElapsed", () => {
  it("已开始且没有 finished_at 的节点判为未结束，结束时间一落盘就不再计时", () => {
    expect(isUnfinished({ started_at: STARTED, finished_at: null })).toBe(true);
    expect(isUnfinished({ started_at: STARTED, finished_at: STARTED + 451 })).toBe(false);
    expect(isUnfinished({ started_at: null, finished_at: null })).toBe(false);
    expect(isUnfinished({})).toBe(false);
  });

  it("运行中节点按服务端时钟显示已等待时长，而不是 latency_ms 的 0", () => {
    const node = { started_at: STARTED, finished_at: null, latency_ms: 0 };
    expect(elapsedMs(node, STARTED + 8 * 60)).toBe(8 * 60 * 1000);
    expect(durationLabel(node, STARTED + 8 * 60)).toBe("已等待 8 分 0 秒");
  });

  it("已结束节点照旧显示 latency_ms", () => {
    const node = { started_at: STARTED, finished_at: STARTED + 103, latency_ms: 103_063 };
    expect(elapsedMs(node, STARTED + 9999)).toBeNull();
    expect(durationLabel(node, STARTED + 9999)).toBe("1 分 43 秒");
  });

  it("服务端时钟落后于 started_at 时不显示负数", () => {
    expect(elapsedMs({ started_at: STARTED, finished_at: null }, STARTED - 5)).toBe(0);
  });

  it("formatDuration 分档与原实现一致", () => {
    expect(formatDuration(12)).toBe("12ms");
    expect(formatDuration(48_800)).toBe("48.8 秒");
    expect(formatDuration(451_573)).toBe("7 分 32 秒");
    expect(formatDuration(null)).toBe("0ms");
  });
});
