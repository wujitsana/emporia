/**
 * The Desktop page's whole stylesheet, scoped under `.e-desktop-root`.
 * vite.desktop.config.ts replaces this placeholder with the built CSS (the kit's,
 * the themes', the views'), so plugin.js stays one file. The page renders it in
 * a <style> inside its own root (desktop/mount.tsx), mounted and removed with it.
 */
export const DESKTOP_CSS: string = "__EMPORIA_DESKTOP_CSS__";

/**
 * Desktop's own global CSS reaches the page (there is no shadow root), so where
 * the app leaned on a browser default that Desktop's base styles change, it is
 * set again here, inside the page only.
 */
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
`;
