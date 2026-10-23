/**
 * P126: no third-party requests from the dashboard. SRCL's global-fonts.css
 * loads its typefaces from public S3 buckets, so every page view told a third
 * party that someone opened the dashboard (IP and time). Remote @font-face rules
 * are dropped at build time; the font stacks fall back to local monospace.
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
