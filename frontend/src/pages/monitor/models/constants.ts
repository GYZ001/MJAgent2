import type { CatalogModel, ModelKind, ModelSelection } from "../../../api";

export type ProviderKey = string;

export const MODEL_ROWS: Array<{ key: ModelKind; label: string; note: string }> = [
  { key: "text", label: "文本模型", note: "分集、映射包、分镜与文本修复" },
  { key: "vlm", label: "视觉理解模型", note: "定妆照、场景图与关键帧质检" },
  { key: "video", label: "视频模型", note: "首尾帧、参考图与视频输入生成" },
  { key: "image", label: "图像模型", note: "Seedream 参考图 / 定妆照" },
];
export const MODEL_KIND_LABELS: Record<ModelKind, string> = {
  text: "文本生成",
  vlm: "视觉理解",
  video: "视频生成",
  image: "图像生成",
};
export const PROVIDER_LABELS: Record<string, string> = {
  hiagent: "火山",
  minimax_h3: "MiniMax H3",
  openrouter: "OpenRouter",
  bailian: "百炼",
  deepseek: "DeepSeek",
  zhipu: "智谱",
};

export function modelBusinessLabel(value: string) {
  return value.trim().toLowerCase() === "text 模型" ? "文本模型" : value;
}

export function formatTokenCapacity(value?: number) {
  if (!value) return "待检测";
  if (value >= 1024 && value % 1024 === 0) return `${value / 1024}K`;
  return value.toLocaleString("zh-CN");
}

export function tokenLimitSourceLabel(source?: string) {
  if (source === "provider_metadata") return "供应商元数据";
  if (source === "configured") return "已配置";
  return "128K/32K 兼容默认";
}

/**
 * “available” 表示连接是否就绪，不表示服务商是否支持该职责。
 * 分配下拉必须保留待配置服务商，否则一个全新环境会把所有选项过滤成空白。
 */
export function modelProviderOptions(
  selection: ModelSelection,
  catalogItems: CatalogModel[],
  kind: ModelKind,
) {
  const providersWithModels = new Set(
    catalogItems
      .filter((item) => item.kinds.includes(kind))
      .map((item) => item.provider),
  );
  const seen = new Set<string>();
  return selection.options
    .filter(
      (option) =>
        providersWithModels.has(option.provider) ||
        option.provider === selection.provider,
    )
    .filter((option) => {
      if (seen.has(option.provider)) return false;
      seen.add(option.provider);
      return true;
    })
    .map((option) => ({
      ...option,
      available:
        option.available ||
        catalogItems.some(
          (item) =>
            item.provider === option.provider &&
            item.kinds.includes(kind) &&
            item.key_configured,
        ),
    }));
}

export function modelAssignmentValue(
  selection: ModelSelection,
  catalogItems: CatalogModel[],
  kind: ModelKind,
  provider: ProviderKey,
  draftModel?: string,
) {
  const models = catalogItems.filter(
    (item) => item.provider === provider && item.kinds.includes(kind),
  );
  const inCatalog = (model: string | undefined) =>
    Boolean(model && models.some((item) => item.model === model));
  if (draftModel !== undefined && inCatalog(draftModel)) return draftModel;
  if (provider === selection.provider && inCatalog(selection.model))
    return selection.model;

  const providerDefault = selection.options.find(
    (option) => option.provider === provider,
  )?.model;
  const configuredDefault = models.find((item) => item.key_configured)?.model;
  if (configuredDefault) return configuredDefault;
  if (inCatalog(providerDefault)) return providerDefault || "";
  return models[0]?.model || draftModel || providerDefault || "";
}

export function modelAssignmentSettingKey(
  provider: ProviderKey,
  kind: ModelKind,
) {
  return provider.startsWith("custom:") ? null : `${provider}_model_${kind}`;
}
