import { useMemo, useRef, useState } from "react";
import { api, ApprovalRequiredError } from "../../../api";
import type { CatalogModel, Health, ModelCatalog, ModelKind, SettingsView } from "../../../api";
import { useFocusTrap } from "../../../hooks/useFocusTrap";
import type { ModelTestState } from "./LibraryModal";
import type { ModelDraft } from "./NewModelModal";
import {
  MODEL_ROWS,
  formatTokenCapacity,
  modelAssignmentSettingKey,
  modelAssignmentValue,
  modelBusinessLabel,
} from "./constants";

/** ModelCenter（模型中心）的全部状态、派生值与事件处理器——从组件体里拆出来，
 *  只是把同一段逻辑挪成一个 hook，不改变任何行为；渲染部分留在
 *  monitor/ModelsSection.tsx。 */
export function useModelCenterState({
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
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [library, setLibrary] = useState(false);
  const [search, setSearch] = useState("");
  const [provider, setProvider] = useState("");
  const [capability, setCapability] = useState("");
  const [connection, setConnection] = useState("");
  const [testStates, setTestStates] = useState<Record<string, ModelTestState>>({});
  const [saving, setSaving] = useState(false);
  const [assignmentConfirm, setAssignmentConfirm] = useState(false);
  const [credential, setCredential] = useState<CatalogModel | null>(null);
  const [credentialDraft, setCredentialDraft] = useState({
    base_url: "",
    api_key: "",
  });
  const [testedSignature, setTestedSignature] = useState("");
  const [testing, setTesting] = useState(false);
  const [credentialSaving, setCredentialSaving] = useState(false);
  const [newModel, setNewModel] = useState(false);
  const [editingModel, setEditingModel] = useState<CatalogModel | null>(null);
  const [deleteModel, setDeleteModel] = useState<CatalogModel | null>(null);
  const [deletingModel, setDeletingModel] = useState(false);
  const [modelDraft, setModelDraft] = useState<ModelDraft>({
    label: "",
    provider_label: "",
    base_url: "",
    api_key: "",
    model: "",
    kinds: ["text"] as ModelKind[],
    protocol: "",
  });
  const [newTested, setNewTested] = useState("");
  const [newTokenLimits, setNewTokenLimits] = useState<{
    context_window_tokens?: number;
    max_output_tokens?: number;
    token_limits_source?: string;
  }>({});
  const [newTesting, setNewTesting] = useState(false);
  const [modelSaving, setModelSaving] = useState(false);
  const libraryTriggerRef = useRef<HTMLElement | null>(null);
  const modelDialogTriggerRef = useRef<HTMLElement | null>(null);
  const nestedModelDialogOpen =
    !!credential || newModel || !!editingModel || !!deleteModel;
  const libraryRef = useFocusTrap(library, () => setLibrary(false), {
    suspended: nestedModelDialogOpen,
    returnFocus: libraryTriggerRef.current,
  });
  const credentialRef = useFocusTrap(!!credential, () => setCredential(null), {
    returnFocus: modelDialogTriggerRef.current,
  });
  const newRef = useFocusTrap(newModel || !!editingModel, () => {
    setNewModel(false);
    setEditingModel(null);
  }, {
    returnFocus: modelDialogTriggerRef.current,
  });
  const deleteRef = useFocusTrap(!!deleteModel, () => setDeleteModel(null), {
    returnFocus: modelDialogTriggerRef.current,
  });
  const assignmentPatch = useMemo(() => {
    const patch: Record<string, string> = {};
    for (const row of MODEL_ROWS) {
      const selection = health?.models?.[row.key];
      if (!selection) continue;
      const providerKey =
        draft[`model_${row.key}_provider`] ?? selection.provider;
      const providerChanged = providerKey !== selection.provider;
      if (providerChanged)
        patch[`model_${row.key}_provider`] = providerKey;
      const modelKey = modelAssignmentSettingKey(providerKey, row.key);
      if (!modelKey) continue;
      const modelValue = draft[modelKey];
      if (
        modelValue &&
        (providerChanged ||
          modelValue !==
            selection.options.find(
              (option) => option.provider === providerKey,
            )?.model)
      )
        patch[modelKey] = modelValue;
    }
    return patch;
  }, [draft, health]);
  const assignmentAffectedRows = MODEL_ROWS.filter((row) =>
    Object.keys(assignmentPatch).some((key) => key.includes(row.key)),
  );
  const assignmentConnectionIssue = assignmentAffectedRows
    .map((row) => {
      const selection = health?.models?.[row.key];
      if (!selection) return `${row.label}尚未加载完成`;
      const providerKey =
        draft[`model_${row.key}_provider`] ?? selection.provider;
      const modelKey = modelAssignmentSettingKey(providerKey, row.key);
      const model = modelAssignmentValue(
        selection,
        catalog?.items || [],
        row.key,
        providerKey,
        modelKey ? draft[modelKey] : undefined,
      );
      const target = catalog?.items.find(
        (item) =>
          item.provider === providerKey &&
          item.model === model &&
          item.kinds.includes(row.key),
      );
      if (!target) return `请先为${row.label}选择一个模型`;
      if (!target.key_configured)
        return `请先配置「${modelBusinessLabel(target.label)}」的连接`;
      return "";
    })
    .find(Boolean);
  const assignmentSaveDisabledReason = saving
    ? "正在保存模型分配"
    : !assignmentAffectedRows.length
      ? "当前没有未保存的模型分配"
      : assignmentConnectionIssue
        ? assignmentConnectionIssue
        : "";
  const saveAssignments = async () => {
    if (!settings) return;
    setSaving(true);
    try {
      const response = await api.updateSettings({
        version: settings.version,
        patch: assignmentPatch,
      });
      setDraft({});
      await Promise.all([refreshHealth(), refreshSettings()]);
      const scope = response.effect_scope;
      toast(
        scope?.new_tasks && scope?.queued_not_started && !scope?.running_tasks
          ? "模型分配已保存；新任务和未启动队列使用新模型，运行中任务保持启动快照"
          : "模型分配已保存；请以系统返回的生效范围为准",
      );
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setSaving(false);
    }
  };
  const filtered = (catalog?.items || []).filter((item) => {
    const q = search.trim().toLowerCase();
    return (
      (!q || `${item.label} ${item.model}`.toLowerCase().includes(q)) &&
      (!provider || item.provider === provider) &&
      (!capability || item.kinds.includes(capability as ModelKind)) &&
      (!connection || (connection === "configured") === !!item.key_configured)
    );
  });
  const testModel = async (item: CatalogModel) => {
    setTestStates((s) => ({ ...s, [item.id]: { state: "testing" } }));
    try {
      const result = await api.testModel(item.id);
      setTestStates((s) => ({
        ...s,
        [item.id]: {
          state: "ok",
          note: `${result.latency_ms} ms · 上下文 ${formatTokenCapacity(result.context_window_tokens)} · 输出 ${formatTokenCapacity(result.max_output_tokens)}`,
        },
      }));
      await refreshCatalog();
    } catch (e) {
      setTestStates((s) => ({
        ...s,
        [item.id]: { state: "fail", note: (e as Error).message },
      }));
    }
  };
  const removeModel = async (item: CatalogModel) => {
    setDeletingModel(true);
    try {
      await api.deleteModel(item.id).catch(e => (e instanceof ApprovalRequiredError ? e.retry() : Promise.reject(e))); // DeleteModal 已问过一次，直接重放
      await refreshCatalog();
      setDeleteModel(null);
      toast(`${modelBusinessLabel(item.label)} 已删除`);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setDeletingModel(false);
    }
  };
  const groupedModels = filtered.reduce<Record<string, CatalogModel[]>>(
    (groups, item) => {
      (groups[item.provider] ||= []).push(item);
      return groups;
    },
    {},
  );
  const defaults: Record<string, string> = {
    openrouter: "https://openrouter.ai/api/v1",
    bailian: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    deepseek: "https://api.deepseek.com/v1",
    zhipu: "https://open.bigmodel.cn/api/paas/v4",
  };
  const openConnectionDialog = (item: CatalogModel, trigger: HTMLElement) => {
    modelDialogTriggerRef.current = trigger;
    setCredential(item);
    setCredentialDraft({
      base_url: item.base_url || defaults[item.provider] || "",
      api_key: "",
    });
    setTestedSignature("");
  };
  const credentialSignature = JSON.stringify(credentialDraft);
  const newSignature = JSON.stringify(modelDraft);
  const credentialTestDisabledReason = !credential
    ? "未选择模型"
    : testing
      ? "正在测试连接"
      : !credentialDraft.base_url.trim()
        ? "请先填写服务地址"
        : !credential.key_configured && !credentialDraft.api_key.trim()
          ? "请先填写访问密钥"
          : "";
  const credentialSaveDisabledReason = credentialSaving
    ? "正在保存连接"
    : testedSignature !== credentialSignature
      ? "请先使用当前地址和密钥通过连接测试"
      : "";
  // 每条模型都要声明接入协议：代码里只有协议实现，没有模型。
  const protocolOptions = Array.from(
    new Set(
      (["video", "image", "text", "vlm"] as ModelKind[]).flatMap((kind) =>
        modelDraft.kinds.includes(kind)
          ? catalog?.media_protocols?.[kind] ?? []
          : [],
      ),
    ),
  );
  const modelDraftMissing = [
    !modelDraft.label.trim() ? "显示名称" : "",
    !modelDraft.provider_label.trim() ? "服务名称" : "",
    !modelDraft.base_url.trim() ? "服务地址" : "",
    !modelDraft.model.trim() ? "模型标识" : "",
    !editingModel && !modelDraft.api_key.trim() ? "访问密钥" : "",
    !modelDraft.kinds.length ? "至少一种模型能力" : "",
    !modelDraft.protocol ? "接入协议" : "",
  ].filter(Boolean);
  const modelTestDisabledReason = newTesting
    ? "正在测试连接"
    : modelDraftMissing.length
      ? `请先填写：${modelDraftMissing.join("、")}`
      : "";
  const modelSaveDisabledReason = modelSaving
    ? "正在保存模型"
    : modelDraftMissing.length
      ? `请先填写：${modelDraftMissing.join("、")}`
      : newTested !== newSignature
        ? "请先使用当前配置通过连接测试"
        : "";

  return {
    draft, setDraft,
    library, setLibrary,
    search, setSearch,
    provider, setProvider,
    capability, setCapability,
    connection, setConnection,
    testStates,
    saving,
    assignmentConfirm, setAssignmentConfirm,
    credential, setCredential,
    credentialDraft, setCredentialDraft,
    testedSignature, setTestedSignature,
    testing, setTesting,
    credentialSaving, setCredentialSaving,
    newModel, setNewModel,
    editingModel, setEditingModel,
    deleteModel, setDeleteModel,
    deletingModel,
    modelDraft, setModelDraft,
    newTested, setNewTested,
    newTokenLimits, setNewTokenLimits,
    newTesting, setNewTesting,
    modelSaving, setModelSaving,
    libraryTriggerRef,
    modelDialogTriggerRef,
    libraryRef,
    credentialRef,
    newRef,
    deleteRef,
    assignmentPatch,
    assignmentAffectedRows,
    assignmentSaveDisabledReason,
    saveAssignments,
    testModel,
    removeModel,
    groupedModels,
    openConnectionDialog,
    credentialSignature,
    newSignature,
    credentialTestDisabledReason,
    credentialSaveDisabledReason,
    protocolOptions,
    modelTestDisabledReason,
    modelSaveDisabledReason,
  };
}
