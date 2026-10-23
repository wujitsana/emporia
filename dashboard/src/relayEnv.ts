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

/**
 * P123: `?as=<agent>` chooses who this dashboard views as, overriding the
 * build-time VITE_AGENT_ID — so one dashboard can look at alpha, beta or the
 * operator. It grants nothing: the relay decides what each identity may see,
 * trusting the header only from this machine (EMPORIA_TRUST_LOCAL_HEADER_AUTH)
 * and otherwise asking that agent to sign the dashboard challenge.
 */
export function viewerFromUrl(search: string): string | null {
  const v = new URLSearchParams(search).get("as")?.trim().toLowerCase() ?? "";
  return /^[a-z0-9][a-z0-9_-]{1,62}[a-z0-9]$/.test(v) ? v : null;
}

export const VIEWER_ID: string | null = resolveViewerId(
  (typeof window !== "undefined" ? viewerFromUrl(window.location.search) : null)
    ?? (import.meta.env.VITE_AGENT_ID as string | undefined),
);

/** sessionStorage key for the dashboard JWT — per viewer (P123), so a token
 *  signed for one agent is never sent while viewing as another. */
export const SESSION_TOKEN_KEY = `emporia_dashboard_token:${VIEWER_ID ?? ""}`;