import { useEffect, useMemo, useState, type CSSProperties } from "react";

import { CAPTURE, PARAM_THEME, PARAM_TINT } from "../capture";
import { applyTheme, TINT_COLORS, TINT_LABELS, TINTS, type Theme, type Tint } from "../themeConfig";
import { Badge } from "./Badge";
import { galleryIds, resolveBadge } from "./catalog.ts";
import { contrast } from "./color.ts";
import { themeColors, type ThemeName } from "./tokens.ts";

const KIND_TITLES: Record<string, string> = {
  quest: "Quests", arc: "Arcs", tier: "Track tiers", feat: "Feats", class: "Classes", renown: "Renown levels", season: "Season",
  rank: "Board ranks", game: "Game marks", status: "Status marks", fallback: "Fallback",
};

/** The lowest ink and accent contrast a badge meets in this theme and tint (animatic surfaces). */
function lowest(theme: ThemeName, tint: Tint): { ink: number; accent: number } {
  const t = themeColors("animatic", theme, tint);
  return {
    ink: Math.min(...t.surfaces.map((s) => contrast(t.ink, s.rgb))),
    accent: Math.min(...t.surfaces.map((s) => contrast(t.accent, s.rgb))),
  };
}

/**
 * The badge gallery (badges.html): every badge at 1×, 2× and 3×, then the whole set on
 * light and dark in all eight tints. For review and for the demo video; read-only — the
 * controls change only what this page shows. `?theme=`, `?tint=` and `?capture=1` work as
 * in the dashboard.
 */
export function BadgeGallery({ embedded = false }: { embedded?: boolean } = {}) {
  const [theme, setTheme] = useState<Theme>(PARAM_THEME ?? "theme-light");
  const [tint, setTint] = useState<Tint>(PARAM_TINT ?? "tint-green");
  const [popKey, setPopKey] = useState(0);
  // As its own page it sets its own theme. Inside the dashboard (More › Badges) the app's theme
  // and tint stand (t, T), and the page changes nothing but what it shows.
  useEffect(() => {
    if (!embedded) applyTheme(theme, tint, { fade: false, capture: CAPTURE });
  }, [theme, tint, embedded]);

  const ids = useMemo(() => galleryIds(), []);
  const groups = useMemo(() => {
    const by = new Map<string, string[]>();
    for (const id of ids) {
      const kind = resolveBadge(id).def.kind;
      by.set(kind, [...(by.get(kind) ?? []), id]);
    }
    return [...by.entries()];
  }, [ids]);

  return (
    <main className="e-badge-gallery">
      {!embedded && <h1 className="e-badge-gallery__title">BADGES</h1>}
      <p className="e-badge-gallery__lede">
        Emporia's pixel badges: ink in the text colour, one accent from the tint, no background. {ids.length} badges,
        drawn at whole-number scales. Season-0 is an open testnet season; badges move no attribute, ladder or points.
      </p>
      <div className="e-badge-gallery__bar" role="group" aria-label="Show the page in">
        {!embedded && (
          <>
            <button type="button" aria-pressed={theme === "theme-light"} onClick={() => setTheme("theme-light")}>light</button>
            <button type="button" aria-pressed={theme === "theme-dark"} onClick={() => setTheme("theme-dark")}>dark</button>
            {TINTS.map((t) => (
              <button key={t || "amber"} type="button" aria-pressed={tint === t} onClick={() => setTint(t)}>
                {TINT_LABELS[t].toLowerCase()}
              </button>
            ))}
          </>
        )}
        <button type="button" onClick={() => setPopKey((k) => k + 1)}>replay the pop</button>
      </div>

      {groups.map(([kind, list]) => (
        <section key={kind}>
          <h2>{(KIND_TITLES[kind] ?? kind).toUpperCase()}</h2>
          <div className="e-badge-scroll">
            <table className="e-badge-table">
              <thead>
                <tr><th>1× · 2× · 3×</th><th>name</th><th>id (and other spellings)</th><th>how it is earned</th></tr>
              </thead>
              <tbody>
                {list.map((id) => {
                  const d = resolveBadge(id).def;
                  return (
                    <tr key={id}>
                      <td>
                        <div className="e-badge-table__scales">
                          <Badge id={id} size={1} />
                          <Badge id={id} size={2} />
                          <Badge key={`${id}.${popKey}`} id={id} size={3} pop={popKey > 0} />
                        </div>
                      </td>
                      <td>{d.name}</td>
                      <td><code>{[id, ...d.aliases].join(" · ")}</code></td>
                      <td>{d.how}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      ))}

      <h2>EVERY TINT</h2>
      <p className="e-badge-gallery__lede">
        Each panel carries its own theme and tint; the lowest contrast any badge colour meets on its canvas, panel and
        row wash is printed on it (WCAG non-text: 3:1).
      </p>
      <div className="e-badge-panels">
        {(["theme-light", "theme-dark"] as const).flatMap((th) => TINTS.map((t) => {
          const name: ThemeName = th === "theme-light" ? "light" : "dark";
          const low = lowest(name, t);
          return (
            <div key={`${th}${t}`} className={`e-badge-panel ${th}`} style={{ "--tint": TINT_COLORS[t] } as CSSProperties}>
              <div className="e-badge-panel__head">
                <span>{name} · {TINT_LABELS[t].toLowerCase()}</span>
                <span>ink ≥ {low.ink.toFixed(1)} · accent ≥ {low.accent.toFixed(1)}</span>
              </div>
              <div className="e-badge-panel__grid">
                {ids.map((id) => <Badge key={id} id={id} size={2} />)}
              </div>
              <div className="e-badge-panel__grid e-badge-panel__grid--panel">
                {ids.map((id) => <Badge key={id} id={id} size={1} />)}
              </div>
            </div>
          );
        }))}
      </div>
    </main>
  );
}
