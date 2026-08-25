import { useId } from "react";
import { useTheme } from "./ThemeContext";
import { AUTO_MODE_HINT, THEME_MODES, THEME_MODE_LABELS } from "./theme";

/** 侧栏用户菜单里的三态主题切换。用 aria-pressed 的按钮组，
 *  不用 radiogroup——那套要自己接管方向键，这里不值当。 */
export default function ThemeSwitch() {
  const { mode, resolved, setMode } = useTheme();
  const labelId = useId();
  return (
    <div className="theme-switch">
      <div className="theme-switch-title" id={labelId}>
        界面主题
      </div>
      <div className="theme-switch-options" role="group" aria-labelledby={labelId}>
        {THEME_MODES.map((item) => (
          <button
            key={item}
            type="button"
            className={mode === item ? "active" : ""}
            aria-pressed={mode === item}
            onClick={() => setMode(item)}
          >
            {THEME_MODE_LABELS[item]}
          </button>
        ))}
      </div>
      {mode === "auto" && (
        <small>{`${AUTO_MODE_HINT} · 现在是${resolved === "dark" ? "暗色" : "亮色"}`}</small>
      )}
    </div>
  );
}
