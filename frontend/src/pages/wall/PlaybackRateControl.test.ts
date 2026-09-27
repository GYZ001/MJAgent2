import { createElement } from "react";
import TestRenderer, { act } from "react-test-renderer";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockShotAdoptVersion } = vi.hoisted(() => ({
  mockShotAdoptVersion: vi.fn(async () => ({ adopted: "v1", reason: "ok" })),
}));

vi.mock("../../api", () => ({ api: { shotAdoptVersion: mockShotAdoptVersion } }));

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import PlaybackRateControl from "./PlaybackRateControl";

function renderControl(version: { id: string; version_no: number; playback_rate?: number | null } | null) {
  const onToast = vi.fn();
  const onRefresh = vi.fn(async () => undefined);
  let view!: TestRenderer.ReactTestRenderer;
  act(() => {
    view = TestRenderer.create(createElement(PlaybackRateControl, {
      shotId: "s1", version, qualificationVersion: "qv1", onToast, onRefresh,
    }));
  });
  return { view, onToast, onRefresh };
}

describe("生成台调速控件", () => {
  beforeEach(() => { mockShotAdoptVersion.mockClear(); });

  it("没有已采纳版本时不渲染任何东西（父组件先判好才传 version）", () => {
    const { view } = renderControl(null);
    expect(view.toJSON()).toBeNull();
    act(() => view.unmount());
  });

  it("有已采纳版本时渲染选倍速控件，默认值取该版本已落库的 playback_rate", () => {
    const { view } = renderControl({ id: "v1", version_no: 1, playback_rate: 1.5 });
    const select = view.root.findByType("select");
    expect(select.props.value).toBe(1.5);
    act(() => view.unmount());
  });

  it("该版本从未设置过倍速（playback_rate 缺失）时默认 1x，不编造别的值", () => {
    const { view } = renderControl({ id: "v1", version_no: 1 });
    expect(view.root.findByType("select").props.value).toBe(1);
    act(() => view.unmount());
  });

  it("文案如实写明不重新生成、不消耗视频额度", () => {
    const { view } = renderControl({ id: "v1", version_no: 1, playback_rate: 1 });
    function textOf(node: TestRenderer.ReactTestInstance): string {
      return node.children.map(c => (typeof c === "string" ? c : textOf(c))).join("");
    }
    const hint = textOf(view.root.findByType("small"));
    expect(hint).toContain("不重新生成");
    expect(hint).toContain("不消耗视频额度");
    act(() => view.unmount());
  });

  it("选倍速后点应用：调用 shotAdoptVersion 时带上选中的 version_id 与倍速，理由自动生成且不少于 4 字", async () => {
    const { view, onToast, onRefresh } = renderControl({ id: "v7", version_no: 7, playback_rate: 1 });
    act(() => {
      view.root.findByType("select").props.onChange({ target: { value: "1.5" } });
    });
    await act(async () => {
      await view.root.findByProps({ className: "btn wall-playback-rate-apply" }).props.onClick();
    });
    expect(mockShotAdoptVersion).toHaveBeenCalledTimes(1);
    const [shotId, versionId, reason, qualificationVersion, , playbackRate] = mockShotAdoptVersion.mock.calls[0];
    expect(shotId).toBe("s1");
    expect(versionId).toBe("v7");
    expect(reason.length).toBeGreaterThanOrEqual(4);
    expect(qualificationVersion).toBe("qv1");
    expect(playbackRate).toBe(1.5);
    expect(onToast).toHaveBeenCalled();
    expect(onRefresh).toHaveBeenCalledTimes(1);
    act(() => view.unmount());
  });

  it("应用失败时把错误原样告知用户", async () => {
    mockShotAdoptVersion.mockRejectedValueOnce(new Error("采用理由太短"));
    const { view, onToast } = renderControl({ id: "v1", version_no: 1, playback_rate: 1 });
    await act(async () => {
      await view.root.findByProps({ className: "btn wall-playback-rate-apply" }).props.onClick();
    });
    expect(onToast).toHaveBeenCalledWith("采用理由太短", true);
    act(() => view.unmount());
  });
});
