import React from "react";
import TestRenderer, { act } from "react-test-renderer";
import { describe, expect, it, vi } from "vitest";
import type { ModelCatalog } from "../../../api";
import NewModelModal, { type ModelDraft } from "./NewModelModal";

// 「添加模型」弹窗要能声明声音生成能力，协议下拉要显示中文名（没有 hint 的
// 协议原样显示标识），选中带 hint 的协议时要在下方给出可照抄的中文填写
// 提示。这些都是纯渲染逻辑，用 react-test-renderer 真挂载覆盖（不是纯函数，
// 纯函数部分已经在 constants.test.ts 里测过了）。

const catalog: ModelCatalog = {
  items: [],
  media_protocols: { voice: ["qwen_voice_design", "minimax_voice_design"] },
  protocol_hints: {
    qwen_voice_design: {
      label: "千问声音设计（阿里百炼）",
      base_url_example: "https://dashscope.aliyuncs.com/api/v1",
      model_example: "qwen3-tts-vd-2026-01-26",
      note: "模型标识填声音设计的目标合成模型",
    },
  },
};

function draft(overrides: Partial<ModelDraft> = {}): ModelDraft {
  return {
    label: "",
    provider_label: "",
    base_url: "",
    api_key: "",
    model: "",
    kinds: ["voice"],
    protocol: "",
    ...overrides,
  };
}

async function renderModal(
  draftOverrides: Partial<ModelDraft> = {},
  protocolOptions: string[] = [],
): Promise<TestRenderer.ReactTestRenderer> {
  let renderer!: TestRenderer.ReactTestRenderer;
  await act(async () => {
    renderer = TestRenderer.create(
      React.createElement(NewModelModal, {
        editingModel: null,
        modelDraft: draft(draftOverrides),
        onDraftChange: vi.fn(),
        catalog,
        protocolOptions,
        newTesting: false,
        modelSaving: false,
        modelTestDisabledReason: "",
        modelSaveDisabledReason: "",
        onClose: vi.fn(),
        modalRef: { current: null },
        onTest: vi.fn(),
        onSave: vi.fn(),
      }),
    );
  });
  return renderer;
}

/** 找 toJSON() 树里 value 等于给定值的 <option>，返回它的可见文本。
 *  react-test-renderer 的 toJSON() 会把 children 从 props 里摘出来单放一个
 *  字段，这里按官方形状读，不靠整树 JSON.stringify 模糊匹配——那样查不出
 *  "value 对但显示文字被写错"这类回归（value 和显示文字恰好都含协议标识时
 *  尤其容易漏判）。 */
function optionText(node: unknown, value: string): string | undefined {
  if (!node || typeof node !== "object") return undefined;
  const item = node as { type?: string; props?: { value?: string }; children?: unknown[] };
  if (item.type === "option" && item.props?.value === value) {
    return (item.children ?? [])
      .filter((c): c is string => typeof c === "string")
      .join("");
  }
  for (const child of item.children ?? []) {
    const found = optionText(child, value);
    if (found !== undefined) return found;
  }
  return undefined;
}

function treeText(renderer: TestRenderer.ReactTestRenderer): string {
  return JSON.stringify(renderer.toJSON());
}

describe("NewModelModal 声音生成能力接入", () => {
  it("模型能力勾选框出现「声音生成」", async () => {
    const renderer = await renderModal();
    expect(treeText(renderer)).toContain("声音生成");
  });

  it("协议下拉：命中 protocol_hints 的显示中文名，value 仍是原始协议标识", async () => {
    const renderer = await renderModal({}, ["qwen_voice_design", "openai"]);
    const tree = renderer.toJSON();
    expect(optionText(tree, "qwen_voice_design")).toBe("千问声音设计（阿里百炼）");
  });

  it("协议下拉：没有 hint 的协议原样显示协议标识，不留空、不编造中文", async () => {
    const renderer = await renderModal({}, ["qwen_voice_design", "openai"]);
    const tree = renderer.toJSON();
    expect(optionText(tree, "openai")).toBe("openai");
  });

  it("选中带 hint 的协议时，协议下方给出服务地址/模型标识/说明的中文填写提示", async () => {
    const renderer = await renderModal(
      { protocol: "qwen_voice_design" },
      ["qwen_voice_design", "openai"],
    );
    const text = treeText(renderer);
    expect(text).toContain("https://dashscope.aliyuncs.com/api/v1");
    expect(text).toContain("qwen3-tts-vd-2026-01-26");
    expect(text).toContain("模型标识填声音设计的目标合成模型");
  });

  it("选中没有 hint 的协议时不编造填写提示，只保留通用说明", async () => {
    const renderer = await renderModal(
      { protocol: "openai" },
      ["qwen_voice_design", "openai"],
    );
    const text = treeText(renderer);
    expect(text).not.toContain("dashscope.aliyuncs.com");
    expect(text).toContain("代码里只实现协议");
  });
});
