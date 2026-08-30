import type { SettingSchema } from "../../../api";
import { settingOptionLabel } from "./definitions";

/** 「预览差异」与「权威生效结果」两块——从 SettingsPanel 里拆出来，纯展示、
 *  不持有状态，只是把同一块 JSX 挪成组件。 */
export default function SettingsPreview({
  preview,
  changed,
  schema,
  values,
  result,
}: {
  preview: boolean;
  changed: Record<string, string>;
  schema: Record<string, SettingSchema>;
  values: Record<string, string>;
  result: Array<{ key: string; requested: string; effective: string; apply_mode: string }>;
}) {
  return (
    <>
      {preview && (
        <div className="settings-preview" role="dialog" aria-label="设置差异预览">
          <h3>确认设置差异</h3>
          {Object.entries(changed).map(([key, value]) => (
            <div key={key}>
              <b>{schema[key].label}</b>
              <span>
                {settingOptionLabel(key, values[key])} →{" "}
                {settingOptionLabel(key, value)}
              </span>
              <small>
                {schema[key].immediate ? "保存成功后即时生效" : "保存后需重启生效"}
              </small>
            </div>
          ))}
        </div>
      )}
      {result.length > 0 && (
        <div className="settings-result">
          <b>权威生效结果</b>
          {result.map((item) => (
            <span key={item.key}>
              {schema[item.key]?.label || item.key}：请求{" "}
              {settingOptionLabel(item.key, item.requested)} / 有效{" "}
              {settingOptionLabel(item.key, item.effective)}（
              {item.apply_mode === "immediate" ? "即时" : "需重启"}）
            </span>
          ))}
        </div>
      )}
    </>
  );
}
