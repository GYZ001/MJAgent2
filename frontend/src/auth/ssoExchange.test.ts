import { describe, expect, it, vi } from "vitest";
import {
  describeSsoExchangeError, extractSsoCode, runSsoBootExchange, stripSsoCodeParam,
} from "./ssoExchange";

describe("extractSsoCode / stripSsoCodeParam", () => {
  it("从 query 里取出 sso_code", () => {
    expect(extractSsoCode("?sso_code=abc123")).toBe("abc123");
    expect(extractSsoCode("?foo=bar&sso_code=abc123")).toBe("abc123");
  });

  it("没有 sso_code 时返回 null（正常情形，不是错误）", () => {
    expect(extractSsoCode("")).toBeNull();
    expect(extractSsoCode("?foo=bar")).toBeNull();
  });

  it("去掉 sso_code 后保留其余参数", () => {
    expect(stripSsoCodeParam("?sso_code=abc&foo=bar")).toBe("?foo=bar");
  });

  it("去掉 sso_code 后没有其余参数时返回空串（不留孤零零的问号）", () => {
    expect(stripSsoCodeParam("?sso_code=abc")).toBe("");
  });
});

describe("describeSsoExchangeError", () => {
  it("Error 实例取其 message", () => {
    expect(describeSsoExchangeError(new Error("交换码无效、已使用或已过期，请重新发起登录")))
      .toBe("交换码无效、已使用或已过期，请重新发起登录");
  });

  it("空文案兜底为通用提示", () => {
    expect(describeSsoExchangeError(new Error(""))).toBe("登录链接已失效，请重新发起登录");
  });
});

describe("runSsoBootExchange", () => {
  it("没有 sso_code 时不清地址栏、不发起交换", async () => {
    const replaceUrl = vi.fn();
    const exchange = vi.fn(async () => ({}));
    const result = await runSsoBootExchange({
      search: "", pathname: "/workspaces", hash: "", replaceUrl, exchange,
    });
    expect(result).toEqual({ attempted: false, ok: true });
    expect(replaceUrl).not.toHaveBeenCalled();
    expect(exchange).not.toHaveBeenCalled();
  });

  it("交换成功：地址栏被清（sso_code 抹掉，其余参数与 path/hash 保留），且用检测到的 code 发起交换", async () => {
    const replaceUrl = vi.fn();
    const exchange = vi.fn(async (code: string) => {
      expect(code).toBe("one-time-code");
      return { session_token: "tok" };
    });
    const result = await runSsoBootExchange({
      search: "?sso_code=one-time-code&tab=jobs", pathname: "/projects/p1/observability", hash: "#top",
      replaceUrl, exchange,
    });
    expect(result).toEqual({ attempted: true, ok: true });
    expect(replaceUrl).toHaveBeenCalledWith("/projects/p1/observability?tab=jobs#top");
    expect(exchange).toHaveBeenCalledWith("one-time-code");
  });

  it("交换失败：仍然先清地址栏（不给刷新重放的机会），并把可读错误文案带回", async () => {
    const replaceUrl = vi.fn();
    const exchange = vi.fn(async () => {
      throw new Error("交换码无效、已使用或已过期，请重新发起登录");
    });
    const result = await runSsoBootExchange({
      search: "?sso_code=replayed", pathname: "/workspaces", hash: "", replaceUrl, exchange,
    });
    expect(replaceUrl).toHaveBeenCalledWith("/workspaces");
    expect(result).toEqual({
      attempted: true, ok: false, errorMessage: "交换码无效、已使用或已过期，请重新发起登录",
    });
  });
});
