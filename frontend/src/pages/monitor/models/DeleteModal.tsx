import type { MutableRefObject } from "react";
import type { CatalogModel } from "../../../api";
import { PROVIDER_LABELS } from "./constants";

export default function DeleteModelModal({
  deleteModel,
  deletingModel,
  modalRef,
  onClose,
  onConfirm,
}: {
  deleteModel: CatalogModel;
  deletingModel: boolean;
  modalRef: MutableRefObject<HTMLElement | null>;
  onClose: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="model-modal-backdrop" role="presentation" onMouseDown={event => {
      if (event.currentTarget === event.target && !deletingModel) onClose();
    }}>
      <section
        ref={modalRef}
        className="model-modal model-delete-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-delete-title"
      >
        <div className="model-modal-head">
          <div>
            <span className="eyebrow">删除模型</span>
            <h2 id="model-delete-title">删除「{deleteModel.label}」？</h2>
            <p>如果该模型仍被任何任务类型使用，系统会阻止删除并保留现有配置。</p>
          </div>
        </div>
        <dl>
          <div><dt>服务商</dt><dd>{PROVIDER_LABELS[deleteModel.provider] || deleteModel.provider_label || deleteModel.provider}</dd></div>
          <div><dt>模型</dt><dd>{deleteModel.model}</dd></div>
          <div><dt>能力</dt><dd>{deleteModel.kinds.join(" / ")}</dd></div>
        </dl>
        <div className="model-modal-actions">
          <button type="button" disabled={deletingModel}
            aria-label={deletingModel ? "保留模型，暂不可用：正在删除模型" : "保留模型，不执行删除"}
            onClick={onClose}>
            保留模型
          </button>
          <button type="button" className="danger" disabled={deletingModel}
            aria-label={deletingModel
              ? `确认删除 ${deleteModel.label}，暂不可用：删除请求正在处理`
              : `确认删除模型 ${deleteModel.label}`}
            onClick={onConfirm}>
            {deletingModel ? "删除中…" : "确认删除模型"}
          </button>
        </div>
      </section>
    </div>
  );
}
