import type { MutableRefObject } from "react";
import type { CatalogModel, ModelCatalog, ModelKind } from "../../../api";
import { MODEL_KIND_LABELS } from "./constants";

export interface ModelDraft {
  label: string;
  provider_label: string;
  base_url: string;
  api_key: string;
  model: string;
  kinds: ModelKind[];
  protocol: string;
}

export default function NewModelModal({
  editingModel,
  modelDraft,
  onDraftChange,
  catalog,
  protocolOptions,
  newTesting,
  modelSaving,
  modelTestDisabledReason,
  modelSaveDisabledReason,
  onClose,
  modalRef,
  onTest,
  onSave,
}: {
  editingModel: CatalogModel | null;
  modelDraft: ModelDraft;
  onDraftChange: (next: ModelDraft) => void;
  catalog: ModelCatalog | null;
  protocolOptions: string[];
  newTesting: boolean;
  modelSaving: boolean;
  modelTestDisabledReason: string;
  modelSaveDisabledReason: string;
  onClose: () => void;
  modalRef: MutableRefObject<HTMLElement | null>;
  onTest: () => void;
  onSave: () => void;
}) {
  return (
    <div className="model-modal-backdrop" role="presentation">
      <section
        className="model-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-model-title"
        ref={(node) => {
          modalRef.current = node;
        }}
      >
        <div className="model-modal-head">
          <div>
            <span className="eyebrow">自定义模型</span>
            <h2 id="new-model-title">
              {editingModel ? "编辑自定义模型" : "添加自定义模型"}
            </h2>
            <p>
              所有模型都在模型库里，按所选接入协议对接；保存前必须通过当前
              配置的连接测试。
            </p>
          </div>
          <button
            className="model-modal-close"
            onClick={onClose}
            aria-label={editingModel ? "关闭编辑模型" : "关闭添加模型"}
          >
            ×
          </button>
        </div>
        <div className="model-form-grid">
          <label className="model-form-field">
            <span>显示名称（必填）</span>
            <input
              value={modelDraft.label}
              onChange={(e) => onDraftChange({ ...modelDraft, label: e.target.value })}
            />
          </label>
          <label className="model-form-field">
            <span>服务名称（必填）</span>
            <input
              value={modelDraft.provider_label}
              onChange={(e) => onDraftChange({ ...modelDraft, provider_label: e.target.value })}
            />
          </label>
          <label className="model-form-field model-form-wide">
            <span>服务地址（必填）</span>
            <input
              value={modelDraft.base_url}
              placeholder="https://…/v1"
              onChange={(e) => onDraftChange({ ...modelDraft, base_url: e.target.value })}
            />
            <small>
              只填到版本号（如 https://…/v1），不要带
              /chat/completions、/images/generations 等具体接口路径。
            </small>
          </label>
          <label className="model-form-field">
            <span>模型技术标识（必填）</span>
            <input
              value={modelDraft.model}
              onChange={(e) => onDraftChange({ ...modelDraft, model: e.target.value })}
            />
          </label>
          <label className="model-form-field">
            <span>访问密钥（{editingModel ? "留空则不修改" : "必填"}）</span>
            <input
              type="password"
              autoComplete="new-password"
              value={modelDraft.api_key}
              placeholder={
                editingModel ? "留空则不修改现有密钥" : "输入访问密钥"
              }
              onChange={(e) => onDraftChange({ ...modelDraft, api_key: e.target.value })}
            />
          </label>
          <fieldset className="model-form-field model-form-wide">
            <legend>模型能力</legend>
            {(["text", "vlm", "video", "image"] as ModelKind[]).map((kind) => (
              <label key={kind}>
                <input
                  type="checkbox"
                  checked={modelDraft.kinds.includes(kind)}
                  onChange={(e) => {
                    const kinds = e.target.checked
                      ? [...modelDraft.kinds, kind]
                      : modelDraft.kinds.filter((item) => item !== kind);
                    const allowed = new Set(
                      (["video", "image", "text", "vlm"] as ModelKind[])
                        .flatMap((item) =>
                          kinds.includes(item)
                            ? catalog?.media_protocols?.[item] ?? []
                            : [],
                        ),
                    );
                    onDraftChange({
                      ...modelDraft,
                      kinds,
                      // 改能力可能让已选协议不再适用（比如从文本改成视频），
                      // 这时必须清掉重选，不能带着一个对新能力无效的协议提交。
                      protocol: allowed.has(modelDraft.protocol)
                        ? modelDraft.protocol
                        : "",
                    });
                  }}
                />
                {MODEL_KIND_LABELS[kind]}
              </label>
            ))}
          </fieldset>
          {protocolOptions.length > 0 && (
            <label className="model-form-field model-form-wide">
              <span>接入协议</span>
              <select
                value={modelDraft.protocol}
                onChange={(e) => onDraftChange({ ...modelDraft, protocol: e.target.value })}
              >
                <option value="">请选择</option>
                {protocolOptions.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
              <small>
                代码里只实现协议，不内置模型；同一协议下换服务只要改地址和
                密钥。文本与视觉理解通常选 openai（OpenAI 兼容）。
              </small>
            </label>
          )}
        </div>
        <div className="model-modal-actions">
          <button
            disabled={Boolean(modelTestDisabledReason)}
            aria-label={modelTestDisabledReason ? `测试连接，暂不可用：${modelTestDisabledReason}` : "测试当前模型连接"}
            title={modelTestDisabledReason || "测试不会将模型加入模型库"}
            onClick={onTest}
          >
            {newTesting ? "测试中…" : "测试连接"}
          </button>
          <button
            className="btn primary small"
            disabled={Boolean(modelSaveDisabledReason)}
            aria-label={modelSaveDisabledReason ? `${editingModel ? "保存模型修改" : "添加到模型库"}，暂不可用：${modelSaveDisabledReason}` : editingModel ? "保存模型修改" : "添加到模型库"}
            title={modelSaveDisabledReason || "保存后可在模型分配中选择"}
            onClick={onSave}
          >
            {modelSaving ? "保存中…" : editingModel ? "保存模型修改" : "添加到模型库"}
          </button>
        </div>
      </section>
    </div>
  );
}
