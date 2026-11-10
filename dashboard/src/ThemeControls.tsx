import { nextTint, TINT_COLORS, TINT_LABELS, toggleTheme, type Theme, type Tint } from "./themeConfig";

export function ThemePanelBody({
  theme,
  tint,
  onTheme,
  onTint,
}: {
  theme: Theme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  compact?: boolean;
}) {
  return (
    <div className="e-theme-duo" role="group" aria-label="Theme">
      <button
        type="button"
        className="e-theme-circle e-theme-circle--mode e-theme-duo__btn"
        data-mode={theme}
        onClick={() => onTheme(toggleTheme(theme))}
        title={theme === "theme-dark" ? "dark (click for light)" : "light (click for dark)"}
        aria-label={theme === "theme-dark" ? "Theme: dark, switch to light" : "Theme: light, switch to dark"}
      />
      <span className="e-theme-duo__rule" aria-hidden />
      <button
        type="button"
        className="e-theme-circle e-theme-circle--accent e-theme-duo__btn"
        style={{ background: TINT_COLORS[tint] }}
        onClick={() => onTint(nextTint(tint))}
        title={`accent: ${TINT_LABELS[tint]} (click for the next)`}
        aria-label={`Accent colour: ${TINT_LABELS[tint]}, click to cycle`}
      />
    </div>
  );
}

/** Header / mobile: same two-circle control, no popover. */
export function ThemeCompact(props: {
  theme: Theme;
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
