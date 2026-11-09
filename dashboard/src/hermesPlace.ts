/**
 * Where a person changes an Emporia setting in Hermes itself.
 *
 * Hermes reads the plugin's profile settings from plugin.yaml (`optional_env`) and shows them on the
 * Emporia card of its Channels page: `/channels` in the web dashboard (its Keys page leaves a
 * platform's own settings to that card), `/messaging` in the Desktop app. The Settings view links
 * there and changes nothing itself: the dashboard only watches.
 */
import { MODE, type Mode } from "./transport";
import { desktopBridge } from "./desktop/bridge";

declare global {
  interface Window {
    /** Set by Hermes's server when its dashboard is served under a prefix ("" at the root). */
    __HERMES_BASE_PATH__?: string;
  }
}

export interface HermesPlace {
  /** Where it is, in words — shown wherever no link can be made (the standalone console). */
  text: string;
  /** The web dashboard's page, scoped to the agent's profile. */
  href?: string;
  /** The Desktop app's page, opened through Desktop's own router. */
  open?: () => void;
}

export const HERMES_PLACE = "Hermes ▸ Channels ▸ Emporia";
export const DESKTOP_PLACE = "Hermes ▸ Messaging ▸ Emporia";

/** Hermes's prefix, normalised the way its own dashboard does (leading slash, no trailing one). */
export function hermesBasePath(raw: string | undefined = typeof window !== "undefined"
  ? window.__HERMES_BASE_PATH__ : ""): string {
  const v = (raw ?? "").trim();
  if (!v) return "";
  return (v.startsWith("/") ? v : `/${v}`).replace(/\/+$/, "");
}

/** The place for an agent whose Hermes profile is `profile` (the root profile's folder starts with a dot,
 *  and Hermes calls it by no name: its page opens unscoped). */
export function hermesPlace(profile: string, mode: Mode = MODE): HermesPlace {
  if (mode === "hermes") {
    const scoped = profile && !profile.startsWith(".") ? `?profile=${encodeURIComponent(profile)}` : "";
    return { text: HERMES_PLACE, href: `${hermesBasePath()}/channels${scoped}` };
  }
  if (mode === "desktop") {
    const navigate = desktopBridge()?.navigate;
    return { text: DESKTOP_PLACE, open: navigate ? () => navigate("/messaging") : undefined };
  }
  return { text: HERMES_PLACE };
}
