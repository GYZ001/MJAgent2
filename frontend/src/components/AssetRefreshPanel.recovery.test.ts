import { createElement } from "react";
import TestRenderer, { act } from "react-test-renderer";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { AssetRefreshReport } from "../api/assetRefresh";

const { mockReport, mockRegenerate, mockAdopt } = vi.hoisted(() => ({
  mockReport: vi.fn(), mockRegenerate: vi.fn(), mockAdopt: vi.fn(),
}));
vi.mock("../api", async () => {
  const { ApiError } = await import("../api/client");
  return { ApiError, api: {
    getAssetRefreshReport: mockReport, regenerateAssetRefresh: mockRegenerate, adoptAssetRefreshGroup: mockAdopt,
  } };
});
import AssetRefreshPanel from "./AssetRefreshPanel";

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(child => typeof child === "string" ? child : textOf(child)).join("");
}

function report(episodeId = "e1", status?: "needs_regen" | "has_candidate"): AssetRefreshReport {
  return {
    episode_id: episodeId,
    groups: status ? [{
      entity_key: "prop:马克杯", entity_type: "prop", entity_name: "马克杯",
      category: "added", category_label: "新增参考图",
      members: [{
        shot_id: "s1", shot_no: 1, duration_s: 15, status, status_label: "待处理", reason: "",
        candidates: status === "has_candidate" ? [{ version_id: "v2", version_no: 2, created_at: 1, video_url: null }] : [],
        entity_category: "added", adopted_version_no: 1,
      }],
      needs_regen_shot_ids: status === "needs_regen" ? ["s1"] : [], needs_regen_seconds: 15,
    }] : [],
    quota: { needs_regen_shot_count: 0, needs_regen_seconds: 0, account: null, enough: true },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

async function renderPanel() {
  const callbacks = { onToast: vi.fn(), onRefresh: vi.fn(), onOpenBible: vi.fn() };
  let view!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    view = TestRenderer.create(createElement(AssetRefreshPanel, { episodeId: "e1", ...callbacks }));
  });
  return { view, ...callbacks };
}

describe("参考资产核对恢复与分集切换", () => {
  beforeEach(() => { mockReport.mockReset(); mockRegenerate.mockReset(); mockAdopt.mockReset(); });

  it("世界书未建立时提供恢复和重试入口", async () => {
    mockReport.mockRejectedValue(new ApiError(409, "世界书暂不可用，暂无法核对参考资产", "CON-409", undefined, "ERR-1", {
      code: "ASSET_REFRESH_BIBLE_NOT_READY", recommended_action: "open_bible",
    }));
    const { view, onToast, onOpenBible } = await renderPanel();
    const alert = view.root.findByProps({ role: "alert" });
    expect(textOf(alert)).toContain("世界书暂不可用");
    act(() => alert.findAllByType("button").find(button => textOf(button) === "前往世界书")!.props.onClick());
    expect(onOpenBible).toHaveBeenCalledOnce();
    expect(onToast).not.toHaveBeenCalled();
    mockReport.mockResolvedValue(report());
    await act(async () => { await alert.findAllByType("button").find(button => textOf(button) === "重试")!.props.onClick(); });
    expect(mockReport).toHaveBeenCalledTimes(2);
    expect(view.toJSON()).toBeNull();
    act(() => view.unmount());
  });

  it("损坏数据提示反馈错误编号并保留重试入口", async () => {
    mockReport.mockRejectedValue(new ApiError(409, "世界书数据已损坏，请反馈错误编号（ERR-1）", "CON-409", undefined, "ERR-1", {
      code: "ASSET_REFRESH_BIBLE_INVALID", recommended_action: "contact_support",
    }));
    const { view } = await renderPanel();
    expect(textOf(view.root.findByProps({ role: "alert" }))).toContain("ERR-1");
    expect(view.root.findAllByType("button").map(textOf)).toEqual(["重试"]);
    act(() => view.unmount());
  });

  it("普通读取失败保留重试入口，不显示世界书恢复按钮", async () => {
    mockReport.mockRejectedValue(new Error("资产核对暂时失败"));
    const { view, onToast } = await renderPanel();
    expect(textOf(view.root.findByProps({ role: "alert" }))).toContain("资产核对暂时失败");
    expect(view.root.findAllByType("button").map(textOf)).toEqual(["重试"]);
    expect(onToast).not.toHaveBeenCalled();
    act(() => view.unmount());
  });

  it.each(["成功", "失败"])("切换分集后忽略旧请求的%s结果", async outcome => {
    const oldRequest = deferred<AssetRefreshReport>();
    mockReport.mockReturnValueOnce(oldRequest.promise);
    const { view, ...callbacks } = await renderPanel();
    mockReport.mockResolvedValue(report("e2", "needs_regen"));
    await act(async () => {
      view.update(createElement(AssetRefreshPanel, { episodeId: "e2", ...callbacks }));
    });
    await act(async () => {
      if (outcome === "成功") oldRequest.resolve(report());
      else oldRequest.reject(new Error("旧分集读取失败"));
    });
    expect(view.root.findAllByProps({ role: "alert" })).toHaveLength(0);
    expect(textOf(view.root.findByProps({ "aria-label": "参考资产已更新" }))).toContain("马克杯");
    expect(callbacks.onToast).not.toHaveBeenCalled();
    act(() => view.unmount());
  });

  it("切换到尚未加载的分集时立即清除旧分集的操作按钮", async () => {
    mockReport.mockResolvedValueOnce(report("e1", "needs_regen"));
    const { view, ...callbacks } = await renderPanel();
    mockReport.mockReturnValue(new Promise(() => undefined));
    await act(async () => {
      view.update(createElement(AssetRefreshPanel, { episodeId: "e2", ...callbacks }));
    });
    expect(view.toJSON()).toBeNull();
    act(() => view.unmount());
  });

  it("组件卸载后旧请求失败不再弹出提示", async () => {
    const request = deferred<AssetRefreshReport>();
    mockReport.mockReturnValue(request.promise);
    const { view, onToast } = await renderPanel();
    act(() => view.unmount());
    await act(async () => { request.reject(new Error("旧请求失败")); });
    expect(onToast).not.toHaveBeenCalled();
  });

  it.each(["重生成", "采纳"])("%s 完成前切走，不向新分集写入提示或刷新结果", async operation => {
    const pending = deferred<void>();
    mockReport.mockResolvedValueOnce(report("e1", operation === "重生成" ? "needs_regen" : "has_candidate"));
    mockRegenerate.mockImplementation(async () => {
      await pending.promise;
      return { queued: ["s1"], errors: [], message: "已提交重生成" };
    });
    mockAdopt.mockImplementation(async () => { await pending.promise; return { adopted: [{ shot_id: "s1" }] }; });
    const { view, ...callbacks } = await renderPanel();
    const button = view.root.findAllByType("button").find(item => textOf(item).includes(operation === "重生成" ? "重生成本组" : "整组采用"))!;
    await act(async () => { button.props.onClick(); });
    mockReport.mockResolvedValue(report("e2"));
    await act(async () => { view.update(createElement(AssetRefreshPanel, { episodeId: "e2", ...callbacks })); });
    await act(async () => { pending.resolve(); });
    expect(callbacks.onToast).not.toHaveBeenCalled();
    expect(callbacks.onRefresh).not.toHaveBeenCalled();
    expect(mockReport.mock.calls.map(args => args[0])).toEqual(["e1", "e2"]);
    act(() => view.unmount());
  });
});
