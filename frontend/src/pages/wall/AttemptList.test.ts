import { createElement } from "react";
import TestRenderer, { act } from "react-test-renderer";
import { describe, expect, it } from "vitest";
import type { ShotVersion } from "../../api";

import AttemptList, { attemptAdoptability } from "./AttemptList";

describe("attemptAdoptability", () => {
  it("有可播放视频的版本都能采纳——人工采纳是最高优先级，字幕闸门只提示不拦", () => {
    expect(attemptAdoptability({ id: "v2", status: "succeeded", video_url: "/media/v2.mp4" }, "v1")).toEqual({ adoptable: true, reason: "" });
    expect(attemptAdoptability({ id: "v3", status: "waiting_human", video_url: "/media/v3.mp4" }, "v1").adoptable).toBe(true);
  });

  it("已是采纳版本 / 没有视频文件的不采纳", () => {
    expect(attemptAdoptability({ id: "v1", status: "succeeded", video_url: "/media/v1.mp4" }, "v1").reason).toBe("已是采纳版本");
    expect(attemptAdoptability({ id: "v5", status: "failed", video_url: undefined }, "v1").reason).toContain("没有可播放的视频");
    expect(attemptAdoptability({ id: "v6", status: "running", video_url: "" }, "v1").adoptable).toBe(false);
  });

  // 2026-09-23 用户拍板：台词修订/身份复核保留的历史版本（status='stale'）永远不可
  // 采纳——即便文件还在、可以播放，它依据的分镜内容已经作废。
  it("stale 版本不可采纳，且逐字带出后端写入的中文原因", () => {
    const result = attemptAdoptability(
      { id: "v7", status: "stale", video_url: "/media/v7.mp4", error: "台词修订前的版本（已过期，仅供对照，不可采纳）" },
      null,
    );
    expect(result).toEqual({ adoptable: false, reason: "台词修订前的版本（已过期，仅供对照，不可采纳）" });
  });

  it("stale 版本没有 error 时用中文兜底文案，不会显示空白原因", () => {
    const result = attemptAdoptability({ id: "v8", status: "stale", video_url: "/media/v8.mp4" }, null);
    expect(result.adoptable).toBe(false);
    expect(result.reason).toContain("不可采纳");
  });
});

const BASE_VERSION = { version_no: 1, prompt_text: "", latency_s: 0 };

function renderList(versions: ShotVersion[], adoptedId: string | null, projectAspectRatio?: string) {
  let view!: TestRenderer.ReactTestRenderer;
  act(() => {
    view = TestRenderer.create(createElement(AttemptList, {
      shotId: "s1", versions, previewId: null, adoptedId,
      statusLabel: (s: string) => s, stampClass: () => "stamp", projectAspectRatio,
      onPreview: () => {}, onToast: () => {}, onRefresh: async () => {},
    }));
  });
  return view;
}
function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === "string" ? c : textOf(c))).join("");
}

// 2026-09-23 用户拍板：台词修订保留的版本必须在生成台候选列表里可见——否则用户
// 完全看不到它、也不知道为什么点不动。唯一版本正常情况下列表精简为不展示。
describe("生成台候选列表：唯一版本是 stale 保留时仍必须可见", () => {
  it("唯一版本是 stale 时照常渲染，并标出「已过期」与具体原因", () => {
    const view = renderList(
      [{ ...BASE_VERSION, id: "v1", status: "stale", video_url: "/media/v1.mp4", error: "台词修订前的版本（已过期，仅供对照，不可采纳）" }],
      null,
    );
    const list = view.root.findAll(n => n.props["aria-label"] === "全部尝试");
    expect(list.length).toBe(1);
    const card = view.root.findAll(n => n.type === "button")[0];
    expect(textOf(card)).toContain("已过期");
    expect(textOf(card)).toContain("台词修订前的版本");
    act(() => view.unmount());
  });

  it("唯一版本是正常已采纳版本时不展示列表（保持原有精简展示，不因本次改动误伤）", () => {
    const view = renderList(
      [{ ...BASE_VERSION, id: "v1", status: "succeeded", video_url: "/media/v1.mp4" }],
      "v1",
    );
    const list = view.root.findAll(n => n.props["aria-label"] === "全部尝试");
    expect(list.length).toBe(0);
    act(() => view.unmount());
  });
});

// 2026-09-23 项目级画幅设置：候选版本的生成画幅与项目当前画幅不同时显示「旧画幅」
// 提示徽标，只提示不拦采纳；拿不到项目画幅就不显示（宁可不提示也不编造）。
describe("旧画幅提示徽标", () => {
  const TWO_VERSIONS = [
    { ...BASE_VERSION, id: "v1", version_no: 1, status: "succeeded", video_url: "/media/v1.mp4", aspect_ratio: "9:16" },
    { ...BASE_VERSION, id: "v2", version_no: 2, status: "succeeded", video_url: "/media/v2.mp4", aspect_ratio: "16:9" },
  ];

  it("版本画幅与项目当前画幅不同时显示旧画幅徽标", () => {
    const view = renderList(TWO_VERSIONS, "v1", "16:9");
    const cards = view.root.findAllByType("button").filter(b => b.props["aria-label"]?.startsWith("v"));
    expect(cards[0].props["aria-label"]).toContain("旧画幅 9:16");
    expect(cards[1].props["aria-label"]).not.toContain("旧画幅");
    act(() => view.unmount());
  });

  it("版本画幅与项目当前画幅相同时不显示徽标", () => {
    const view = renderList(TWO_VERSIONS, "v1", "9:16");
    const cards = view.root.findAllByType("button").filter(b => b.props["aria-label"]?.startsWith("v"));
    expect(cards[0].props["aria-label"]).not.toContain("旧画幅");
    act(() => view.unmount());
  });

  it("拿不到项目画幅（未传 projectAspectRatio）时不显示徽标", () => {
    const view = renderList(TWO_VERSIONS, "v1");
    const cards = view.root.findAllByType("button").filter(b => b.props["aria-label"]?.startsWith("v"));
    expect(cards.every(c => !c.props["aria-label"]?.includes("旧画幅"))).toBe(true);
    act(() => view.unmount());
  });

  it("版本没有 aspect_ratio 字段时按 9:16 处理（冻结契约：缺失即默认画幅）", () => {
    const view = renderList(
      [
        { ...BASE_VERSION, id: "v1", version_no: 1, status: "succeeded", video_url: "/media/v1.mp4" },
        { ...BASE_VERSION, id: "v2", version_no: 2, status: "succeeded", video_url: "/media/v2.mp4", aspect_ratio: "16:9" },
      ],
      null, "16:9",
    );
    const cards = view.root.findAllByType("button").filter(b => b.props["aria-label"]?.startsWith("v"));
    expect(cards[0].props["aria-label"]).toContain("旧画幅 9:16");
    expect(cards[1].props["aria-label"]).not.toContain("旧画幅");
    act(() => view.unmount());
  });
});
