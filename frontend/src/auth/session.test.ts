import { describe, expect, it } from "vitest";
import { canSeeSystemSettings, type AuthState } from "./session";

function stateWith(overrides: Partial<AuthState>): AuthState {
  return {
    user: { id: "u1", username: "alice", display_name: "Alice" },
    isSystemAdmin: false,
    ...overrides,
  };
}

describe("canSeeSystemSettings", () => {
  it("非系统管理员看不到系统设置", () => {
    const state = stateWith({ isSystemAdmin: false });
    expect(canSeeSystemSettings(state)).toBe(false);
  });

  it("系统管理员能看到系统设置", () => {
    const state = stateWith({ isSystemAdmin: true });
    expect(canSeeSystemSettings(state)).toBe(true);
  });
});
