/**
 * The relay's name in the header: its public host (`emporia.example.net`), or "local relay" when it runs
 * on this machine. It comes from what the relay says about itself (/health: `relay_url`, `host`), never
 * from the address the page was loaded from: inside the Hermes tab that is the Hermes dashboard's own
 * (`localhost:9119`), which says nothing about the relay.
 */
import type { Health } from "./api";

const LOCAL = new Set(["localhost", "0.0.0.0", "::1", "[::1]", ""]);

function hostnameOf(raw: string): string {
  const text = raw.trim();
  if (!text) return "";
  try {
    return new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `http://${text}`).hostname.toLowerCase();
  } catch {
    return "";
  }
}

function isLocalHost(name: string): boolean {
  return LOCAL.has(name) || /^127\./.test(name) || name.endsWith(".localhost");
}

/** "local relay", the relay's public host, or "relay" while it has not said who it is. */
export function relayLabel(health: Pick<Health, "host" | "relay_url"> | null | undefined): string {
  if (!health) return "relay";
  const name = hostnameOf(health.relay_url ?? "") || hostnameOf(health.host ?? "");
  if (!name && !health.relay_url && !health.host) return "relay";
  return isLocalHost(name) ? "local relay" : name;
}
