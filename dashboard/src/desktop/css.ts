/**
 * The Desktop page's whole stylesheet, scoped under `.e-desktop-root`.
 * vite.desktop.config.ts replaces this placeholder with the built CSS (the kit's,
 * the themes', the views'), so plugin.js stays one file. The page renders it in
 * a <style> inside its own root (desktop/mount.tsx), mounted and removed with it.
 */
export const DESKTOP_CSS: string = "__EMPORIA_DESKTOP_CSS__";

/**
 * Desktop's own global CSS reaches the page (there is no shadow root): its base
 * layer (`@layer base`: `* { border: 0 solid; margin: 0; padding: 0 }`, `svg
 * { display: block }`, transparent buttons, unstyled lists…) would otherwise
 * stand in for the browser defaults the app was drawn against, in the tab's
 * shadow root and the console alike. This rule, first in the page's sheet,
 * takes every element inside the page back to the browser's own defaults. It
 * is unlayered, so it wins over Desktop's layered base rules; and `:where()`
 * gives it no specificity, so every rule of the app's own still wins over it.
 * Inherited properties (font, colour) still come from the app's own root.
 */
export const DESKTOP_RESET = `
:where(.e-desktop-root) *,
:where(.e-desktop-root) *::before,
:where(.e-desktop-root) *::after {
  all: revert;
}
`;

/** The page's own frame inside Desktop's workspace pane. */
export const DESKTOP_POLISH = `
.e-desktop-root {
  height: 100%;
  min-height: 0;
  overflow: auto;
  position: relative;
  contain: layout paint;
  outline: none;
  text-align: left;
}
.e-desktop-root > .e-host-body {
  min-height: 100%;
  box-sizing: border-box;
}
/* Before the app has a profile: the "opening", "not set up" and "did not answer" notes, in Desktop's own colours. */
.e-desktop-root .e-desktop-note {
  max-width: 36rem;
  margin: 3rem auto;
  padding: 0 16px;
  font: 15px/1.5 system-ui, sans-serif;
  color: var(--ui-text-secondary, inherit);
}
.e-desktop-root .e-desktop-note h2 {
  font-size: 1.25rem;
  margin: 0 0 0.75rem;
  color: var(--ui-text-primary, inherit);
}
.e-desktop-root .e-desktop-note pre {
  padding: 0.75rem 1rem;
  border-radius: 6px;
  background: rgba(127, 127, 127, 0.15);
  overflow-x: auto;
}
.e-desktop-root .e-desktop-note__retry {
  cursor: pointer;
}
`;
