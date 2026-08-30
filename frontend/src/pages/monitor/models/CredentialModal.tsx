import type { MutableRefObject } from "react";
import type { CatalogModel } from "../../../api";
import { modelBusinessLabel } from "./constants";

export default function CredentialModal({
  credential,
  credentialDraft,
  onDraftChange,
  testing,
  credentialSaving,
  credentialTestDisabledReason,
  credentialSaveDisabledReason,
  onClose,
  modalRef,
  onTest,
  onSave,
}: {
  credential: CatalogModel;
  credentialDraft: { base_url: string; api_key: string };
  onDraftChange: (next: { base_url: string; api_key: string }) => void;
  testing: boolean;
  credentialSaving: boolean;
  credentialTestDisabledReason: string;
  credentialSaveDisabledReason: string;
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
        aria-labelledby="credential-title"
        ref={(node) => {
          modalRef.current = node;
        }}
      >
        <div className="model-modal-head">
          <div>
            <span className="eyebrow">模型连接</span>
            <h2 id="credential-title">{modelBusinessLabel(credential.label)} 的连接</h2>
            <p>密钥留空表示不修改现有值；接口不会回显明文。</p>
          </div>
          <button
            className="model-modal-close"
            onClick={onClose}
            aria-label="关闭连接配置"
          >
            ×
          </button>
        </div>
        <div className="model-form-grid">
          <label className="model-form-field model-form-wide">
            <span>服务地址</span>
            <input
              value={credentialDraft.base_url}
              onChange={(e) => onDraftChange({ ...credentialDraft, base_url: e.target.value })}
            />
          </label>
          <label className="model-form-field model-form-wide">
            <span>该模型专用访问密钥</span>
            <input
              type="password"
              autoComplete="new-password"
              value={credentialDraft.api_key}
              placeholder={
                credential.key_configured
                  ? "留空则不修改现有密钥"
                  : "输入访问密钥"
              }
              onChange={(e) => onDraftChange({ ...credentialDraft, api_key: e.target.value })}
            />
          </label>
        </div>
        <div className="model-modal-actions">
          <button
            disabled={Boolean(credentialTestDisabledReason)}
            aria-label={credentialTestDisabledReason ? `测试连接，暂不可用：${credentialTestDisabledReason}` : `测试 ${modelBusinessLabel(credential.label)} 的当前连接`}
            title={credentialTestDisabledReason || "测试不会保存地址或密钥"}
            onClick={onTest}
          >
            {testing ? "测试中…" : "测试连接"}
          </button>
          <button
            className="btn primary small"
            disabled={Boolean(credentialSaveDisabledReason)}
            aria-label={credentialSaveDisabledReason ? `保存连接，暂不可用：${credentialSaveDisabledReason}` : `保存 ${modelBusinessLabel(credential.label)} 的连接`}
            title={credentialSaveDisabledReason || "保存后该模型将使用当前连接配置"}
            onClick={onSave}
          >
            {credentialSaving ? "保存中…" : "保存连接"}
          </button>
        </div>
      </section>
    </div>
  );
}
