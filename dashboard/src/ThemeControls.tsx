import { nextTint, TINT_COLORS, TINT_LABELS, toggleTheme, toggleVivid, type BaseTheme, type Theme, type Tint } from "./themeConfig";
import { rgbHex, vividPalette } from "./themePalette";

/** The text switch's state as the control shows it. */
export interface InkControl {
  on: boolean;
  /** Why the switch cannot be on here (empty when it can). */
  disabledWhy: string;
  onToggle: () => void;
}

export function ThemePanelBody({
  theme,
  base,
  tint,
  onTheme,
  onTint,
  ink,
}: {
  theme: Theme;
  /** The last of light / dark (what turning vivid off returns to); light when not given. */
  base?: BaseTheme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  compact?: boolean;
  ink?: InkControl;
}) {
  const last: BaseTheme = base ?? (theme === "theme-dark" ? "theme-dark" : "theme-light");
  const vivid = theme === "theme-vivid";
  // The mode circle shows the mode a click goes to leave: light or dark (from vivid, the last of the two).
  const mode: BaseTheme = vivid ? last : (theme as BaseTheme);
  return (
    <div className="e-theme-duo" role="group" aria-label="Theme">
      <button
        type="button"
        className="e-theme-circle e-theme-circle--mode e-theme-duo__btn"
        data-mode={mode}
        onClick={() => onTheme(toggleTheme(theme, last))}
        title={mode === "theme-dark" ? "dark (click for light)" : "light (click for dark)"}
        aria-label={mode === "theme-dark" ? "Theme: dark, switch to light" : "Theme: light, switch to dark"}
      />
      <span className="e-theme-duo__rule" aria-hidden />
      <button
        type="button"
        className="e-theme-circle e-theme-circle--vivid e-theme-duo__btn"
        data-on={vivid ? "true" : "false"}
        style={{ background: TINT_COLORS[tint], color: rgbHex(vividPalette(tint).text) }}
        onClick={() => onTheme(toggleVivid(theme, last))}
        aria-pressed={vivid}
        title={vivid ? `vivid on (click for ${last === "theme-dark" ? "dark" : "light"})` : "vivid off (click for vivid)"}
        aria-label={vivid ? `Vivid: on, switch to ${last === "theme-dark" ? "dark" : "light"}` : "Vivid: off, switch on"}
      />
      <span className="e-theme-duo__rule" aria-hidden />
      <button
        type="button"
        className="e-theme-circle e-theme-circle--accent e-theme-duo__btn"
        style={{ background: TINT_COLORS[tint] }}
        onClick={() => onTint(nextTint(tint))}
        title={`accent: ${TINT_LABELS[tint]} (click for the next)`}
        aria-label={`Accent color: ${TINT_LABELS[tint]}, click to cycle`}
      />
      {ink && (
        <>
          <span className="e-theme-duo__rule" aria-hidden />
          <button
            type="button"
            role="switch"
            aria-checked={ink.on}
            aria-label="Pure black or white text"
            disabled={!!ink.disabledWhy}
            className="e-switch e-theme-duo__btn"
            data-on={ink.on ? "true" : "false"}
            title={ink.disabledWhy || (ink.on ? "text: pure black or white (click for the theme's own ink)" : "text: the theme's own ink (click for pure black or white)")}
            onClick={ink.onToggle}
          >
            <span className="e-switch__knob" aria-hidden />
          </button>
        </>
      )}
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
  ink?: InkControl;
}) {
  return (
    <div className="e-theme-compact e-theme-compact--inline">
      <ThemePanelBody {...props} />
    </div>
  );
}
