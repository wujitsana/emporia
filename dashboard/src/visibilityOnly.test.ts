/**
 * Review pass 2: the dashboard is visibility-only. It reads the relay; it never
 * writes to it, votes, or speaks on a socket beyond a keep-alive ping.
 *
 * The console refuses every non-GET to the relay (console/app.py), but the
 * dashboard served by a relay has no console in between, so the source is held
 * to it too. Every request that is not a GET in dashboard/src is one of:
 * - the local console opening sealed text with the viewer's own key (`…/open`), on this machine;
 * - the tab's socket ticket (`/_ws_ticket`, transport.ts), a POST because it mints a 30-second
 *   in-memory credential in the plugin's own process: nothing is stored or changed anywhere.
 * The chat drawer's socket is Hermes's own chat (/api/ws), not the relay's.
 */
import { describe, expect, it } from "vitest";

type Fs = { readFileSync(p: string, enc: "utf8"): string; readdirSync(p: string, o: { recursive: true }): string[] };
const proc = (globalThis as unknown as { process: { cwd(): string; getBuiltinModule(m: string): unknown } }).process;
const fs = proc.getBuiltinModule("node:fs") as Fs;
const SRC = `${proc.cwd()}/src`;
const FILES = fs.readdirSync(SRC, { recursive: true })
  .filter((f) => /\.(ts|tsx)$/.test(f) && !/\.test\.tsx?$/.test(f) && !f.endsWith(".d.ts"))
  .map((f) => ({ file: f, text: fs.readFileSync(`${SRC}/${f}`, "utf8") }));

describe("the dashboard only reads", () => {
  it("writes only to the local console's /open routes and the tab's socket ticket", () => {
    const found: string[] = [];
    for (const { file, text } of FILES) {
      for (const m of text.matchAll(/method:\s*["'`](POST|PUT|PATCH|DELETE)["'`]/g)) {
        const around = text.slice(Math.max(0, m.index! - 400), m.index! + 200);
        const ok = (/consoleUrl\(`\/(dm|room|session)\/[^`]*\/open`\)/.test(around))
          || (file === "transport.ts" && /_ws_ticket/.test(around));
        if (!ok) found.push(`${file}: ${m[0]}`);
      }
    }
    expect(found).toEqual([]);
  });

  it("api.ts posts nothing", () => {
    const api = FILES.find((f) => f.file === "api.ts")!.text;
    expect(/post<|method:\s*["'`]POST/.test(api)).toBe(false);
  });

  it("sends nothing on a relay socket but a keep-alive ping", () => {
    const found: string[] = [];
    for (const { file, text } of FILES) {
      if (file.startsWith("chat/")) continue;            // Hermes's own chat socket, not the relay's
      for (const m of text.matchAll(/\.send\(([^)]*)\)/g)) {
        if (m[1] !== `'{"type":"ping"}'`) found.push(`${file}: ${m[0]}`);
      }
    }
    expect(found).toEqual([]);
  });
});
