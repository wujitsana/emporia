import { nextTint, TINT_COLORS, TINT_LABELS, toggleTheme, toggleVivid, type BaseTheme, type Theme, type Tint } from "./themeConfig";
import { rgbHex, vividPalette } from "./themePalette";

export function ThemePanelBody({
  theme,
  base,
  tint,
  onTheme,
  onTint,
}: {
  theme: Theme;
  /** The last of light / dark (what turning vivid off returns to); light when not given. */
  base?: BaseTheme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  compact?: boolean;
}) {
  const last: BaseTheme = base ?? (theme === "theme-dark" ? "theme-dark" : "theme-light");
  const vivid = theme === "theme-vivid";
  // The mode circle shows where a click goes: the opposite of the current mode (from vivid, of the last light / dark).
  const mode: BaseTheme = vivid ? last : (theme as BaseTheme);
  const target: BaseTheme = mode === "theme-dark" ? "theme-light" : "theme-dark";
  const targetWord = target === "theme-dark" ? "dark" : "light";
  const tintColor = TINT_COLORS[tint];
  const inkColor = rgbHex(vividPalette(tint).text);
  return (
    <div className="e-theme-duo" role="group" aria-label="Theme">
      <button
        type="button"
        className="e-theme-circle e-theme-circle--mode e-theme-duo__btn"
        data-mode={target}
        onClick={() => onTheme(toggleTheme(theme, last))}
        title={`switch to ${targetWord}`}
        aria-label={`Theme: switch to ${targetWord}`}
      />
      <span className="e-theme-duo__rule" aria-hidden />
      <button
        type="button"
        role="switch"
        aria-checked={vivid}
        className="e-switch e-theme-duo__btn"
        data-on={vivid ? "true" : "false"}
        style={{ color: tintColor, background: vivid ? tintColor : "transparent" }}
        onClick={() => onTheme(toggleVivid(theme, last))}
        title={vivid ? `vivid on (click for ${last === "theme-dark" ? "dark" : "light"})` : "vivid off (click for vivid)"}
        aria-label={vivid ? `Vivid: on, switch to ${last === "theme-dark" ? "dark" : "light"}` : "Vivid: off, switch on"}
      >
        <span className="e-switch__knob" aria-hidden style={{ background: vivid ? inkColor : tintColor }} />
      </button>
      <span className="e-theme-duo__rule" aria-hidden />
      <button
        type="button"
        className="e-theme-circle e-theme-circle--accent e-theme-duo__btn"
        style={{ background: TINT_COLORS[tint] }}
        onClick={() => onTint(nextTint(tint))}
        title={`accent: ${TINT_LABELS[tint]} (click for the next)`}
        aria-label={`Accent color: ${TINT_LABELS[tint]}, click to cycle`}
      />
    </div>
  );
}

/** Header / mobile: same three-circle control, no popover. */
export function ThemeCompact(props: {
  theme: Theme;
  base?: BaseTheme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
}) {
  return (
    <div className="e-theme-compact e-theme-compact--inline">
      <ThemePanelBody {...props} />
    </div>
  );
}
