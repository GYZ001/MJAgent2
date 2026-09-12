/**
 * 应用启动时处理 SSO 回跳的纯逻辑（DOM 无关，供 vitest `environment: 'node'`
 * 直接单测）。URL 上的 `?sso_code=` 是一次性交换码（60 秒 TTL，见
 * app/sso/api.py 模块文档"会话交接不走查询串"）：真正的会话令牌只在
 * `POST /auth/sso/exchange` 的响应体里，从不出现在地址栏——地址栏上这枚码
 * 拿到后必须立刻清掉，这是纵深防御最外层，不是唯一防线。
 *
 * DOM 交互（`window.location`/`window.history.replaceState`）留给调用方
 * （`auth/AuthContext.tsx`）注入，本文件只做字符串处理与流程编排。
 */

/** 从 `location.search` 里取 sso_code；不存在返回 null（这是正常情形——
 *  绝大多数页面加载都没有它，不是错误）。 */
export function extractSsoCode(search: string): string | null {
  return new URLSearchParams(search).get("sso_code");
}

/** 去掉 sso_code 参数后剩下的 query 字符串（含前导 `?`；没有其它参数则为
 *  空串）。其余参数原样保留，不清空整个 query。 */
export function stripSsoCodeParam(search: string): string {
  const params = new URLSearchParams(search);
  params.delete("sso_code");
  const rest = params.toString();
  return rest ? `?${rest}` : "";
}

/** 交换失败的用户可读文案；与后端口径一致——不区分「过期/重放/无效」，
 *  统一提示重新登录。 */
export function describeSsoExchangeError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err ?? "");
  return message || "登录链接已失效，请重新发起登录";
}

export interface SsoBootLocation {
  search: string;
  pathname: string;
  hash: string;
}

export interface SsoBootDeps extends SsoBootLocation {
  /** 把地址栏替换成给定 URL（调用方用 `window.history.replaceState` 实现）。 */
  replaceUrl: (url: string) => void;
  /** 真正发起交换请求（调用方传 `exchangeSsoCode`）。 */
  exchange: (code: string) => Promise<unknown>;
}

export interface SsoBootResult {
  /** 本次启动是否检测到 sso_code 并尝试了交换；false 时其余字段无意义。 */
  attempted: boolean;
  ok: boolean;
  errorMessage?: string;
}

/**
 * 检测并处理一次 SSO 回跳。无论交换成功与否，只要检测到 sso_code 就立刻
 * 清地址栏（避免用户刷新页面时重放同一枚一次性码）；交换失败时把可读文案
 * 透出，调用方负责展示。
 */
export async function runSsoBootExchange(deps: SsoBootDeps): Promise<SsoBootResult> {
  const code = extractSsoCode(deps.search);
  if (!code) return { attempted: false, ok: true };

  const cleanUrl = `${deps.pathname}${stripSsoCodeParam(deps.search)}${deps.hash}`;
  deps.replaceUrl(cleanUrl);

  try {
    await deps.exchange(code);
    return { attempted: true, ok: true };
  } catch (err) {
    return { attempted: true, ok: false, errorMessage: describeSsoExchangeError(err) };
  }
}
