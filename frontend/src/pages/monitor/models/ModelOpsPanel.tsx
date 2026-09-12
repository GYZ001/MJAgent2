import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api";
import type {
  CatalogModel,
  ModelCredentialSummary,
  ModelHealthItem,
  PurposeBindingRow,
  PurposeStatus,
} from "../../../api";
import { blockStatus, DataBoundary } from "../shared";
import "../../../styles/ModelOps.css";
import FallbackBanner from "./FallbackBanner";
import type { HealthRow } from "./ModelHealthTable";
import ModelHealthTable from "./ModelHealthTable";
import PurposeBindings from "./PurposeBindings";
import RateLimitModal from "./RateLimitModal";

/** 模型中心管理面板：健康度/凭据/用途绑定三块只读聚合 + 启停/限速/绑定重排
 *  写操作，自己管自己的加载状态（不复用 usePoll，避免从这层深的文件静态
 *  import App.tsx 造成循环依赖）。挂载在 ModelsSection 里，紧贴既有模型库
 *  管理区之上——它展示的是"正在实际发生什么"，模型库管的是"配置了什么"。 */
export default function ModelOpsPanel({
  catalog,
  onConfigureConnection,
  refreshCatalog,
  toast,
}: {
  catalog: CatalogModel[];
  onConfigureConnection: (item: CatalogModel, trigger: HTMLElement) => void;
  refreshCatalog: () => Promise<unknown>;
  toast: (message: string, error?: boolean) => void;
}) {
  const [health, setHealth] = useState<ModelHealthItem[] | null>(null);
  const [credentials, setCredentials] = useState<ModelCredentialSummary[] | null>(null);
  const [purposes, setPurposes] = useState<PurposeStatus[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyModelId, setBusyModelId] = useState<string | null>(null);
  const [busyBindingKey, setBusyBindingKey] = useState<string | null>(null);
  const [rateLimitRow, setRateLimitRow] = useState<HealthRow | null>(null);
  const [rateLimitSaving, setRateLimitSaving] = useState(false);

  const load = useCallback(async () => {
    try {
      const [h, c, p] = await Promise.all([
        api.getModelHealth(), api.getModelCredentialsSummary(), api.getPurposeStatus(),
      ]);
      setHealth(h.items);
      setCredentials(c.items);
      setPurposes(p.items);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const credentialByModelId = new Map((credentials || []).map((c) => [c.model_id, c]));
  const rows: HealthRow[] = (health || []).map((item) => ({
    ...item, credential: credentialByModelId.get(item.model_id),
  }));

  const toggleModelEnabled = async (row: HealthRow) => {
    setBusyModelId(row.model_id);
    try {
      await api.updateModel(row.model_id, { enabled: !row.enabled });
      await Promise.all([load(), refreshCatalog()]);
      toast(`${row.label} 已${row.enabled ? "停用" : "启用"}`);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusyModelId(null);
    }
  };

  const saveRateLimit = async (draft: { rpm: number; tpm: number; concurrency: number }) => {
    if (!rateLimitRow) return;
    setRateLimitSaving(true);
    try {
      await api.updateModel(rateLimitRow.model_id, { rate_limit: draft });
      await refreshCatalog();
      setRateLimitRow(null);
      toast(`${rateLimitRow.label} 的限速已保存（进程内生效）`);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setRateLimitSaving(false);
    }
  };

  const swapBindings = async (purpose: string, a: PurposeBindingRow, b: PurposeBindingRow) => {
    const key = `${purpose}:${a.priority}`;
    setBusyBindingKey(key);
    try {
      await api.upsertModelBinding({
        purpose, model_id: b.model_id, priority: a.priority, enabled: b.enabled, params: b.params,
      });
      await api.upsertModelBinding({
        purpose, model_id: a.model_id, priority: b.priority, enabled: a.enabled, params: a.params,
      });
      await load();
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusyBindingKey(null);
    }
  };

  const toggleBindingEnabled = async (purpose: string, binding: PurposeBindingRow) => {
    const key = `${purpose}:${binding.priority}`;
    setBusyBindingKey(key);
    try {
      await api.upsertModelBinding({
        purpose, model_id: binding.model_id, priority: binding.priority,
        enabled: !binding.enabled, params: binding.params,
      });
      await load();
      toast(`${purpose} 优先级 ${binding.priority} 已${binding.enabled ? "停用" : "启用"}`);
    } catch (e) {
      toast((e as Error).message, true);
    } finally {
      setBusyBindingKey(null);
    }
  };

  const status = blockStatus(loading, error, health, false);
  return (
    <section className="model-ops-panel">
      <DataBoundary status={status} error={error} onRetry={() => void load()} emptyLabel="暂无数据">
        <FallbackBanner purposes={purposes || []} />
        <h4>模型健康度与凭据</h4>
        <ModelHealthTable
          rows={rows}
          busyModelId={busyModelId}
          onToggleEnabled={(row) => void toggleModelEnabled(row)}
          onRotate={(row, trigger) => {
            const item = catalog.find((m) => m.id === row.model_id);
            if (item) onConfigureConnection(item, trigger);
          }}
          onEditRateLimit={(row) => setRateLimitRow(row)}
        />
        <h4>用途绑定优先级链</h4>
        <PurposeBindings
          purposes={purposes || []}
          busyKey={busyBindingKey}
          onSwap={(purpose, a, b) => void swapBindings(purpose, a, b)}
          onToggleEnabled={(purpose, binding) => void toggleBindingEnabled(purpose, binding)}
        />
      </DataBoundary>
      {rateLimitRow && (
        <RateLimitModal
          row={rateLimitRow}
          initial={catalog.find((m) => m.id === rateLimitRow.model_id)?.rate_limit}
          saving={rateLimitSaving}
          onClose={() => setRateLimitRow(null)}
          onSave={(draft) => void saveRateLimit(draft)}
        />
      )}
    </section>
  );
}
