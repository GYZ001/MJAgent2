import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("exchangeSsoCode", () => {
  it("交换成功后把令牌记进内存/localStorage，后续请求立刻带上它", async () => {
    const tokens: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/auth/sso/exchange") {
          const body = JSON.parse(String(init?.body));
          expect(body).toEqual({ code: "one-time-code" });
          return Response.json({ session_token: "sso-token", header: "X-Manju-Session" });
        }
        if (url === "/api/projects") {
          tokens.push(new Headers(init?.headers).get("X-Manju-Session") || "");
          return Response.json([]);
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { exchangeSsoCode } = await import("./sso");
    const { api } = await import("./index");
    const data = await exchangeSsoCode("one-time-code");
    expect(data.session_token).toBe("sso-token");

    await api.get("/projects");
    expect(tokens).toEqual(["sso-token"]);
  });

  it("交换失败（码过期/重放/无效）时抛出错误，不落任何令牌", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url === "/api/auth/sso/exchange") {
          return Response.json({ detail: "交换码无效、已使用或已过期，请重新发起登录" }, { status: 400 });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { exchangeSsoCode } = await import("./sso");
    await expect(exchangeSsoCode("replayed-code")).rejects.toMatchObject({
      status: 400,
      message: "交换码无效、已使用或已过期，请重新发起登录",
    });
  });
});

describe("listSsoProviders / ssoStartUrl", () => {
  it("拉取启用中的 IdP 列表", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input);
        if (url === "/api/auth/sso/providers") {
          return Response.json({ items: [{ id: "idp_1", name: "公司 Okta", kind: "oidc" }] });
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );
    const { listSsoProviders } = await import("./sso");
    await expect(listSsoProviders()).resolves.toEqual({
      items: [{ id: "idp_1", name: "公司 Okta", kind: "oidc" }],
    });
  });

  it("构造的跳转地址带上 idp_id 与 redirect_to，且不经 fetch", async () => {
    const { ssoStartUrl } = await import("./sso");
    expect(ssoStartUrl("idp_1", "/workspaces")).toBe(
      "/api/auth/sso/idp_1/start?redirect_to=%2Fworkspaces",
    );
  });
});

describe("listMyIdentities / startSsoLink / unlinkSso", () => {
  it("按预期的方法与路径发请求", async () => {
    const calls: { method: string; url: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        calls.push({ method: init?.method || "GET", url });
        if (url === "/api/auth/sso/my-identities") return Response.json({ items: [] });
        if (url === "/api/auth/sso/link") return Response.json({ authorize_url: "https://idp.example/authorize" });
        if (url === "/api/auth/sso/link/idp_1") return Response.json({ ok: true });
        throw new Error(`unexpected request: ${url}`);
      }),
    );
    const { listMyIdentities, startSsoLink, unlinkSso } = await import("./sso");
    await listMyIdentities();
    await startSsoLink("idp_1", "/settings");
    await unlinkSso("idp_1");
    expect(calls).toEqual([
      { method: "GET", url: "/api/auth/sso/my-identities" },
      { method: "POST", url: "/api/auth/sso/link" },
      { method: "DELETE", url: "/api/auth/sso/link/idp_1" },
    ]);
  });
});
