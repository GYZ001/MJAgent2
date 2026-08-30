import { useEffect, useLayoutEffect, useMemo, useState } from "react";
import { api } from "../../api";
import type { SettingsView } from "../../api";
import type { NavigationGuardPrompt } from "../../App";
import { blockStatus, DataBoundary, track } from "./shared";
import {
  categorizeSettingKeys,
  isLegacyQaRetrySettingKey,
  normalizeDraft,
} from "./settings/definitions";
import SettingField from "./settings/SettingField";
import SettingsPreview from "./settings/SettingsPreview";

export { normalizeDraft, categorizeSettingKeys, settingOptionLabel } from "./settings/definitions";

export default function SettingsPanel({
  state,
  loading,
  error,
  refresh,
  toast,
  registerGuard,
  editable,
}: {
  state: SettingsView | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<SettingsView | null>;
  toast: (message: string, error?: boolean) => void;
  registerGuard: (guard: NavigationGuardPrompt | null, unsaved?: boolean) => void;
  editable: boolean;
}) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set());
  const [result, setResult] = useState<
    Array<{
      key: string;
      requested: string;
      effective: string;
      apply_mode: string;
    }>
  >([]);
  const values = state?.values || {};
  const schema = state?.schema || {};
  const visibleSchemaEntries = Object.entries(schema).filter(
    ([key]) =>
      !key.startsWith("model_") &&
      !key.includes("_model_") &&
      key !== "model_route" &&
      !isLegacyQaRetrySettingKey(key),
  );
  const visibleSchema = Object.fromEntries(visibleSchemaEntries);
  const settingGroups = categorizeSettingKeys(
    visibleSchemaEntries.map(([key]) => key),
  );
  const normalized = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(draft).map(([key, raw]) => [
          key,
          normalizeDraft(schema[key], raw),
        ]),
      ),
    [draft, schema],
  );
  const fieldErrors = useMemo(() => {
    const next: Record<string, string> = {};
    for (const [key, value] of Object.entries(normalized))
      if (value == null)
        next[key] =
          `请输入合法的${schema[key]?.type === "integer" ? "整数" : "值"}`;
    const merged = {
      ...values,
      ...Object.fromEntries(
        Object.entries(normalized).filter(([, value]) => value != null),
      ),
    } as Record<string, string>;
    if (
      Number(merged.video_ready_low_watermark) >
      Number(merged.video_ready_high_watermark)
    )
      next.video_ready_high_watermark = "高水位不能低于低水位";
    if (
      Number(merged.episode_video_inflight_limit) >
      Number(merged.project_video_inflight_limit)
    )
      next.project_video_inflight_limit = "单项目上限不能低于单集上限";
    return next;
  }, [normalized, schema, values]);
  const changed = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(normalized).filter(
          ([key, value]) => value != null && value !== values[key],
        ),
      ) as Record<string, string>,
    [normalized, values],
  );
  const dirty =
    Object.keys(changed).length > 0 || Object.keys(fieldErrors).length > 0;
  const resetAllDisabledReason = !editable
    ? "当前设置为只读"
    : !dirty
      ? "当前没有未保存修改"
      : "";
  const previewDisabledReason = !editable
    ? "当前设置为只读"
    : Object.keys(fieldErrors).length
      ? `请先修正 ${Object.keys(fieldErrors).length} 项输入`
      : !Object.keys(changed).length
        ? "当前没有可预览的修改"
        : "";
  const saveDisabledReason = !editable
    ? "当前设置为只读"
    : saving
      ? "正在保存系统设置"
      : "";
  useLayoutEffect(() => {
    const guard = dirty
      ? {
          title: "放弃未保存的系统设置？",
          summary: `${Object.keys(changed).length} 项设置尚未保存`,
          message:
            "离开后，本页填写的修改和校验结果都会丢失；当前已生效设置不会改变。",
          details: Object.keys(fieldErrors).length
            ? [`另有 ${Object.keys(fieldErrors).length} 项输入仍需修正`]
            : ["尚未点击“保存并应用”，不会影响正在运行的任务"],
          confirmLabel: "放弃修改并离开",
          cancelLabel: "继续编辑",
          danger: true,
        }
      : null;
    registerGuard(guard, dirty);
    const before = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
      }
    };
    window.addEventListener("beforeunload", before);
    return () => {
      registerGuard(null, false);
      window.removeEventListener("beforeunload", before);
    };
  }, [changed, dirty, fieldErrors, registerGuard]);
  useEffect(() => {
    if (state)
      setDraft((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([key]) => key in state.schema),
        ),
      );
  }, [state?.version]); // eslint-disable-line react-hooks/exhaustive-deps
  const edit = (key: string, raw: string) => {
    const normalizedValue = normalizeDraft(schema[key], raw);
    setDraft((current) => {
      const next = { ...current };
      if (normalizedValue != null && normalizedValue === values[key])
        delete next[key];
      else next[key] = raw;
      return next;
    });
    setPreview(false);
    setResult([]);
    setSaveError("");
  };
  const save = async () => {
    if (
      !state ||
      !Object.keys(changed).length ||
      Object.keys(fieldErrors).length
    )
      return;
    setSaving(true);
    setSaveError("");
    try {
      const response = await api.updateSettings({
        version: state.version,
        patch: changed,
      });
      setResult(response.items || []);
      setDraft({});
      setPreview(false);
      track("settings_submit", {
        result: "succeeded",
        filter_count: Object.keys(changed).length,
      });
      await refresh();
      const requiresRestart = (response.items || []).some(
        (item: { apply_mode: string }) => item.apply_mode === "restart",
      );
      toast(
        requiresRestart
          ? `系统设置 v${response.version} 已保存；标记项将在重启后生效`
          : `系统设置 v${response.version} 已整体即时生效`,
      );
    } catch (e) {
      track("settings_submit", { result: "failed" });
      setSaveError((e as Error).message);
      toast((e as Error).message, true);
    } finally {
      setSaving(false);
    }
  };
  const status = blockStatus(loading, error, state, !state);
  const toggleGroup = (groupId: string) =>
    setOpenGroups((current) => {
      const next = new Set(current);
      if (next.has(groupId)) next.delete(groupId);
      else next.add(groupId);
      return next;
    });
  return (
    <section className="card monitor-section monitor-settings">
      <div className="monitor-section-head compact">
        <div>
          <span className="eyebrow">系统策略</span>
          <h2>系统设置</h2>
        </div>
        <p>按功能展开 · 每项说明影响范围 · 修改后统一预览保存</p>
      </div>
      {!editable && (
        <div className="monitor-state stale" role="status">
          设置编辑新链路已由发布开关切为只读；现有运行时配置保持不变。
        </div>
      )}
      <DataBoundary
        status={status}
        error={error}
        updatedAt={state?.server_time}
        onRetry={() => void refresh()}
        emptyLabel="没有可维护设置"
      >
        {state && (
          <>
            {state.health === "invalid" && (
              <div className="monitor-state error" role="alert">
                检测到历史非法配置，运行时健康状态不可视为正常：
                {state.issues.map((issue) => issue.field).join("、")}
              </div>
            )}
            <div className="setting-category-list">
              {settingGroups.map((group) => {
                const expanded = openGroups.has(group.id);
                const changedCount = group.keys.filter(
                  (key) => key in changed,
                ).length;
                const errorCount = group.keys.filter(
                  (key) => key in fieldErrors,
                ).length;
                const panelId = `settings-panel-${group.id}`;
                return (
                  <section
                    className={`setting-category ${expanded ? "open" : ""}`}
                    id={`settings-group-${group.id}`}
                    key={group.id}
                  >
                    <button
                      type="button"
                      className="setting-category-toggle"
                      aria-expanded={expanded}
                      aria-controls={panelId}
                      onClick={() => toggleGroup(group.id)}
                    >
                      <span>
                        <b>{group.title}</b>
                        <small>{group.description}</small>
                      </span>
                      <span className="setting-category-meta">
                        {group.affects.map((effect) => (
                          <em key={effect}>{effect}</em>
                        ))}
                        <strong>
                          {errorCount
                            ? `${errorCount} 项错误`
                            : changedCount
                              ? `${changedCount} 项已改`
                              : `${group.keys.length} 项设置`}
                        </strong>
                        <i aria-hidden="true">⌄</i>
                      </span>
                    </button>
                    {expanded && (
                      <div className="monitor-settings-grid" id={panelId}>
                        {group.keys.map((key) => (
                          <SettingField
                            key={key}
                            settingKey={key}
                            spec={visibleSchema[key]}
                            current={draft[key] ?? values[key] ?? visibleSchema[key].default}
                            error={fieldErrors[key]}
                            editable={editable}
                            hasDraft={draft[key] !== undefined}
                            groupAffects={group.affects}
                            onChange={(raw) => edit(key, raw)}
                            onReset={() =>
                              setDraft((currentDraft) => {
                                const next = { ...currentDraft };
                                delete next[key];
                                return next;
                              })
                            }
                          />
                        ))}
                      </div>
                    )}
                  </section>
                );
              })}
            </div>
            <SettingsPreview
              preview={preview}
              changed={changed}
              schema={schema}
              values={values}
              result={result}
            />
            {saveError && (
              <div className="monitor-state error" role="alert">
                保存失败：{saveError}。草稿仍保留，可修正后重试。
              </div>
            )}
            <div className="monitor-settings-actions">
              <span>
                {Object.keys(changed).length
                  ? `${Object.keys(changed).length} 项合法改动待保存`
                  : Object.keys(fieldErrors).length
                    ? `${Object.keys(fieldErrors).length} 项校验错误`
                    : "当前没有未保存修改"}
              </span>
              <button
                onClick={() => {
                  setDraft({});
                  setPreview(false);
                }}
                disabled={Boolean(resetAllDisabledReason)}
                aria-label={resetAllDisabledReason ? `全部重置，暂不可用：${resetAllDisabledReason}` : `重置 ${Object.keys(draft).length} 项未保存修改`}
                title={resetAllDisabledReason || "恢复为当前已生效设置"}
              >
                全部重置
              </button>
              {!preview ? (
                <button
                  className="btn primary small"
                  disabled={Boolean(previewDisabledReason)}
                  aria-label={previewDisabledReason ? `预览差异，暂不可用：${previewDisabledReason}` : `预览 ${Object.keys(changed).length} 项设置差异`}
                  title={previewDisabledReason || "预览不会保存或应用设置"}
                  onClick={() => {
                    setPreview(true);
                    track("settings_preview", {
                      filter_count: Object.keys(changed).length,
                    });
                  }}
                >
                  预览差异
                </button>
              ) : (
                <button
                  className="btn primary small"
                  disabled={Boolean(saveDisabledReason)}
                  aria-label={saveDisabledReason ? `批准并保存全部，暂不可用：${saveDisabledReason}` : `批准并保存 ${Object.keys(changed).length} 项设置`}
                  title={saveDisabledReason || "保存后即时项立即生效，其他项在重启后生效"}
                  onClick={() => void save()}
                >
                  {saving ? "整体提交中…" : "批准并保存全部"}
                </button>
              )}
            </div>
          </>
        )}
      </DataBoundary>
    </section>
  );
}
