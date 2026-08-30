import type { CatalogModel, Health, ModelCatalog } from "../../../api";
import {
  MODEL_ROWS,
  PROVIDER_LABELS,
  modelAssignmentSettingKey,
  modelAssignmentValue,
  modelBusinessLabel,
  modelProviderOptions,
} from "./constants";

/** 四类模型职责的分配网格——从 ModelCenter 里拆出来，纯展示 + 转发事件，草稿
 *  状态（draft）仍由 ModelsSection 持有并通过 setDraft 回调下发变更。 */
export default function AssignmentGrid({
  health,
  catalog,
  draft,
  setDraft,
  onConfigureConnection,
}: {
  health: Health | null;
  catalog: ModelCatalog | null;
  draft: Record<string, string>;
  setDraft: (updater: (current: Record<string, string>) => Record<string, string>) => void;
  onConfigureConnection: (item: CatalogModel, trigger: HTMLElement) => void;
}) {
  const catalogLabel = (providerKey: string, model: string) =>
    modelBusinessLabel(catalog?.items.find(
      (item) => item.provider === providerKey && item.model === model,
    )?.label || model || "未配置");
  return (
    <div className="model-grid">
      {MODEL_ROWS.map((row) => {
        const selection = health?.models?.[row.key];
        if (!selection)
          return (
            <div className="monitor-loading" key={row.key}>
              正在加载 {row.label}…
            </div>
          );
        const providerKey =
          draft[`model_${row.key}_provider`] ?? selection.provider;
        const options = modelProviderOptions(
          selection,
          catalog?.items || [],
          row.key,
        );
        // 模型库里一条模型就是一个 provider，所以「服务」按服务名称分组，
        // 否则同一家服务下有两个模型时会在下拉里出现两个同名项。
        const serviceOf = (provider: string) =>
          catalog?.items.find((item) => item.provider === provider)
            ?.provider_label || provider;
        const currentService = serviceOf(providerKey);
        const services = Array.from(
          new Map(
            options.map((option) => [serviceOf(option.provider), option]),
          ).entries(),
        );
        const models =
          catalog?.items.filter(
            (item) =>
              item.kinds.includes(row.key) &&
              serviceOf(item.provider) === currentService,
          ) || [];
        const modelDraftKey = modelAssignmentSettingKey(
          providerKey,
          row.key,
        );
        const currentModel = modelAssignmentValue(
          selection,
          catalog?.items || [],
          row.key,
          providerKey,
          modelDraftKey ? draft[modelDraftKey] : undefined,
        );
        const selectedModel = models.find(
          (item) => item.provider === providerKey,
        );
        const runningModel = catalog?.items.find(
          (item) =>
            item.provider === selection.provider &&
            item.model === selection.model &&
            item.kinds.includes(row.key),
        );
        const runningReady = Boolean(
          runningModel?.key_configured ||
            selection.options.find(
              (option) => option.provider === selection.provider,
            )?.available,
        );
        return (
          <div className="model-row" key={row.key}>
            <div className="model-name">
              <span
                className={`model-kind-icon ${row.key}`}
                aria-hidden="true"
              >
                {row.key[0].toUpperCase()}
              </span>
              <b>{row.label}</b>
              <span>{row.note}</span>
            </div>
            <div className="model-selects">
              <label className="model-select-field">
                <span>服务</span>
                <select
                  aria-label={
                    options.length <= 1
                      ? `${row.label}服务商，当前只有一个受支持的服务`
                      : `${row.label}服务商`
                  }
                  value={currentService}
                  disabled={services.length <= 1}
                  onChange={(e) => {
                    const nextService = e.target.value;
                    const nextProvider =
                      (catalog?.items.find(
                        (item) =>
                          item.kinds.includes(row.key) &&
                          serviceOf(item.provider) === nextService,
                      )?.provider) || providerKey;
                    const nextModel = modelAssignmentValue(
                      selection,
                      catalog?.items || [],
                      row.key,
                      nextProvider,
                    );
                    setDraft((value) => {
                      const next = {
                        ...value,
                        [`model_${row.key}_provider`]: nextProvider,
                      };
                      const nextModelKey = modelAssignmentSettingKey(
                        nextProvider,
                        row.key,
                      );
                      if (nextModelKey) next[nextModelKey] = nextModel;
                      return next;
                    });
                  }}
                >
                  {services.map(([service, option]) => (
                    <option key={service} value={service}>
                      {service}
                      {option.available ? "" : "（待配置）"}
                    </option>
                  ))}
                </select>
              </label>
              <label className="model-select-field">
                <span>模型</span>
                <select
                  aria-label={`${row.label}目标模型`}
                  value={providerKey}
                  disabled={models.length <= 1}
                  onChange={(e) => {
                    setDraft((value) => ({
                      ...value,
                      [`model_${row.key}_provider`]: e.target.value,
                    }));
                  }}
                >
                  {models.length ? (
                    models.map((item) => (
                      <option key={item.id} value={item.provider}>
                        {modelBusinessLabel(item.label)}
                        {item.key_configured ? "" : "（待配置）"}
                      </option>
                    ))
                  ) : (
                    <option value={providerKey}>
                      {catalogLabel(providerKey, currentModel)}
                    </option>
                  )}
                </select>
              </label>
              <div
                className={`model-target-status ${selectedModel?.key_configured ? "ready" : "pending"}`}
              >
                <span>
                  {selectedModel?.key_configured
                    ? "所选模型连接已配置"
                    : selectedModel
                      ? "所选模型待配置；配置后才能保存分配"
                      : "当前服务下没有可分配模型"}
                </span>
                {selectedModel && !selectedModel.key_configured && (
                  <button
                    type="button"
                    aria-label={`配置 ${modelBusinessLabel(selectedModel.label)} 的连接`}
                    onClick={(event) => onConfigureConnection(selectedModel, event.currentTarget)}
                  >
                    配置连接
                  </button>
                )}
              </div>
            </div>
            <div className="model-current">
              <span
                className={`model-live-dot ${runningReady ? "" : "pending"}`}
              />
              {runningReady ? "当前运行" : "当前分配未就绪"}
              <strong>
                {PROVIDER_LABELS[selection.provider] ||
                  catalog?.items.find(
                    (item) => item.provider === selection.provider,
                  )?.provider_label ||
                  "自定义服务"}{" "}
                · {catalogLabel(selection.provider, selection.model)}
              </strong>
              <details className="model-assignment-technical">
                <summary>技术标识</summary>
                <code>{selection.model}</code>
              </details>
              <small>
                保存后：新任务与尚未启动的排队任务使用新分配；运行中任务保持启动快照。
              </small>
            </div>
          </div>
        );
      })}
    </div>
  );
}
