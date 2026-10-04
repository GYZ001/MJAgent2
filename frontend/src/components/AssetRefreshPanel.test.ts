import { createElement } from "react";
import TestRenderer, { act } from "react-test-renderer";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockReport, mockRegenerate, mockAdopt } = vi.hoisted(() => ({
  mockReport: vi.fn(),
  mockRegenerate: vi.fn(async () => ({ episode_id: "e1", queued: ["s1"], errors: [], message: "已为 1 段提交重生成" })),
  mockAdopt: vi.fn(async () => ({ episode_id: "e1", entity_key: "prop:马克杯", adopted: [{ shot_id: "s2", version_id: "v2" }] })),
}));

vi.mock("../api", () => ({
  api: {
    getAssetRefreshReport: mockReport,
    regenerateAssetRefresh: mockRegenerate,
    adoptAssetRefreshGroup: mockAdopt,
  },
}));

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import AssetRefreshPanel from "./AssetRefreshPanel";

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === "string" ? c : textOf(c))).join("");
}

function emptyReport() {
  return { episode_id: "e1", groups: [], quota: { needs_regen_shot_count: 0, needs_regen_seconds: 0, account: null, enough: true } };
}

function reportWithNeedsRegen() {
  return {
    episode_id: "e1",
    groups: [{
      entity_key: "prop:马克杯", entity_type: "prop", entity_name: "马克杯",
      category: "added", category_label: "新增参考图",
      members: [{
        shot_id: "s1", shot_no: 1, duration_s: 15, status: "needs_regen",
        status_label: "需要重生成", reason: "新增参考图：马克杯", candidates: [], entity_category: "added",
      }],
      needs_regen_shot_ids: ["s1"], needs_regen_seconds: 15,
    }],
    quota: { needs_regen_shot_count: 1, needs_regen_seconds: 15, account: null, enough: true },
  };
}

function reportWithCandidateAndNeedsRegen() {
  return {
    episode_id: "e1",
    groups: [{
      entity_key: "prop:马克杯", entity_type: "prop", entity_name: "马克杯",
      category: "updated", category_label: "参考图已更新",
      members: [
        {
          shot_id: "s1", shot_no: 1, duration_s: 15, status: "needs_regen",
          status_label: "需要重生成", reason: "新增参考图：马克杯", candidates: [], entity_category: "updated",
        },
        {
          shot_id: "s2", shot_no: 2, duration_s: 15, status: "has_candidate",
          status_label: "已有按最新参考生成的成功候选（1 个）", reason: "", candidates: [{ version_id: "v2", created_at: 2 }],
          entity_category: "updated",
        },
      ],
      needs_regen_shot_ids: ["s1"], needs_regen_seconds: 15,
    }],
    quota: { needs_regen_shot_count: 1, needs_regen_seconds: 15, account: null, enough: true },
  };
}

function reportWithCandidate() {
  return {
    episode_id: "e1",
    groups: [{
      entity_key: "prop:马克杯", entity_type: "prop", entity_name: "马克杯",
      category: "updated", category_label: "参考图已更新",
      members: [{
        shot_id: "s2", shot_no: 2, duration_s: 15, status: "has_candidate",
        status_label: "已有按最新参考生成的成功候选（1 个）", reason: "", candidates: [{ version_id: "v2", created_at: 2 }],
        entity_category: "updated",
      }],
      needs_regen_shot_ids: [], needs_regen_seconds: 0,
    }],
    quota: { needs_regen_shot_count: 0, needs_regen_seconds: 0, account: null, enough: true },
  };
}

async function renderPanel() {
  const onToast = vi.fn();
  const onRefresh = vi.fn(async () => undefined);
  let view!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    view = TestRenderer.create(createElement(AssetRefreshPanel, { episodeId: "e1", onToast, onRefresh }));
  });
  return { view, onToast, onRefresh };
}

describe("参考资产已更新面板", () => {
  beforeEach(() => {
    mockReport.mockReset();
    mockRegenerate.mockClear();
    mockAdopt.mockClear();
  });

  it("没有任何分组时不渲染任何内容", async () => {
    mockReport.mockResolvedValue(emptyReport());
    const { view } = await renderPanel();
    expect(view.toJSON()).toBeNull();
  });

  it("需要重生成的段：重生成按钮可点，整组采用按钮因无候选而禁用", async () => {
    mockReport.mockResolvedValue(reportWithNeedsRegen());
    const { view } = await renderPanel();
    const text = textOf(view.root.findByProps({ "aria-label": "参考资产已更新" }));
    expect(text).toContain("马克杯");
    expect(text).toContain("需要重生成");
    const buttons = view.root.findAllByType("button");
    const regenerate = buttons.find(b => textOf(b).includes("重生成本组"))!;
    const adopt = buttons.find(b => textOf(b).includes("整组采用"))!;
    expect(regenerate.props.disabled).toBe(false);
    expect(adopt.props.disabled).toBe(true);
    act(() => view.unmount());
  });

  it("点击重生成本组只对该组的 entity_key 发起请求", async () => {
    mockReport.mockResolvedValue(reportWithNeedsRegen());
    const { view, onToast, onRefresh } = await renderPanel();
    const buttons = view.root.findAllByType("button");
    const regenerate = buttons.find(b => textOf(b).includes("重生成本组"))!;
    await act(async () => { await regenerate.props.onClick(); });
    expect(mockRegenerate).toHaveBeenCalledWith("e1", ["prop:马克杯"], expect.any(String));
    expect(onToast).toHaveBeenCalledWith("已为 1 段提交重生成", false);
    expect(onRefresh).toHaveBeenCalled();
    act(() => view.unmount());
  });

  it("有候选版本的段默认预选最新候选，整组采用按钮可点；点击后带上选中的版本号", async () => {
    mockReport.mockResolvedValue(reportWithCandidate());
    const { view, onToast } = await renderPanel();
    const buttons = view.root.findAllByType("button");
    const adopt = buttons.find(b => textOf(b).includes("整组采用"))!;
    expect(adopt.props.disabled).toBe(false);
    await act(async () => { await adopt.props.onClick(); });
    expect(mockAdopt).toHaveBeenCalledWith(
      "e1", "prop:马克杯", { s2: "v2" }, expect.stringContaining("马克杯"), expect.any(String),
    );
    expect(onToast).toHaveBeenCalledWith("已一次性替换 1 段的采用视频；旧版本保留可回退");
    act(() => view.unmount());
  });

  it("本组还有段需要重生成时，即便另一段已选好候选，整组采用按钮仍禁用且点击被拦住", async () => {
    // 2026-10-03 复现的真实缺陷：旧实现只看"有候选的段是否都选好版本"，没看
    // "本组是否还有 needs_regen 的段"——会在只换一部分段的情况下允许整组采
    // 用，制造跨段资产不一致（CLAUDE.md「不做部分采用」）。
    mockReport.mockResolvedValue(reportWithCandidateAndNeedsRegen());
    const { view, onToast } = await renderPanel();
    const buttons = view.root.findAllByType("button");
    const adopt = buttons.find(b => textOf(b).includes("整组采用"))!;
    expect(adopt.props.disabled).toBe(true);
    expect(adopt.props.title).toContain("还有 1 段尚未重生成");
    await act(async () => { await adopt.props.onClick(); });
    expect(mockAdopt).not.toHaveBeenCalled();
    expect(onToast).toHaveBeenCalledWith(expect.stringContaining("还有 1 段尚未重生成"), true);
    act(() => view.unmount());
  });
});
