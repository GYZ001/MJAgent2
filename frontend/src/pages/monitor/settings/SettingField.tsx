import type { SettingSchema } from "../../../api";
import { SETTING_FIELD_IMPACTS, settingOptionLabel } from "./definitions";

/** 单个设置项的渲染——从 SettingsSection 的分组循环里拆出来，只是把同一块 JSX
 *  挪成组件而非重写；行为、DOM 结构与原来完全一致。 */
export default function SettingField({
  settingKey,
  spec,
  current,
  error,
  editable,
  hasDraft,
  groupAffects,
  onChange,
  onReset,
}: {
  settingKey: string;
  spec: SettingSchema;
  current: string;
  error?: string;
  editable: boolean;
  hasDraft: boolean;
  groupAffects: string[];
  onChange: (raw: string) => void;
  onReset: () => void;
}) {
  const id = `setting-${settingKey}`;
  return (
    <div className={`setting-field ${error ? "invalid" : ""}`}>
      <label htmlFor={id}>
        <b>
          {spec.label}
          {spec.experimental ? <em>实验</em> : ""}
        </b>
        <small className="setting-field-impact">
          影响：
          {SETTING_FIELD_IMPACTS[settingKey] || groupAffects.join("、")}
        </small>
        <small>
          {spec.unit || "无单位"} ·{" "}
          {spec.type === "boolean"
            ? "开关"
            : spec.type === "enum"
              ? `可选 ${spec.options?.map((option) => settingOptionLabel(settingKey, option)).join(" / ")}`
              : spec.type === "string"
                ? `最多 ${spec.max_length || 1000} 字符`
                : `${spec.min}~${spec.max}，步长 ${spec.step}`}
        </small>
      </label>
      {spec.type === "boolean" ? (
        <input
          id={id}
          type="checkbox"
          checked={current === "true"}
          onChange={(e) => onChange(e.target.checked ? "true" : "false")}
          disabled={!editable}
        />
      ) : spec.type === "enum" ? (
        <select
          id={id}
          value={current}
          onChange={(e) => onChange(e.target.value)}
          disabled={!editable}
        >
          {spec.options?.map((option) => (
            <option key={option} value={option}>
              {settingOptionLabel(settingKey, option)}
            </option>
          ))}
        </select>
      ) : (
        <input
          id={id}
          type={spec.type === "string" ? "text" : "number"}
          min={spec.min}
          max={spec.max}
          step={spec.step}
          value={current}
          onChange={(e) => onChange(e.target.value)}
          aria-invalid={!!error}
          disabled={!editable}
        />
      )}
      {error && (
        <b className="setting-field-error" role="alert">
          {error}
        </b>
      )}
      <details className="setting-field-technical">
        <summary>技术信息</summary>
        <code>
          {settingKey} · 默认 {spec.default} ·{" "}
          {spec.immediate ? "即时生效" : "需重启生效"}
        </code>
      </details>
      <button
        type="button"
        disabled={!editable || !hasDraft}
        aria-label={
          !editable
            ? `重置${spec.label}，暂不可用：当前设置为只读`
            : !hasDraft
              ? `重置${spec.label}，暂不可用：此项尚未修改`
              : `重置${spec.label}`
        }
        title={
          !editable
            ? "当前设置为只读"
            : !hasDraft
              ? "此项尚未修改"
              : "恢复为当前已生效值"
        }
        onClick={onReset}
      >
        重置此项
      </button>
    </div>
  );
}
