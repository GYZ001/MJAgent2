import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("api session recovery", () => {
  it("refreshes a stale session and retries a rejected GET once", async () => {
    let sessionRequests = 0;
    const projectTokens: string[] = [];

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input);
        if (url === "/api/session") {
          sessionRequests += 1;
          return Response.json({
            session_token: sessionRequests === 1 ? "stale-token" : "fresh-token",
          });
        }
        if (url === "/api/projects") {
          const token = new Headers(init?.headers).get("X-Manju-Session") || "";
          projectTokens.push(token);
          if (token === "stale-token") {
            return Response.json({ detail: "本机会话已失效" }, { status: 401 });
          }
          return Response.json([{ id: "project-1" }]);
        }
        throw new Error(`unexpected request: ${url}`);
      }),
    );

    const { api } = await import("./api");
    await expect(api.get("/projects")).resolves.toEqual([{ id: "project-1" }]);

    expect(sessionRequests).toBe(2);
    expect(projectTokens).toEqual(["stale-token", "fresh-token"]);
  });
});
