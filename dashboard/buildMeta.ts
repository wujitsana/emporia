/**
 * What both committed plugin bundles carry about their build, shared by
 * vite.plugin.config.ts (the dashboard tab) and vite.desktop.config.ts (the
 * Desktop page) so the two halves never disagree:
 *
 * - **the source digest** (P295): a SOURCE file next to each bundle, recomputed
 *   by tests/test_dashboard_plugin.py, which fails when dashboard/ changed and a
 *   bundle was not rebuilt (`npm run build:plugin` builds both);
 * - **the version**: read from the plugin's own plugin.yaml, the one place it is
 *   written, and compiled into both bundles (src/buildInfo.ts).
 */
import fs from "fs";
import path from "path";
import crypto from "crypto";

/** Inputs every bundle is built from; each config adds its own file. */
export const SHARED_INPUTS = ["src", "package-lock.json", "postcss.noRemoteFonts.ts", "buildMeta.ts"];

export function sourceDigest(inputs: string[]): string {
  const files: string[] = [];
  const walk = (rel: string) => {
    const st = fs.statSync(rel);
    if (st.isDirectory()) fs.readdirSync(rel).sort().forEach((n) => walk(path.posix.join(rel, n)));
    else if (!/\.test\.tsx?$/.test(rel)) files.push(rel);
  };
  inputs.forEach(walk);
  const h = crypto.createHash("sha256");
  for (const f of files.sort()) {
    h.update(f + "\n");
    h.update(fs.readFileSync(f));
  }
  return h.digest("hex");
}

/** A Vite plugin that writes the digest of `inputs` to `file` once the bundle is written. */
export function writeSourceDigest(inputs: string[], file: string) {
  return {
    name: "emporia-source-digest",
    closeBundle() {
      fs.writeFileSync(file, sourceDigest(inputs) + "\n");
    },
  };
}

const PLUGIN_YAML = path.resolve("../hermes_plugin/emporia/plugin.yaml");

/** `version:` from plugin.yaml (a top-level scalar; no YAML parser needed for it). */
export function pluginVersion(): string {
  const m = /^version:\s*["']?([0-9][0-9A-Za-z.+-]*)["']?\s*$/m.exec(fs.readFileSync(PLUGIN_YAML, "utf8"));
  if (!m) throw new Error(`no version in ${PLUGIN_YAML}`);
  return m[1];
}
