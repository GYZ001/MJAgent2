import { createElement } from "react";
import TestRenderer, { act } from "react-test-renderer";
import { beforeEach, describe, expect, it, vi } from "vitest";

const { mockShotGenerate } = vi.hoisted(() => ({
  mockShotGenerate: vi.fn(async () => ({ reused: false, job_id: "job-1" })),
}));

vi.mock("../../api", () => ({ api: { shotGenerate: mockShotGenerate } }));

// eslint-disable-next-line import/first -- mock 必须先注册，import 必须在其后
import CritiqueRetakePanel from "./CritiqueRetakePanel";

function textOf(node: TestRenderer.ReactTestInstance): string {
  return node.children.map(c => (typeof c === "string" ? c : textOf(c))).join("");
}

function renderPanel(disabled = false) {
  const onToast = vi.fn();
  const onRefresh = vi.fn(async () => undefined);
  let view!: TestRenderer.ReactTestRenderer;
  act(() => {
    view = TestRenderer.create(createElement(CritiqueRetakePanel, {
      shotId: "s1", disabled, qualificationVersion: "qv1", onToast, onRefresh,
    }));
  });
  return { view, onToast, onRefresh };
}

describe("带意见重拍面板", () => {
  beforeEach(() => { mockShotGenerate.mockClear(); });

  it("默认折叠成一个按钮，不带任何输入框——不用模态弹窗", () => {
    const { view } = renderPanel();
    expect(view.root.findAllByType("textarea").length).toBe(0);
    const toggle = view.root.findByType("button");
    expect(textOf(toggle)).toContain("带意见重拍");
    act(() => view.unmount());
  });

  it("生成资格未通过时（disabled=true）折叠按钮跟着禁用", () => {
    const { view } = renderPanel(true);
    expect(view.root.findByType("button").props.disabled).toBe(true);
    act(() => view.unmount());
  });

  it("展开后给出额度/合规/不改台词三条如实提示，且不是模态弹窗（无遮罩）", () => {
    const { view } = renderPanel();
    act(() => { view.root.findByType("button").props.onClick(); });
    const text = textOf(view.root.findByProps({ "aria-label": "带意见重拍" }));
    expect(text).toContain("视频生成额度");
    expect(text).toContain("内容审核拒收");
    expect(text).toContain("不改台词、不写回分镜");
    act(() => view.unmount());
  });

  it("空文本或只有空白不能提交——提交按钮禁用且点击不触发请求", async () => {
    const { view } = renderPanel();
    act(() => { view.root.findByType("button").props.onClick(); });
    const submit = view.root.findByProps({ className: "btn primary wall-critique-submit" });
    expect(submit.props.disabled).toBe(true);
    const textarea = view.root.findByType("textarea");
    act(() => { textarea.props.onChange({ target: { value: "   " } }); });
    await act(async () => {
      await view.root.findByProps({ className: "btn primary wall-critique-submit" }).props.onClick();
    });
    expect(mockShotGenerate).not.toHaveBeenCalled();
    act(() => view.unmount());
  });

  it("提交去首尾空白后的一条意见，走 critique 追加通道，不是 prompt_override", async () => {
    const { view, onToast, onRefresh } = renderPanel();
    act(() => { view.root.findByType("button").props.onClick(); });
    const textarea = view.root.findByType("textarea");
    act(() => { textarea.props.onChange({ target: { value: "  光线再暗一点  " } }); });
    await act(async () => {
      await view.root.findByProps({ className: "btn primary wall-critique-submit" }).props.onClick();
    });
    expect(mockShotGenerate).toHaveBeenCalledTimes(1);
    const args = mockShotGenerate.mock.calls[0];
    expect(args[0]).toBe("s1");
    expect(args[1]).toBeUndefined(); // prompt_override 不动
    expect(args[2]).toBe(false); // reroll：critique 自身已足以产生新版本
    expect(args[3]).toEqual(["光线再暗一点"]);
    expect(args[4]).toBe("qv1");
    expect(onToast).toHaveBeenCalled();
    expect(onRefresh).toHaveBeenCalledTimes(1);
    // 提交成功后收起面板、清空文本框
    expect(view.root.findAllByType("textarea").length).toBe(0);
    act(() => view.unmount());
  });

  it("提交失败时把错误原样告知用户，面板不收起（方便再改一次意见重试）", async () => {
    mockShotGenerate.mockRejectedValueOnce(new Error("触发内容审核拒收"));
    const { view, onToast } = renderPanel();
    act(() => { view.root.findByType("button").props.onClick(); });
    const textarea = view.root.findByType("textarea");
    act(() => { textarea.props.onChange({ target: { value: "更血腥一点" } }); });
    await act(async () => {
      await view.root.findByProps({ className: "btn primary wall-critique-submit" }).props.onClick();
    });
    expect(onToast).toHaveBeenCalledWith("触发内容审核拒收", true);
    expect(view.root.findAllByType("textarea").length).toBe(1);
    act(() => view.unmount());
  });

  it("展开后 disabled 才变为 true（例如旁边「重新生成」先把该镜头打成处理中）：提交按钮跟着禁用，点击不发请求", async () => {
    const onToast = vi.fn();
    const onRefresh = vi.fn(async () => undefined);
    let view!: TestRenderer.ReactTestRenderer;
    act(() => {
      view = TestRenderer.create(createElement(CritiqueRetakePanel, {
        shotId: "s1", disabled: false, qualificationVersion: "qv1", onToast, onRefresh,
      }));
    });
    act(() => { view.root.findByType("button").props.onClick(); });
    const textarea = view.root.findByType("textarea");
    act(() => { textarea.props.onChange({ target: { value: "光线再暗一点" } }); });
    // 父组件按最新生成资格重算出 disabled=true，面板仍处于展开态
    act(() => {
      view.update(createElement(CritiqueRetakePanel, {
        shotId: "s1", disabled: true, qualificationVersion: "qv1", onToast, onRefresh,
      }));
    });
    const submit = view.root.findByProps({ className: "btn primary wall-critique-submit" });
    expect(submit.props.disabled).toBe(true);
    await act(async () => { await submit.props.onClick(); });
    expect(mockShotGenerate).not.toHaveBeenCalled();
    act(() => view.unmount());
  });

  it("reused=true 时按 reused_reason 诚实转述——in_flight 说的是仍在处理中，不是已复用成片", async () => {
    mockShotGenerate.mockResolvedValueOnce({ reused: true, active: true, reused_reason: "in_flight" });
    const { view, onToast } = renderPanel();
    act(() => { view.root.findByType("button").props.onClick(); });
    const textarea = view.root.findByType("textarea");
    act(() => { textarea.props.onChange({ target: { value: "光线再暗一点" } }); });
    await act(async () => {
      await view.root.findByProps({ className: "btn primary wall-critique-submit" }).props.onClick();
    });
    expect(onToast).toHaveBeenCalledWith("已有任务在处理中，未重复提交");
    act(() => view.unmount());
  });
});
