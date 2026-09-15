/**
 * 链路节点的耗时展示：已结束的节点显示 latency_ms，未结束的显示「已等待」实时计时。
 *
 * 2026-09-15 用户在映射台等了八分钟：模型调用在供应商侧 450 秒内一个字节都没
 * 返回，界面却把这个运行中的节点显示成「0ms」、整条链路显示成「12ms」——latency_ms
 * 只有结束后才有值，运行中恒为 0。「未结束」由数据推导（started_at 有值而
 * finished_at 为空），不看 status 枚举。计时以服务端 server_time 为锚，避免浏览器
 * 与服务器时钟偏差把等待时长算成负数或凭空多出几分钟。
 */
import { useEffect, useState } from "react";

export interface TraceTiming {
  started_at?: number | null;
  finished_at?: number | null;
  latency_ms?: number | null;
}

export function formatDuration(value?: number | null) {
  const milliseconds = Math.max(0, Number(value || 0));
  if (milliseconds < 1000) return `${Math.round(milliseconds)}ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1000).toFixed(1)} 秒`;
  return `${Math.floor(milliseconds / 60_000)} 分 ${Math.round((milliseconds % 60_000) / 1000)} 秒`;
}

/** 已开始且尚未记录结束时间。 */
export function isUnfinished(item: TraceTiming) {
  const started = Number(item.started_at);
  return Number.isFinite(started) && started > 0 && !item.finished_at;
}

/** 未结束节点自开始以来的毫秒数（以服务端秒级时钟 nowSeconds 为准）；已结束返回 null。 */
export function elapsedMs(item: TraceTiming, nowSeconds: number): number | null {
  if (!isUnfinished(item)) return null;
  return Math.max(0, (nowSeconds - Number(item.started_at)) * 1000);
}

export function durationLabel(item: TraceTiming, nowSeconds: number) {
  const elapsed = elapsedMs(item, nowSeconds);
  return elapsed === null ? formatDuration(item.latency_ms) : `已等待 ${formatDuration(elapsed)}`;
}

/** 链路本身或任一节点未结束时才需要实时计时。 */
export function hasUnfinished(trace: (TraceTiming & { nodes: TraceTiming[] }) | null | undefined) {
  return Boolean(trace && (isUnfinished(trace) || trace.nodes.some(isUnfinished)));
}

export function traceDurations(nodes: (TraceTiming & { id: string })[], nowSeconds: number) {
  return new Map(nodes.map((node): [string, string] => [node.id, durationLabel(node, nowSeconds)]));
}

/**
 * 以服务端时间为锚的秒级时钟：取回链路时记下 server_time 与本地时刻，之后按本地
 * 流逝推进；ticking 为 false（链路里没有未结束节点）时不起定时器。
 */
export function useServerClock(serverTime: number | null | undefined, ticking: boolean): number {
  const [now, setNow] = useState(() => Number(serverTime) || Date.now() / 1000);
  useEffect(() => {
    const anchor = Number(serverTime) || Date.now() / 1000;
    const anchoredAt = Date.now();
    const update = () => setNow(anchor + (Date.now() - anchoredAt) / 1000);
    update();
    if (!ticking) return undefined;
    const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [serverTime, ticking]);
  return now;
}
