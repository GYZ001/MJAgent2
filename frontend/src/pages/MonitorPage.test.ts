import { describe, expect, it } from "vitest";
import {
  blockStatus,
  categorizeSettingKeys,
  normalizeDraft,
  type SettingSchema,
} from "./MonitorPage";

const integer: SettingSchema = {
  label: "视频提交并发",
  type: "integer",
  default: "4",
  min: 1,
  max: 64,
  step: 1,
  unit: "任务",
  immediate: true,
  experimental: false,
};

describe("监制房数据块状态", () => {
  it("首次加载与无快照失败不会伪装成空数据", () => {
    expect(blockStatus(true, null, null, true)).toBe("loading");
    expect(blockStatus(false, "断网", null, true)).toBe("error");
  });

  it("有成功快照后刷新失败进入 stale", () => {
    expect(blockStatus(false, "超时", { total: 3 }, false)).toBe("stale");
  });

  it("只有成功查询才可进入 ready-empty", () => {
    expect(blockStatus(false, null, { total: 0 }, true)).toBe("ready-empty");
  });
});

describe("设置规范化与脏状态基础规则", () => {
  it("拒绝文本、非有限数、越界和非整数", () => {
    expect(normalizeDraft(integer, "abc")).toBeNull();
    expect(normalizeDraft(integer, "Infinity")).toBeNull();
    expect(normalizeDraft(integer, "0")).toBeNull();
    expect(normalizeDraft(integer, "2.5")).toBeNull();
  });

  it("数字字符串规范化后可与基线深比较", () => {
    expect(normalizeDraft(integer, "020")).toBe("20");
    expect(normalizeDraft(integer, "20")).toBe("20");
  });

  it("布尔与枚举只接受 schema 声明值", () => {
    const boolean: SettingSchema = {
      label: "开关",
      type: "boolean",
      default: "true",
      immediate: true,
      experimental: false,
    };
    const choice: SettingSchema = {
      label: "策略",
      type: "enum",
      default: "safe",
      options: ["safe", "fast"],
      immediate: true,
      experimental: false,
    };
    expect(normalizeDraft(boolean, "yes")).toBeNull();
    expect(normalizeDraft(boolean, "false")).toBe("false");
    expect(normalizeDraft(choice, "unknown")).toBeNull();
  });
});

describe("系统设置功能分类", () => {
  it("按业务影响归类且每个设置只出现一次", () => {
    const keys = [
      "video_submit_concurrency",
      "vlm_request_concurrency",
      "episode_cost_limit_cny",
      "storyboard_workspace_safe_readonly",
      "future_setting",
    ];
    const groups = categorizeSettingKeys(keys);
    expect(groups.find((group) => group.id === "video-flow")?.keys).toEqual([
      "video_submit_concurrency",
    ]);
    expect(
      groups.find((group) => group.id === "quality-repair")?.affects,
    ).toContain("视频质检");
    expect(groups.find((group) => group.id === "other")?.keys).toEqual([
      "future_setting",
    ]);
    expect(groups.flatMap((group) => group.keys).sort()).toEqual(
      [...keys].sort(),
    );
  });
});
