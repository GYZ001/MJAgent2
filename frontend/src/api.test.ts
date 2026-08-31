import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

/** 登录接口的最小合法响应：login() 只读 session_token，其余字段测试里用不到就不填。 */
function loginResponse(token: string) {
  return Response.json({ session_token: token });
}

describe("api session recovery", () => {
  it("GET 的 401 扛过一次重试后判定登录过期：拒绝返回，且只触发一次 unauthenticated 信号", async () => {
    let projectCalls = 0;
    const projectTokens: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/auth/login") return loginResponse("login-token");
        if (url === "/api/projects") {
          projectCalls += 1;
          projectTokens.push(new Headers(init?.headers).get("X-Manju-Session") || "");
          // 登录态在服务端已经失效（会话过期/被登出/被吊销）：不存在能静默换新的凭证，
          // 无论重试几次都还是 401。
          return Response.json({ detail: "登录已过期" }, { status: 401 });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { api, login, onUnauthenticated } = await import("./api");
    await login("alice", "secret12");

    let unauthenticatedCalls = 0;
    onUnauthenticated(() => {
      unauthenticatedCalls += 1;
    });

    // 调用方不能被静默吞掉失败——旧版「刷新会话后静默成功」的语义已经不存在了。
    await expect(api.get("/projects")).rejects.toMatchObject({ status: 401 });

    // 仍然只重试一次（覆盖"登录刚完成、请求发出时用的还是旧值"的竞态），
    // 而不是无限重试；重试用的还是内存里同一个 token，因为没有别的凭证可换。
    expect(projectCalls).toBe(2);
    expect(projectTokens).toEqual(["login-token", "login-token"]);
    // 观察的是效果（订阅者真的被调用了一次），不是"某个函数被调用过"这种壳。
    expect(unauthenticatedCalls).toBe(1);
  });

  it("reports a stopped local backend with an actionable error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));

    const { api } = await import("./api");

    await expect(api.get("/projects")).rejects.toMatchObject({
      code: "BACKEND_UNAVAILABLE",
      message: "无法连接本机后端服务，请等待服务恢复后重试",
    });
  });

  it("下载的 401 扛过一次重试后判定登录过期：拒绝返回，且只触发一次 unauthenticated 信号", async () => {
    let archiveCalls = 0;
    const archiveTokens: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/auth/login") return loginResponse("login-token");
        if (url === "/api/delivery/packages/pkg-1/archive") {
          archiveCalls += 1;
          archiveTokens.push(new Headers(init?.headers).get("X-Manju-Session") || "");
          return Response.json({ detail: "登录已过期" }, { status: 401 });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { api, login, onUnauthenticated } = await import("./api");
    await login("alice", "secret12");

    let unauthenticatedCalls = 0;
    onUnauthenticated(() => {
      unauthenticatedCalls += 1;
    });

    await expect(api.download("/delivery/packages/pkg-1/archive")).rejects.toMatchObject({
      status: 401,
    });

    expect(archiveCalls).toBe(2);
    expect(archiveTokens).toEqual(["login-token", "login-token"]);
    expect(unauthenticatedCalls).toBe(1);
  });

  it("does not loop when a download stays rejected after the session refresh", async () => {
    let downloads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        downloads += 1;
        return Response.json({ detail: "本机会话已失效" }, { status: 401 });
      }),
    );

    const { api } = await import("./api");
    await expect(api.download("/delivery/packages/pkg-1/report")).rejects.toMatchObject({
      status: 401,
    });
    expect(downloads).toBe(2);
  });

  it("reports a stopped local backend for downloads too", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));

    const { api } = await import("./api");
    await expect(api.download("/delivery/packages/pkg-1/report")).rejects.toMatchObject({
      code: "BACKEND_UNAVAILABLE",
      message: "无法连接本机后端服务，请等待服务恢复后重试",
    });
  });

  it("内存里的登录 token 在多次写操作之间原样复用，不会引发额外的网络请求", async () => {
    let loginCalls = 0;
    let mutations = 0;
    const mutationTokens: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/auth/login") {
          loginCalls += 1;
          return loginResponse("login-token");
        }
        if (url === "/api/test-mutation") {
          mutations += 1;
          mutationTokens.push(new Headers(init?.headers).get("X-Manju-Session") || "");
          return Response.json({ ok: true });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { api, login } = await import("./api");
    await login("alice", "secret12");
    await api.post("/test-mutation", {});
    await api.post("/test-mutation", {});

    // 旧版这里验证的是"共享的匿名会话只领取一次、后续写操作复用它"；换成登录态后，
    // 对应的保证是：登录只调用一次，签发的 token 原样复用两次写操作，中间不再有任何
    // 额外的"维持会话"往返请求（这类往返在新设计里根本不存在了）。
    expect(loginCalls).toBe(1);
    expect(mutations).toBe(2);
    expect(mutationTokens).toEqual(["login-token", "login-token"]);
  });

  it("unauthenticated 信号只在 401 扛过重试时触发一次；恢复正常后的请求不会误触发", async () => {
    let projectsFailing = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url === "/api/auth/login") return loginResponse("login-token");
        if (url === "/api/projects") {
          if (projectsFailing) return Response.json({ detail: "登录已过期" }, { status: 401 });
          return Response.json([{ id: "project-1" }]);
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { api, login, onUnauthenticated } = await import("./api");
    await login("alice", "secret12");

    let unauthenticatedCalls = 0;
    onUnauthenticated(() => {
      unauthenticatedCalls += 1;
    });

    // 第一次：扛过重试仍是 401，判定登录过期——触发且只触发一次（不是零次）。
    await expect(api.get("/projects")).rejects.toMatchObject({ status: 401 });
    expect(unauthenticatedCalls).toBe(1);

    // 重新登录后端点恢复正常：正常请求不该再把信号又触发一次（不是两次）。
    projectsFailing = false;
    await login("alice", "secret12");
    await expect(api.get("/projects")).resolves.toEqual([{ id: "project-1" }]);
    expect(unauthenticatedCalls).toBe(1);
  });
});

