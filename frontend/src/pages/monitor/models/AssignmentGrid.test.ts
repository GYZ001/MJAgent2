import React from "react";
import TestRenderer from "react-test-renderer";
import { describe, expect, it, vi } from "vitest";
import type { CatalogModel, Health, ModelCatalog, ModelKind, ModelSelection } from "../../../api";
import AssignmentGrid from "./AssignmentGrid";

// 声音生成刚上线时，模型库里大概率一个声音模型都没有（只有自建服务商能
// 声明，需要用户在「添加模型」里手填）。这一行分配格必须给出路，不能是一
// 个空下拉；同时要保证已有模型的职责（文本/视觉理解/视频/图像）不受影响，
// 照常渲染选择器——这不只是防voice的回归，也顺带补了此前四类 kind 在
// "模型库里一个都没有"这种全新部署场景下同样会出现空下拉的既有缺口。

function catalogModel(kind: ModelKind, id: string): CatalogModel {
  return {
    id, provider: `custom:${id}`, model: `${id}-model`, label: `${id} 模型`,
    kinds: [kind], builtin: false, key_configured: true,
  };
}

function selectionFor(id: ModelKind): ModelSelection {
  return {
    key: id, label: `${id} 模型`, provider: `custom:${id}`, model: `${id}-model`,
    options: [{ provider: `custom:${id}`, model: `${id}-model`, available: true }],
  };
}

/** 覆盖 text/vlm/video/image 四类都有模型的健康度，只让 voice 缺席——用来
 *  隔离验证"只有 voice 这一行是空态"，而不是测试环境本身处于全局加载中。
 *  故意不包含 voice 键，模拟后端这部分尚未接入 health 时的形态；用 cast
 *  声明这是刻意构造的防御性场景，不是数据不完整的笔误。 */
function healthMissingVoice(): Health {
  return {
    ok: true,
    models: {
      text: selectionFor("text"),
      vlm: selectionFor("vlm"),
      video: selectionFor("video"),
      image: selectionFor("image"),
    },
  } as unknown as Health;
}

function fullCatalog(): ModelCatalog {
  return {
    items: [
      catalogModel("text", "text"),
      catalogModel("vlm", "vlm"),
      catalogModel("video", "video"),
      catalogModel("image", "image"),
    ],
  };
}

function render(health: Health | null, catalog: ModelCatalog | null) {
  const renderer = TestRenderer.create(
    React.createElement(AssignmentGrid, {
      health, catalog, draft: {}, setDraft: vi.fn(), onConfigureConnection: vi.fn(),
    }),
  );
  return renderer;
}

/** 找 toJSON() 树里包含给定文字的 .model-row 容器，把这一行单独摘出来再
 *  校验——五行内容混在一整棵树里时，笼统的整树断言看不出"是不是恰好只有
 *  voice 这一行没有下拉"，容易把别的行的下拉误判成 voice 的。 */
function rowContaining(root: unknown, text: string): unknown {
  if (!root || typeof root !== "object") return undefined;
  const item = root as { type?: string; props?: { className?: string }; children?: unknown[] };
  if (item.type === "div" && item.props?.className === "model-row") {
    if (JSON.stringify(item).includes(text)) return item;
  }
  for (const child of item.children ?? []) {
    const found = rowContaining(child, text);
    if (found) return found;
  }
  return undefined;
}

describe("AssignmentGrid 声音生成模型行的空态", () => {
  it("health/catalog 都还没加载完时，每一行都显示加载中，不提前判定空态", () => {
    const renderer = render(null, null);
    const text = JSON.stringify(renderer.toJSON());
    expect(text).toContain("正在加载");
    expect(text).not.toContain("还没有");
  });

  it("模型库里没有声音模型、health 也没有 voice 键：给出「添加模型」的出路而不是空下拉", () => {
    const renderer = render(healthMissingVoice(), fullCatalog());
    const tree = renderer.toJSON();
    const voiceRow = rowContaining(tree, "声音生成模型");
    expect(voiceRow).toBeTruthy();
    const voiceRowText = JSON.stringify(voiceRow);
    expect(voiceRowText).toContain("还没有声音生成模型");
    expect(voiceRowText).toContain("添加模型");
    expect(voiceRowText).not.toContain('"type":"select"');
  });

  it("health 里 voice 键存在但 options 为空（比照现有四类 kind 的既有兜底形态）时同样给出路", () => {
    const health = healthMissingVoice();
    health.models = {
      ...health.models,
      voice: { key: "voice", label: "声音生成模型", provider: "", model: "", options: [] },
    };
    const renderer = render(health, fullCatalog());
    const voiceRowText = JSON.stringify(rowContaining(renderer.toJSON(), "声音生成模型"));
    expect(voiceRowText).toContain("还没有声音生成模型");
    expect(voiceRowText).not.toContain('"type":"select"');
  });

  it("已有模型的职责（文本）不受影响，照常渲染服务/模型选择器", () => {
    const renderer = render(healthMissingVoice(), fullCatalog());
    const textRowText = JSON.stringify(rowContaining(renderer.toJSON(), "文本模型"));
    expect(textRowText).toContain('"type":"select"');
    expect(textRowText).not.toContain("还没有");
  });
});
