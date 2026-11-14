import { nextTint, TINT_COLORS, TINT_LABELS, toggleTheme, toggleVivid, type BaseTheme, type Theme, type Tint } from "./themeConfig";
import { rgbHex, vividPalette } from "./themePalette";
import { FONTS, FONT_LABELS, INK_COLORS, INK_LABELS, nextInk, type Font, type Ink } from "./lookPrefs";

/** The viewer's font and ink choice, and whether the ink reaches body text on this theme and tint. */
export interface LookControl {
  font: Font;
  ink: Ink;
  bodyInk: boolean;
  onFont: (f: Font) => void;
  onInk: (i: Ink) => void;
}

export function ThemePanelBody({
  theme,
  base,
  tint,
  onTheme,
  onTint,
  look,
}: {
  theme: Theme;
  /** The last of light / dark (what turning vivid off returns to); light when not given. */
  base?: BaseTheme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  compact?: boolean;
  look?: LookControl;
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
        key={tint}
        type="button"
        className="e-sleeve e-theme-duo__btn"
        style={{ background: TINT_COLORS[tint], color: rgbHex(vividPalette(tint).text) }}
        onClick={() => onTint(nextTint(tint))}
        title={`accent: ${TINT_LABELS[tint]} (click for the next)`}
        aria-label={`Accent colour: ${TINT_LABELS[tint]}, click to cycle`}
      >
        <span className="e-sleeve__name" aria-hidden>{TINT_LABELS[tint]}</span>
      </button>
      {look && (
        <>
          <span className="e-theme-duo__rule" aria-hidden />
          <button
            type="button"
            className="e-inkwell e-theme-duo__btn"
            data-ink={look.ink}
            style={look.ink === "auto" ? undefined : { ["--well" as string]: INK_COLORS[look.ink] }}
            onClick={() => look.onInk(nextInk(look.ink))}
            title={`text colour: ${INK_LABELS[look.ink]} (click for ${INK_LABELS[nextInk(look.ink)]})`}
            aria-label={`Text colour: ${INK_LABELS[look.ink]}, click for ${INK_LABELS[nextInk(look.ink)]}`}
          >
            <span className="e-inkwell__drip" aria-hidden />
          </button>
          {look.ink !== "auto" && !look.bodyInk && (
            <span className="e-lowcontrast" role="img" title="Low contrast here: body text stays on Auto; the title follows your choice" aria-label="Low contrast: body text stays on Auto">
              low contrast
            </span>
          )}
          <span className="e-theme-duo__rule" aria-hidden />
          <span className="e-keys" role="group" aria-label="Font">
            {FONTS.map((f) => (
              <button
                key={f}
                type="button"
                className={`e-key e-key--${f}`}
                aria-pressed={look.font === f}
                aria-label={`Font: ${FONT_LABELS[f]}`}
                title={`font: ${FONT_LABELS[f]}`}
                onClick={() => look.onFont(f)}
              >
                <span aria-hidden>{f === "type" ? "a" : "A"}</span>
              </button>
            ))}
          </span>
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
  look?: LookControl;
}) {
  return (
    <div className="e-theme-compact e-theme-compact--inline">
      <ThemePanelBody {...props} />
    </div>
  );
}
