/**
 * How a kit CSS-module class is named: `<Component>_<local>_<hash>`.
 *
 * dashboard.css restyles the vendored kit with selectors such as
 * `[class*="SidebarLayout_sidebar"]` and `[class*="ActionButton_content"]`.
 * Vite's default names (`_sidebar_1gl67_8`) carry no component, so after the
 * kit was vendored every one of those selectors matched nothing — the mobile
 * stacking of split views included. Every build (dev, console, Hermes tab) and
 * the tests use this one function, so the selectors and the names cannot drift
 * apart again.
 */
export function cssModuleName(local: string, filename: string, css = ""): string {
  const base = (filename.split(/[\\/]/).pop() ?? "").replace(/\?.*$/, "").replace(/\.module\.\w+$/, "").replace(/\.\w+$/, "");
  const component = base.replace(/[^A-Za-z0-9_-]/g, "") || "m";
  return `${component}_${local}_${hash(component + "\n" + css)}`;
}

/** Short, stable, CSS-safe digest (FNV-1a, base 36). */
function hash(s: string): string {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(36).slice(0, 5);
}
