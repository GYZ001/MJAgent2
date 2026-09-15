import { describe, expect, it } from "vitest";

import { attemptAdoptability } from "./AttemptList";

describe("attemptAdoptability", () => {
  it("已成功且过技术门禁的版本可采纳", () => {
    const v = { id: "v2", status: "succeeded", technical_validation_json: JSON.stringify({ passed: true }) };
    expect(attemptAdoptability(v, "v1")).toEqual({ adoptable: true, reason: "" });
  });

  it("已是采纳版本 / 未成功 / 未过门禁的只预览不采纳", () => {
    expect(attemptAdoptability({ id: "v1", status: "succeeded", technical_validation_json: JSON.stringify({ passed: true }) }, "v1").adoptable).toBe(false);
    expect(attemptAdoptability({ id: "v3", status: "waiting_human", technical_validation_json: null }, "v1").reason).toContain("未成功");
    expect(attemptAdoptability({ id: "v2", status: "succeeded", technical_validation_json: JSON.stringify({ passed: false, issues: [] }) }, "v1").reason).toContain("技术门禁");
    expect(attemptAdoptability({ id: "v2", status: "succeeded", technical_validation_json: "not json" }, "v1").adoptable).toBe(false);
  });
});
