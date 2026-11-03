#!/usr/bin/env node
// `tsc --noEmit` from the package root type-checks nothing at all — the root
// tsconfig.json is solution-style (`"files": []` + `references`), and under
// `--noEmit` (as opposed to `--build`) tsc honors that and exits 0 having
// looked at zero files. This runs the real project config directly and fails
// on any first-party (src/) error, while treating errors inside node_modules as
// informational. (Until P283 those were the 34 in srcl's raw TypeScript; with the
// kit in src/ui/kit, `npm run typecheck` passes outright.)
import { spawnSync } from "node:child_process";

const result = spawnSync(
  process.platform === "win32" ? "npx.cmd" : "npx",
  ["tsc", "-p", "tsconfig.app.json", "--noEmit"],
  { encoding: "utf8" }
);

// Each diagnostic starts with an unindented "path(line,col): error TSxxxx: ...";
// tsc wraps long messages onto indented continuation lines with no file prefix
// of their own, so those must stay grouped with the diagnostic they belong to
// rather than being counted (or misclassified) as separate entries.
const rawLines = `${result.stdout || ""}${result.stderr || ""}`.split("\n");
const blocks = [];
for (const line of rawLines) {
  if (line.trim().length === 0) continue;
  if (/^npm (notice|warn) /.test(line)) continue; // npm 12 prints these to stderr; not tsc's
  if (/^\S/.test(line)) {
    blocks.push([line]);
  } else if (blocks.length > 0) {
    blocks[blocks.length - 1].push(line);
  } else {
    blocks.push([line]);
  }
}

const firstPartyBlocks = blocks.filter((b) => !b[0].startsWith("node_modules/"));
const vendorBlocks = blocks.filter((b) => b[0].startsWith("node_modules/"));

if (firstPartyBlocks.length > 0) {
  console.error(firstPartyBlocks.map((b) => b.join("\n")).join("\n"));
  console.error(`\n${firstPartyBlocks.length} first-party TypeScript error(s) in src/.`);
  process.exit(1);
}

if (vendorBlocks.length > 0) {
  console.log(
    `typecheck:src passed (${vendorBlocks.length} error(s) in node_modules ignored).`
  );
} else {
  console.log("typecheck:src passed (0 errors).");
}
process.exit(0);
