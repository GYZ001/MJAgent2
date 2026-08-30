import type { MutableRefObject } from "react";
import type { CatalogModel, ModelCatalog, ModelKind } from "../../../api";
import SearchField from "../../../components/SearchField";
import {
  MODEL_KIND_LABELS,
  MODEL_ROWS,
  PROVIDER_LABELS,
  formatTokenCapacity,
  modelBusinessLabel,
  tokenLimitSourceLabel,
} from "./constants";

export type ModelTestState = { state: "testing" | "ok" | "fail"; note?: string };

/** 模型库弹窗——从 ModelCenter 里拆出来，纯展示 + 转发事件，不持有自己的
 *  草稿状态（草稿属于新建/编辑/连接弹窗，由 ModelsSection 统一持有）。 */
export default function LibraryModal({
  catalog,
  search,
  onSearchChange,
  provider,
  onProviderChange,
  capability,
  onCapabilityChange,
  connection,
  onConnectionChange,
  groupedModels,
  testStates,
  onClose,
  modalRef,
  onTest,
  onConfigureConnection,
  onEdit,
  onDeleteRequest,
}: {
  catalog: ModelCatalog | null;
  search: string;
  onSearchChange: (value: string) => void;
  provider: string;
  onProviderChange: (value: string) => void;
  capability: string;
  onCapabilityChange: (value: string) => void;
  connection: string;
  onConnectionChange: (value: string) => void;
  groupedModels: Record<string, CatalogModel[]>;
  testStates: Record<string, ModelTestState>;
  onClose: () => void;
  modalRef: MutableRefObject<HTMLElement | null>;
  onTest: (item: CatalogModel) => void;
  onConfigureConnection: (item: CatalogModel, trigger: HTMLElement) => void;
  onEdit: (item: CatalogModel, trigger: HTMLElement) => void;
  onDeleteRequest: (item: CatalogModel, trigger: HTMLElement) => void;
}) {
  return (
    <div
      className="model-modal-backdrop"
      role="presentation"
      onMouseDown={(e) => {
        if (e.currentTarget === e.target) onClose();
      }}
    >
      <section
        className="model-modal model-library-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="library-title"
        ref={(node) => {
          modalRef.current = node;
        }}
      >
        <div className="model-modal-head">
          <div>
            <span className="eyebrow">模型库</span>
            <h2 id="library-title">管理模型</h2>
            <p>搜索、分组与连接状态筛选不会修改模型数据。</p>
          </div>
          <button
            className="model-modal-close"
            onClick={onClose}
            aria-label="关闭模型库"
          >
            ×
          </button>
        </div>
        <div className="monitor-toolbar">
          <SearchField
            value={search}
            onChange={onSearchChange}
            placeholder="搜索模型名称或技术标识"
            ariaLabel="搜索模型库"
          />
          <label>
            <span>服务商</span>
            <select
              aria-label="按服务商筛选模型"
              value={provider}
              onChange={(e) => onProviderChange(e.target.value)}
            >
              <option value="">全部</option>
              {Array.from(
                new Set(
                  (catalog?.items || []).map((item) => item.provider),
                ),
              ).map((item) => (
                <option key={item} value={item}>
                  {PROVIDER_LABELS[item] ||
                    catalog?.items.find((model) => model.provider === item)
                      ?.provider_label ||
                    "自定义服务"}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>能力</span>
            <select
              aria-label="按能力筛选模型"
              value={capability}
              onChange={(e) => onCapabilityChange(e.target.value)}
            >
              <option value="">全部</option>
              {MODEL_ROWS.map((row) => (
                <option key={row.key} value={row.key}>
                  {row.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>连接</span>
            <select
              aria-label="按连接状态筛选模型"
              value={connection}
              onChange={(e) => onConnectionChange(e.target.value)}
            >
              <option value="">全部</option>
              <option value="configured">已配置</option>
              <option value="pending">仅看待配置</option>
            </select>
          </label>
        </div>
        <div className="model-library-list">
          {Object.entries(groupedModels).map(([providerKey, items]) => (
            <section className="model-provider-group" key={providerKey}>
              <h3>
                {PROVIDER_LABELS[providerKey] ||
                  items[0]?.provider_label ||
                  providerKey}
                <small>{items.length} 个模型</small>
              </h3>
              {items.map((item) => {
                const test = testStates[item.id];
                return (
                  <div className="model-library-item" key={item.id}>
                    <div className="model-library-main">
                      <div>
                        <b>{modelBusinessLabel(item.label)}</b>
                        {!item.builtin && (
                          <span className="stamp gold">自定义</span>
                        )}
                      </div>
                      <code>
                        {PROVIDER_LABELS[item.provider] ||
                          item.provider_label ||
                          item.provider}
                      </code>
                      <span>{item.kinds.map((kind: ModelKind) => MODEL_KIND_LABELS[kind]).join(" / ")}</span>
                      {(item.kinds.includes("text") || item.kinds.includes("vlm")) && (
                        <span>
                          上下文 {formatTokenCapacity(item.context_window_tokens)} · 输出 {formatTokenCapacity(item.max_output_tokens)} · {tokenLimitSourceLabel(item.token_limits_source)}
                        </span>
                      )}
                      <details className="model-library-technical">
                        <summary>技术标识</summary>
                        <code>{item.model}</code>
                      </details>
                    </div>
                    <span
                      className={`stamp ${item.key_configured ? "green" : "red"}`}
                    >
                      {item.key_configured ? "连接已配置" : "待配置"}
                    </span>
                    <div className="model-library-actions">
                      <button
                        type="button"
                        aria-label={test?.state === "testing"
                          ? `测试 ${modelBusinessLabel(item.label)}，暂不可用：连接测试正在进行`
                          : `测试 ${modelBusinessLabel(item.label)}`}
                        disabled={test?.state === "testing"}
                        onClick={() => onTest(item)}
                      >
                        {test?.state === "testing"
                          ? "测试中…"
                          : test?.state === "ok"
                            ? `可用 · ${test.note}`
                            : test?.state === "fail"
                              ? "测试失败"
                              : "测试"}
                      </button>
                      <button
                        type="button"
                        aria-label={`配置 ${modelBusinessLabel(item.label)} 的连接`}
                        onClick={(event) => onConfigureConnection(item, event.currentTarget)}
                      >
                        连接
                      </button>
                      {!item.builtin && (
                        <button
                          type="button"
                          aria-label={`编辑 ${modelBusinessLabel(item.label)}`}
                          onClick={(event) => onEdit(item, event.currentTarget)}
                        >
                          编辑
                        </button>
                      )}
                      {!item.builtin && (
                        <button
                          type="button"
                          className="danger"
                          aria-label={`删除 ${modelBusinessLabel(item.label)}`}
                          onClick={(event) => onDeleteRequest(item, event.currentTarget)}
                        >
                          删除
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </section>
          ))}
        </div>
      </section>
    </div>
  );
}
