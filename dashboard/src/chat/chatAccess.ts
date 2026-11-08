/**
 * Whether this dashboard can chat with an agent, and how "ask about this"
 * reaches the drawer.
 *
 * Chat exists only in the Hermes tab: there the Hermes plugin SDK opens
 * Hermes's own chat socket (`/api/ws`) with Hermes's sign-in, for the profile
 * the tab is open in. The standalone console has no Hermes chat API behind it,
 * so it shows no chat (docs/proposals/dashboard-chat.md).
 */
import { ChatGateway } from "./gateway";
import { MODE } from "../transport";

interface HermesSdkLike {
  buildWsUrl: (path: string, params?: Record<string, string>) => Promise<string>;
  authedFetch: (url: string, init?: RequestInit) => Promise<Response>;
}

/** Hermes REST reads (a relay session's transcript), with Hermes's sign-in. */
export function chatFetch(url: string): Promise<Response> {
  const s = sdk();
  return s ? s.authedFetch(url) : Promise.reject(new Error("no Hermes here"));
}

function sdk(): HermesSdkLike | null {
  if (typeof window === "undefined") return null;
  return (window as { __EMPORIA_HERMES_SDK__?: HermesSdkLike }).__EMPORIA_HERMES_SDK__ ?? null;
}

/** The Hermes profile the tab was loaded for; chat never names another. */
export function chatProfile(): string | null {
  if (typeof window === "undefined") return null;
  const p = (window as { __EMPORIA_HERMES_PROFILE__?: { name?: string } | null }).__EMPORIA_HERMES_PROFILE__;
  return p?.name ? p.name : null;
}

/** Never on the Desktop page: Desktop's own chat is the agent's, so the page has no drawer. */
export function chatAvailable(): boolean {
  if (MODE === "desktop") return false;
  return !!sdk() && !!chatProfile();
}

/** A gateway for this tab's profile, over Hermes's own chat socket. */
export function makeGateway(): ChatGateway | null {
  const s = sdk();
  const profile = chatProfile();
  if (!s || !profile) return null;
  return new ChatGateway(async () => new WebSocket(await s.buildWsUrl("/api/ws")), profile);
}

/* ── whether the drawer is open (shared by the header toggle and the drawer) ─ */

let open = false;
const openSubs = new Set<() => void>();

export function setChatOpen(v: boolean): void {
  open = v;
  openSubs.forEach((fn) => fn());
}

export function chatOpen(): boolean {
  return open;
}

export function subscribeChatOpen(fn: () => void): () => void {
  openSubs.add(fn);
  return () => openSubs.delete(fn);
}

/* ── "ask about this": fill the composer, never send ─────────────────────── */

type AskListener = (text: string) => void;
const listeners = new Set<AskListener>();

/** Open the chat with `text` in the composer. The person reads it and sends it — or not. */
export function askAgent(text: string): void {
  listeners.forEach((fn) => fn(text));
}

export function onAsk(fn: AskListener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
