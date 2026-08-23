import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it } from "vitest";

import { isStaleRecord, loadRecordForTest, ServerTaskTimer } from "./TaskTimer";

const STORAGE_KEY = "mjagent.timer.episode.ep_1.screenplay";

/** 项目测试环境是 node（未装 jsdom），这里只补一个最小的 localStorage。 */
function installStorageStub() {
  const store = new Map<string, string>();
  (globalThis as { window?: unknown }).window = {
    localStorage: {
      getItem: (key: string) => store.get(key) ?? null,
      setItem: (key: string, value: string) => { store.set(key, value); },
      clear: () => { store.clear(); },
    },
  };
}

describe("ServerTaskTimer", () => {
  it("按服务端起止时间计算耗时，不依赖本地起点", () => {
    const html = renderToStaticMarkup(createElement(ServerTaskTimer, {
      label: "剧本",
      startedAt: 1_000,
      finishedAt: 1_125,
      running: false,
    }));

    expect(html).toContain("本次耗时");
    expect(html).toContain("2分05秒");
  });

  it("缺少服务端起点时不渲染，避免显示臆造的时长", () => {
    const html = renderToStaticMarkup(createElement(ServerTaskTimer, {
      label: "剧本",
      startedAt: null,
      finishedAt: null,
      running: true,
    }));

    expect(html).toBe("");
  });

  it("运行中以服务端起点为准，20 小时前的本地残留不参与计算", () => {
    const startedAt = Math.floor(Date.now() / 1000) - 90;
    const html = renderToStaticMarkup(createElement(ServerTaskTimer, {
      label: "剧本",
      startedAt,
      finishedAt: null,
      running: true,
    }));

    expect(html).toContain("已执行");
    expect(html).not.toContain("1244");
  });
});

describe("useTaskTimer 的搁浅记录清理", () => {
  beforeEach(() => {
    installStorageStub();
  });

  it("搁浅判定只看心跳距今多久，与起点早晚无关", () => {
    const now = Date.now();
    // 起点很早但心跳新鲜 = 运行中刷新，必须续算。
    expect(isStaleRecord({ startAt: now - 20 * 3600_000, seenAt: now - 1_000 }, now)).toBe(false);
    // 起点很近但心跳已停 = 页面关过，必须丢弃。
    expect(isStaleRecord({ startAt: now - 120_000, seenAt: now - 120_000 }, now)).toBe(true);
    // 没有起点就无所谓搁浅。
    expect(isStaleRecord({ lastMs: 1_000 }, now)).toBe(false);
  });

  it("搁浅阈值为 20 秒：刷新期内保留，超出即丢弃", () => {
    const now = Date.now();
    expect(isStaleRecord({ startAt: now - 60_000, seenAt: now - 19_000 }, now)).toBe(false);
    expect(isStaleRecord({ startAt: now - 60_000, seenAt: now - 21_000 }, now)).toBe(true);
  });

  it("心跳过期的起点会被丢弃，新任务不会从旧时间累加", () => {
    const stranded = Date.now() - 20 * 60 * 60 * 1000;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      startAt: stranded,
      seenAt: stranded,
    }));

    expect(loadRecordForTest(STORAGE_KEY).startAt).toBeUndefined();
  });

  it("心跳新鲜的起点保留，运行中刷新页面仍续算", () => {
    const startAt = Date.now() - 30_000;
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      startAt,
      seenAt: Date.now() - 2_000,
    }));

    expect(loadRecordForTest(STORAGE_KEY).startAt).toBe(startAt);
  });

  it("缺少心跳字段的旧记录按起点判定，超期同样丢弃", () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      startAt: Date.now() - 20 * 60 * 60 * 1000,
    }));

    expect(loadRecordForTest(STORAGE_KEY).startAt).toBeUndefined();
  });

  it("已完成记录（只有 lastMs）不受搁浅判定影响", () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({
      lastMs: 5_000,
      finishedAt: Date.now() - 20 * 60 * 60 * 1000,
    }));

    expect(loadRecordForTest(STORAGE_KEY).lastMs).toBe(5_000);
  });
});
