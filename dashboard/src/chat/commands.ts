/**
 * Slash commands in the chat. Hermes owns them: its catalog (`commands.catalog`)
 * lists them, `complete.slash` completes them, `slash.exec` runs them. This file
 * only decides which few the drawer handles itself and which the catalog marks
 * as terminal-only, so those say so instead of failing silently.
 */

/** Handled by the drawer, the way Hermes's own clients handle them locally. */
export const LOCAL_COMMANDS: Record<string, string> = {
  "/new": "Start a new session",
  "/reset": "Start a new session",
  "/clear": "Start a new session",
  "/resume": "Pick a session of this profile",
  "/sessions": "Pick a session of this profile",
  "/stop": "Stop the running turn",
  "/retry": "Send your last message again",
};

/** What the catalog's `desktop` field says about a command outside a terminal. */
export type Reach = "ok" | "terminal" | "voice" | "settings";

export function reachOf(meta: { desktop?: string | null } | undefined): Reach {
  switch (meta?.desktop) {
    case "terminal":
      return "terminal";
    case "composer-voice":
      return "voice";
    case "settings":
      return "settings";
    default:
      return "ok";
  }
}

export function reachNote(cmd: string, reach: Reach): string {
  switch (reach) {
    case "terminal":
      return `${cmd} works in Hermes's terminal chat only (hermes --tui), not here.`;
    case "voice":
      return `${cmd}: voice is not in this chat yet. Use Hermes's own chat for voice.`;
    case "settings":
      return `${cmd}: sign-in lives in Hermes's settings, not in this chat.`;
    default:
      return "";
  }
}

export interface Catalog {
  /** canonical name → reach, aliases resolved */
  reach: Map<string, Reach>;
  canon: Map<string, string>;
}

export function parseCatalog(result: unknown): Catalog {
  const r = (result ?? {}) as {
    commands?: Record<string, { desktop?: string | null } | null>;
    canon?: Record<string, string>;
  };
  const reach = new Map<string, Reach>();
  for (const [name, meta] of Object.entries(r.commands ?? {})) reach.set(name, reachOf(meta ?? undefined));
  const canon = new Map<string, string>(Object.entries(r.canon ?? {}));
  return { reach, canon };
}

/** `/name rest` → ["/name", "rest"]; null when the text is not a command. */
export function splitCommand(text: string): [string, string] | null {
  const t = text.trim();
  if (!t.startsWith("/") || t.startsWith("//")) return null;
  const m = /^(\/[^\s]+)\s*([\s\S]*)$/.exec(t);
  return m ? [m[1].toLowerCase(), m[2]] : null;
}

export function classify(cmd: string, catalog: Catalog | null): { local: boolean; reach: Reach } {
  const canonical = catalog?.canon.get(cmd) ?? cmd;
  if (cmd in LOCAL_COMMANDS || canonical in LOCAL_COMMANDS) return { local: true, reach: "ok" };
  return { local: false, reach: catalog?.reach.get(cmd) ?? catalog?.reach.get(canonical) ?? "ok" };
}

export interface CompletionRow {
  text: string;
  display: string;
  meta: string;
  kind: string;
  reach: Reach;
}

/** `complete.slash` items, marked with what the catalog says about them. */
export function completionRows(result: unknown, catalog: Catalog | null): CompletionRow[] {
  const items = ((result ?? {}) as { items?: { text: string; display?: string; meta?: string; kind?: string | null }[] })
    .items ?? [];
  return items.slice(0, 12).map((it) => {
    const display = it.display || `/${it.text}`;
    const name = display.split(/\s/)[0];
    return {
      text: it.text,
      display,
      meta: it.meta ?? "",
      kind: it.kind ?? "",
      reach: name in LOCAL_COMMANDS ? "ok" : (catalog?.reach.get(name) ?? "ok"),
    };
  });
}
