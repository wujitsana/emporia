/**
 * P126: no third-party requests from the dashboard. srcl's global-fonts.css
 * loaded its typefaces from public S3 buckets, so every page view told a third
 * party that someone opened the dashboard (IP and time). P283 dropped srcl; this
 * stays as the guard: any remote @font-face rule is dropped at build time, and
 * the font stacks fall back to local monospace.
 */
import type { Plugin } from "postcss";

export const noRemoteFonts: Plugin = {
  postcssPlugin: "emporia-no-remote-fonts",
  AtRule: {
    "font-face"(rule) {
      if (/url\(\s*['"]?(https?:)?\/\//i.test(rule.toString())) rule.remove();
    },
  },
};
