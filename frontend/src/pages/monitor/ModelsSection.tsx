import { api } from "../../api";
import type { Health, ModelCatalog, SettingsView } from "../../api";
import DecisionDialog from "../../components/DecisionDialog";
import AssignmentGrid from "./models/AssignmentGrid";
import CredentialModal from "./models/CredentialModal";
import DeleteModelModal from "./models/DeleteModal";
import LibraryModal from "./models/LibraryModal";
import NewModelModal from "./models/NewModelModal";
import { modelBusinessLabel } from "./models/constants";
import { useModelCenterState } from "./models/useModelCenterState";

export default function ModelCenter({
  health,
  catalog,
  settings,
  refreshHealth,
  refreshCatalog,
  refreshSettings,
  toast,
}: {
  health: Health | null;
  catalog: ModelCatalog | null;
  settings: SettingsView | null;
  refreshHealth: () => Promise<Health | null>;
  refreshCatalog: () => Promise<ModelCatalog | null>;
  refreshSettings: () => Promise<SettingsView | null>;
  toast: (message: string, error?: boolean) => void;
}) {
  const s = useModelCenterState({
    health, catalog, settings, refreshHealth, refreshCatalog, refreshSettings, toast,
  });
  return (
    <section className="card model-hub monitor-section">
      <div className="model-hub-head">
        <div>
          <h3>模型中心</h3>
          <p>四类职责、友好名称与生效范围清晰可见。</p>
        </div>
        <div className="model-hub-actions">
          <button className="btn ghost small" onClick={(event) => {
            s.libraryTriggerRef.current = event.currentTarget;
            s.setLibrary(true);
          }}>
            管理模型库
          </button>
          <button
            className="btn primary small"
            onClick={(event) => {
              s.modelDialogTriggerRef.current = event.currentTarget;
              s.setEditingModel(null);
              s.setModelDraft({
                label: "",
                provider_label: "",
                base_url: "",
                api_key: "",
                model: "",
                kinds: ["text"],
                protocol: "",
              });
              s.setNewTested("");
              s.setNewTokenLimits({});
              s.setNewModel(true);
            }}
          >
            添加模型
          </button>
        </div>
      </div>
      <AssignmentGrid
        health={health}
        catalog={catalog}
        draft={s.draft}
        setDraft={s.setDraft}
        onConfigureConnection={s.openConnectionDialog}
      />
      <div className="model-actions">
        <span>
          {Object.keys(s.assignmentPatch).length
            ? `${Object.keys(s.assignmentPatch).length} 项未保存分配`
            : "没有未保存分配"}
        </span>
        <button
          className="btn primary small"
          disabled={Boolean(s.assignmentSaveDisabledReason)}
          aria-label={s.assignmentSaveDisabledReason ? `保存模型分配，暂不可用：${s.assignmentSaveDisabledReason}` : `保存 ${s.assignmentAffectedRows.length} 类模型分配`}
          title={s.assignmentSaveDisabledReason || "保存前会再次说明对新任务、排队任务和运行中任务的影响"}
          onClick={() => s.setAssignmentConfirm(true)}
        >
          {s.saving ? "保存中…" : "保存模型分配"}
        </button>
      </div>
      {s.library && (
        <LibraryModal
          catalog={catalog}
          search={s.search}
          onSearchChange={s.setSearch}
          provider={s.provider}
          onProviderChange={s.setProvider}
          capability={s.capability}
          onCapabilityChange={s.setCapability}
          connection={s.connection}
          onConnectionChange={s.setConnection}
          groupedModels={s.groupedModels}
          testStates={s.testStates}
          onClose={() => s.setLibrary(false)}
          modalRef={s.libraryRef}
          onTest={(item) => void s.testModel(item)}
          onConfigureConnection={s.openConnectionDialog}
          onEdit={(item, trigger) => {
            s.modelDialogTriggerRef.current = trigger;
            s.setEditingModel(item);
            s.setModelDraft({
              label: item.label,
              provider_label: item.provider_label || item.provider,
              base_url: item.base_url || "",
              api_key: "",
              model: item.model,
              kinds: item.kinds,
              protocol: item.protocol || "",
            });
            s.setNewTested("");
            s.setNewTokenLimits({
              context_window_tokens: item.context_window_tokens,
              max_output_tokens: item.max_output_tokens,
              token_limits_source: item.token_limits_source,
            });
          }}
          onDeleteRequest={(item, trigger) => {
            s.modelDialogTriggerRef.current = trigger;
            s.setDeleteModel(item);
          }}
        />
      )}
      {s.credential && (
        <CredentialModal
          credential={s.credential}
          credentialDraft={s.credentialDraft}
          onDraftChange={(next) => {
            s.setCredentialDraft(next);
            s.setTestedSignature("");
          }}
          testing={s.testing}
          credentialSaving={s.credentialSaving}
          credentialTestDisabledReason={s.credentialTestDisabledReason}
          credentialSaveDisabledReason={s.credentialSaveDisabledReason}
          onClose={() => s.setCredential(null)}
          modalRef={s.credentialRef}
          onTest={async () => {
            s.setTesting(true);
            try {
              await api.testModel(s.credential!.id, s.credentialDraft);
              s.setTestedSignature(s.credentialSignature);
              toast(`${modelBusinessLabel(s.credential!.label)} 连接测试通过`);
            } catch (e) {
              toast((e as Error).message, true);
            } finally {
              s.setTesting(false);
            }
          }}
          onSave={async () => {
            s.setCredentialSaving(true);
            try {
              await api.saveModelCredentials(
                s.credential!.id,
                { ...s.credentialDraft, confirm: true },
              );
              await refreshCatalog();
              const label = modelBusinessLabel(s.credential!.label);
              s.setCredential(null);
              toast(`${label} 的连接已保存`);
            } catch (e) {
              toast((e as Error).message, true);
            } finally {
              s.setCredentialSaving(false);
            }
          }}
        />
      )}
      {(s.newModel || s.editingModel) && (
        <NewModelModal
          editingModel={s.editingModel}
          modelDraft={s.modelDraft}
          onDraftChange={(next) => {
            s.setModelDraft(next);
            s.setNewTested("");
          }}
          catalog={catalog}
          protocolOptions={s.protocolOptions}
          newTesting={s.newTesting}
          modelSaving={s.modelSaving}
          modelTestDisabledReason={s.modelTestDisabledReason}
          modelSaveDisabledReason={s.modelSaveDisabledReason}
          onClose={() => {
            s.setNewModel(false);
            s.setEditingModel(null);
          }}
          modalRef={s.newRef}
          onTest={async () => {
            s.setNewTesting(true);
            try {
              const result = s.editingModel
                ? await api.testModel(s.editingModel.id, s.modelDraft)
                : await api.testNewModel(s.modelDraft);
              s.setNewTokenLimits({
                context_window_tokens: result.context_window_tokens,
                max_output_tokens: result.max_output_tokens,
                token_limits_source: result.token_limits_source,
              });
              s.setNewTested(s.newSignature);
              toast("连接测试通过");
            } catch (e) {
              toast((e as Error).message, true);
            } finally {
              s.setNewTesting(false);
            }
          }}
          onSave={async () => {
            s.setModelSaving(true);
            try {
              if (s.editingModel)
                await api.updateModel(
                  s.editingModel.id,
                  { ...s.modelDraft, ...s.newTokenLimits },
                );
              else
                await api.createModel({
                  ...s.modelDraft,
                  ...s.newTokenLimits,
                });
              await refreshCatalog();
              const wasEditing = Boolean(s.editingModel);
              const label = s.modelDraft.label;
              s.setNewModel(false);
              s.setEditingModel(null);
              toast(wasEditing ? `${label} 已更新` : `${label} 已加入模型库`);
            } catch (e) {
              toast((e as Error).message, true);
            } finally {
              s.setModelSaving(false);
            }
          }}
        />
      )}
      {s.deleteModel && (
        <DeleteModelModal
          deleteModel={s.deleteModel}
          deletingModel={s.deletingModel}
          modalRef={s.deleteRef}
          onClose={() => s.setDeleteModel(null)}
          onConfirm={() => void s.removeModel(s.deleteModel!)}
        />
      )}
      {s.assignmentConfirm && (
        <DecisionDialog
          title="保存模型分配？"
          summary={`${s.assignmentAffectedRows.length} 类模型职责将使用新分配`}
          message="新任务和尚未启动的排队任务会使用新模型；正在运行的任务保持启动时的模型快照。"
          details={[
            `影响职责：${s.assignmentAffectedRows.map((row) => row.label).join("、")}`,
            "不会删除模型库配置，也不会重启正在运行的任务",
          ]}
          confirmLabel="确认保存模型分配"
          cancelLabel="返回检查"
          onClose={() => s.setAssignmentConfirm(false)}
          onConfirm={() => {
            s.setAssignmentConfirm(false);
            void s.saveAssignments();
          }}
        />
      )}
    </section>
  );
}
