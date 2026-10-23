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
