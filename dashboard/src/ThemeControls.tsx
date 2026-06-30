import { useState } from "react";
import { TINT_COLORS, TINT_LABELS, TINTS, type Theme, type Tint } from "./themeConfig";

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
    <div className="e-theme-picker e-theme-picker--mini">
      <div className="e-mode-toggles" role="group" aria-label="Theme mode">
        <button
          type="button"
          className={`e-mode-toggle${theme === "theme-dark" ? " is-active" : ""}`}
          onClick={() => onTheme("theme-dark")}
          aria-pressed={theme === "theme-dark"}
          title="Dark"
        >
          ☾
        </button>
        <button
          type="button"
          className={`e-mode-toggle${theme === "theme-light" ? " is-active" : ""}`}
          onClick={() => onTheme("theme-light")}
          aria-pressed={theme === "theme-light"}
          title="Light"
        >
          ☀
        </button>
      </div>
      <div className="e-accent-row" role="list" aria-label="Accent color">
        {TINTS.map((t) => (
          <button
            key={t || "amber"}
            type="button"
            role="listitem"
            className={`e-accent-dot${tint === t ? " is-active" : ""}`}
            title={TINT_LABELS[t]}
            aria-label={TINT_LABELS[t]}
            aria-pressed={tint === t}
            onClick={() => onTint(t)}
          >
            <span style={{ background: TINT_COLORS[t] }} />
          </button>
        ))}
      </div>
    </div>
  );
}

export function ThemeCompact({
  theme,
  tint,
  onTheme,
  onTint,
}: {
  theme: Theme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="e-theme-compact">
      {open && (
        <>
          <button
            type="button"
            className="e-theme-compact__backdrop"
            aria-label="Close theme"
            onClick={() => setOpen(false)}
          />
          <div className="e-theme-compact__pop" role="dialog" aria-label="Theme">
            <ThemePanelBody theme={theme} tint={tint} onTheme={onTheme} onTint={onTint} />
          </div>
        </>
      )}
      <button
        type="button"
        className="e-theme-compact__btn"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Theme"
      >
        <span className={`e-accent-dot e-accent-dot--trigger${open ? " is-active" : ""}`}>
          <span style={{ background: TINT_COLORS[tint] }} />
        </span>
      </button>
    </div>
  );
}

export function ThemeProfileCard(props: {
  theme: Theme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
}) {
  return <ThemePanelBody {...props} />;
}