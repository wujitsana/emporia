/** Resolved HTTP base for API + WS (env, else same-origin, else dev default). */
export function resolveRelayUrl(): string {
  const env = import.meta.env.VITE_RELAY_URL;
  if (typeof env === "string" && env.length > 0) return env;
  if (typeof window !== "undefined" && window.location?.host) {
    return `${window.location.protocol}//${window.location.host}`;
  }
  return "http://127.0.0.1:8088";
}

export const RELAY = resolveRelayUrl();
/**
 * Who is using THIS dashboard, or `null` when nobody is — the single answer to
 * that question for the whole app.
 *
 * P35-1: there used to be four. `relayEnv` defaulted to the placeholder string
 * `"dashboard"`, `relayContext` defaulted to `null`, `SocialHubView` kept its
 * own private copy of the placeholder version, and `api.ts` defaulted to
 * nothing. The placeholder is what turned an inconsistency into a bug: it is
 * truthy, so every `if (!VIEWER)` guard in the app tested a branch that could
 * never be taken, while four other call sites had worked out that the
 * placeholder was lying and re-derived identity as `VIEWER !== "dashboard"`.
 *
 * The placeholder is gone rather than centralised. A sentinel that means "no
 * identity" while occupying the identity field is a thing every reader has to
 * remember, and half of them didn't. Absence is spelled `null`.
 */
export function resolveViewerId(raw: string | undefined | null): string | null {
  const v = (raw ?? "").trim();
  return v === "" ? null : v;
}

export const VIEWER_ID: string | null = resolveViewerId(
  import.meta.env.VITE_AGENT_ID as string | undefined,
);