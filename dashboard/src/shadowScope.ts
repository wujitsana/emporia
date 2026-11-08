/**
 * P126: rewrite a selector written for the document so it applies inside the
 * dashboard's shadow root instead (vite.plugin.config.ts runs it on every rule):
 * `html` and `:root` become `:host`, `body` becomes the app's container.
 */
const BOUNDARY = "(^|[\\s>+~(,])";
const AFTER = "(?=$|[\\s.:#\\[>+~),])";

export function scopeToShadowHost(selector: string): string {
  return selector
    .replace(/:root\b/g, ":host")
    .replace(new RegExp(`${BOUNDARY}html${AFTER}`, "g"), "$1:host")
    .replace(new RegExp(`${BOUNDARY}body${AFTER}`, "g"), "$1.e-host-body");
}

/** The Desktop page's outer element: every rule is scoped under it. */
export const DESKTOP_ROOT = "e-desktop-root";
/** Its inner element, the app's "body": themes and tints go on it (themeConfig.ts). */
export const DESKTOP_BODY = "e-host-body";

/**
 * The Desktop page has no shadow root: the app's CSS sits in the app's own
 * document (vite.desktop.config.ts runs this on every top-level rule). So
 * `html`, `:root` and `body` become the app's inner container, and every rule
 * is scoped under the outer one. Nothing reaches Desktop's own UI, and Desktop's
 * element rules lose to ours, which carry one class more.
 */
export function scopeToContainer(selector: string, opts: { cssModule?: boolean } = {}): string {
  const body = `.${DESKTOP_BODY}`;
  const s = selector.trim()
    .replace(/:root\b/g, body)
    .replace(new RegExp(`${BOUNDARY}html${AFTER}`, "g"), `$1${body}`)
    .replace(new RegExp(`${BOUNDARY}body${AFTER}`, "g"), `$1${body}`)
    // `html body` read as the container inside itself.
    .replace(new RegExp(`\\${body}(\\s*>?\\s*)\\${body}${AFTER}`, "g"), body);
  if (s.startsWith(`.${DESKTOP_ROOT}`)) return s;
  // In a CSS module the scope class is a global name, not one of the module's own.
  return opts.cssModule ? `:global(.${DESKTOP_ROOT}) ${s}` : `.${DESKTOP_ROOT} ${s}`;
}
