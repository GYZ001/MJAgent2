import { describe, expect, it } from "vitest";
import {
  MODEL_KIND_LABELS,
  MODEL_KINDS,
  MODEL_ROWS,
  protocolHintText,
  protocolLabel,
} from "./constants";

// 声音生成（voice）是新增的第五种模型能力（只有自建服务商能声明，给短剧
// 角色配固定音色）。这里锁三件事，全部是纯函数/数据断言，不用渲染组件：
// 1) 能力勾选框、分配行、中文标签三处清单收敛后都带上 voice，不会漏一处；
// 2) 协议中文名——命中 protocol_hints 显示中文，没命中如实回退成原始协议
//    标识，不能编造中文也不能在前端写死协议清单；
// 3) 协议下方的填写提示按“服务地址/模型标识/说明”拼接，缺项不留空分隔符，
//    没有 hint 时返回空串（调用方据此决定要不要渲染这一行）。

describe("MODEL_KINDS 能力总清单", () => {
  it("包含声音生成，且与 MODEL_ROWS 的 key 集合完全一致", () => {
    expect(MODEL_KINDS).toContain("voice");
    expect([...MODEL_KINDS].sort()).toEqual(
      MODEL_ROWS.map((row) => row.key).sort(),
    );
  });

  it("MODEL_KIND_LABELS 覆盖 MODEL_KINDS 里的每一个能力，不出现 undefined", () => {
    for (const kind of MODEL_KINDS) {
      expect(MODEL_KIND_LABELS[kind]).toBeTruthy();
      expect(typeof MODEL_KIND_LABELS[kind]).toBe("string");
    }
  });

  it("声音生成的中文名是「声音生成」，分配行标签是「声音生成模型」", () => {
    expect(MODEL_KIND_LABELS.voice).toBe("声音生成");
    const voiceRow = MODEL_ROWS.find((row) => row.key === "voice");
    expect(voiceRow?.label).toBe("声音生成模型");
    expect(voiceRow?.note).toBeTruthy();
  });
});

describe("protocolLabel 协议中文名与回退", () => {
  it("命中 protocol_hints 时显示中文名", () => {
    expect(
      protocolLabel("qwen_voice_design", {
        qwen_voice_design: { label: "千问声音设计（阿里百炼）" },
      }),
    ).toBe("千问声音设计（阿里百炼）");
  });

  it("没有 hints、或 hints 里查不到这个协议时，原样显示协议标识", () => {
    // 没有 hints 参数（老协议、后端还没给 hint）——不能因此显示空白或 undefined。
    expect(protocolLabel("seedance", undefined)).toBe("seedance");
    // hints 存在但查不到这个协议——同样原样回退，不能编造中文名。
    expect(
      protocolLabel("openai", { qwen_voice_design: { label: "千问声音设计" } }),
    ).toBe("openai");
  });
});

describe("protocolHintText 协议填写提示", () => {
  it("服务地址示例/模型标识示例/说明三段都在时依次拼接", () => {
    const text = protocolHintText({
      label: "千问声音设计（阿里百炼）",
      base_url_example: "https://dashscope.aliyuncs.com/api/v1",
      model_example: "qwen3-tts-vd-2026-01-26",
      note: "模型标识填声音设计的目标合成模型",
    });
    expect(text).toContain("https://dashscope.aliyuncs.com/api/v1");
    expect(text).toContain("qwen3-tts-vd-2026-01-26");
    expect(text).toContain("模型标识填声音设计的目标合成模型");
  });

  it("缺项时跳过、不留空分隔符（比如没有 note）", () => {
    const text = protocolHintText({
      label: "示例协议",
      base_url_example: "https://example.test/v1",
    });
    expect(text).toBe("服务地址示例：https://example.test/v1");
    expect(text.endsWith("；")).toBe(false);
  });

  it("没有 hint 时返回空串，调用方据此不渲染提示行", () => {
    expect(protocolHintText(undefined)).toBe("");
  });
});
