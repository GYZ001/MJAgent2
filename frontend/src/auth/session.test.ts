import { describe, expect, it } from "vitest";
import {
  ALL_SCOPES,
  ROLE_SCOPES,
  canSeeSystemSettings,
  isSystemAdmin,
  roleLabel,
  scopesFor,
  type AuthState,
} from "./session";

function stateWith(overrides: Partial<AuthState>): AuthState {
  return {
    user: { id: "u1", username: "alice", display_name: "Alice" },
    workspaces: [],
    isSystemAdmin: false,
    ...overrides,
  };
}

describe("ROLE_SCOPES", () => {
  it("workspace_admin 拥有读/写项目/文字生成/媒体生成/交付", () => {
    expect(ROLE_SCOPES.workspace_admin).toEqual([
      "manju:read",
      "manju:project-write",
      "manju:generation-text",
      "manju:generation-media",
      "manju:delivery",
    ]);
  });

  it("production 拥有读/写项目/文字生成/媒体生成，但没有交付", () => {
    expect(ROLE_SCOPES.production).toEqual([
      "manju:read",
      "manju:project-write",
      "manju:generation-text",
      "manju:generation-media",
    ]);
  });

  it("review 只有读与交付", () => {
    expect(ROLE_SCOPES.review).toEqual(["manju:read", "manju:delivery"]);
  });

  it("readonly 只有读", () => {
    expect(ROLE_SCOPES.readonly).toEqual(["manju:read"]);
  });
});

describe("scopesFor", () => {
  it("系统管理员在任意团队都拿到全部 scope", () => {
    const state = stateWith({ isSystemAdmin: true, workspaces: [] });
    expect(scopesFor(state, "ws-not-a-member")).toEqual(new Set(ALL_SCOPES));
  });

  it("普通用户按所属团队的角色换算 scope", () => {
    const state = stateWith({
      workspaces: [{ id: "ws1", name: "团队一", role: "production" }],
    });
    expect(scopesFor(state, "ws1")).toEqual(new Set(ROLE_SCOPES.production));
  });

  it("不在该团队时返回空集", () => {
    const state = stateWith({
      workspaces: [{ id: "ws1", name: "团队一", role: "workspace_admin" }],
    });
    expect(scopesFor(state, "ws2").size).toBe(0);
  });
});

describe("isSystemAdmin / canSeeSystemSettings", () => {
  it("非系统管理员两者都为 false", () => {
    const state = stateWith({
      isSystemAdmin: false,
      workspaces: [{ id: "ws1", name: "团队一", role: "workspace_admin" }],
    });
    expect(isSystemAdmin(state)).toBe(false);
    expect(canSeeSystemSettings(state)).toBe(false);
  });

  it("空间管理员（团队内角色）不等于系统管理员，看不到系统设置", () => {
    const state = stateWith({
      isSystemAdmin: false,
      workspaces: [{ id: "ws1", name: "团队一", role: "workspace_admin" }],
    });
    expect(canSeeSystemSettings(state)).toBe(false);
  });

  it("系统管理员两者都为 true", () => {
    const state = stateWith({ isSystemAdmin: true });
    expect(isSystemAdmin(state)).toBe(true);
    expect(canSeeSystemSettings(state)).toBe(true);
  });
});

describe("roleLabel", () => {
  it("映射 4 个角色到中文展示名", () => {
    expect(roleLabel("workspace_admin")).toBe("空间管理员");
    expect(roleLabel("production")).toBe("制作");
    expect(roleLabel("review")).toBe("审校");
    expect(roleLabel("readonly")).toBe("只读");
  });

  it("未知角色原样返回，不抛错", () => {
    expect(roleLabel("weird_role")).toBe("weird_role");
  });
});