describe("waiting_approval 的自动消费边界（2026-08-30：除了删除资源，否则不需要弹窗）", () => {
  it("confirmation_policy=always（删除资源）不自动消费，抛出 ApprovalRequiredError 供调用方展示确认弹窗", async () => {
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/auth/login") return loginResponse("login-token");
        if (url === "/api/projects/p1/purge") {
          calls += 1;
          const token = new Headers(init?.headers).get("X-Manju-Approval-Token");
          if (!token) {
            return Response.json(
              {
                status: "waiting_approval",
                approval_token: "tok-1",
                preflight: { summary: "彻底删除项目", confirmation_policy: "always" },
              },
              { status: 202 },
            );
          }
          return Response.json({ ok: true, purged: true });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { api, login, ApprovalRequiredError } = await import("./api");
    await login("alice", "secret12");

    let caught: InstanceType<typeof ApprovalRequiredError> | undefined;
    try {
      await api.del("/projects/p1/purge");
    } catch (e) {
      caught = e as InstanceType<typeof ApprovalRequiredError>;
    }
    expect(caught).toBeInstanceOf(ApprovalRequiredError);
    expect(caught?.preflight.summary).toBe("彻底删除项目");
    expect(calls).toBe(1); // 没有自动带 approval_token 重放

    await expect(caught!.retry()).resolves.toEqual({ ok: true, purged: true });
    expect(calls).toBe(2);
  });

  it("confirmation_policy 不是 always（非删除资源）时仍照旧自动带 approval_token 重放，不弹窗", async () => {
    let calls = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/auth/login") return loginResponse("login-token");
        if (url === "/api/system/settings") {
          calls += 1;
          const token = new Headers(init?.headers).get("X-Manju-Approval-Token");
          if (!token) {
            return Response.json(
              {
                status: "waiting_approval",
                approval_token: "tok-2",
                preflight: { summary: "更新设置", confirmation_policy: "when_impact" },
              },
              { status: 202 },
            );
          }
          return Response.json({ ok: true, updated: true });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { api, login } = await import("./api");
    await login("alice", "secret12");

    await expect(api.put("/system/settings", {})).resolves.toEqual({ ok: true, updated: true });
    expect(calls).toBe(2); // 首次拿到 202，第二次自动带 token 重放——调用方全程无感
  });
});
