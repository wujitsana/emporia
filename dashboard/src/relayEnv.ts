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
export const VIEWER = import.meta.env.VITE_AGENT_ID ?? "dashboard";