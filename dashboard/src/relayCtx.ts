/**
 * The relay context's value, its React context object, and the hook that reads
 * it — everything except the provider component.
 *
 * P42: `useRelayCtx` used to live beside `RelayContextProvider`, which put this
 * file in the Fast-Refresh warning list: a module exporting both a component and
 * a non-component loses hot reloading for the component. Splitting is the actual
 * fix rather than a suppression, and it is the one case among the six where a
 * split was genuinely needed — the other five were exports nothing outside the
 * file imported.
 */
import { createContext, useContext } from "react";
import { RELAY } from "./relayEnv";

/**
 * viewerId  — who is using THIS dashboard (VITE_AGENT_ID, set per profile at install time)
 * relayOwner — who operates the relay (from /ui-config; null = bare server, no agent operator)
 *
 * In a federated network, these are independent:
 *   - You (hackathon_hermes) connect to a relay run by someone else → viewerId ≠ relayOwner
 *   - You run your own relay → viewerId === relayOwner → isRelayOperator = true
 *
 * Auth note: dashboard sessions are scoped to localhost for now.
 * Production path: POST /dashboard/challenge → sign with agent Ed25519 key via MCP
 *   → POST /dashboard/session → relay issues short-lived JWT → Bearer token on requests.
 */
export interface RelayCtxValue {
  viewerId: string | null;
  relayOwner: string | null;
  relayId: string | null;
  relayUrl: string;
  requireNous: boolean;
  writeRequiresNous: boolean;
  /** P41-1: relay policy — registration must prove key possession via the
   *  challenge flow. The relay has reported this all along and nothing read it. */
  requireChallenge: boolean;
  agentCount: number;
  activeSessionCount: number;
  relayVersion: string;
  isRelayOperator: boolean;
  isSpectator: boolean;
  /** P36-5: the relay's own answer to "will you accept my X-Emporia-Agent-Id
   *  header?", per connection.
   *
   *  This replaced `isLocalRelay`, which was standing in for it and getting it
   *  wrong — header auth is off unless the operator sets
   *  EMPORIA_TRUST_LOCAL_HEADER_AUTH=1, so "the relay is on localhost" says
   *  nothing about whether it will authenticate you. `isLocalRelay` had no
   *  other reader and defaulted to `true`, i.e. it failed open; it is gone
   *  rather than left around for the next person to reach for. */
  headerAuthTrusted: boolean;
  /** Bearer JWT for remote relay auth (sessionStorage-backed) */
  dashboardToken: string | null;
  setDashboardToken: (t: string | null) => void;
  refresh: () => void;
}

const DEFAULT: RelayCtxValue = {
  viewerId: null,
  relayOwner: null,
  relayId: null,
  relayUrl: RELAY,
  requireNous: false,
  writeRequiresNous: false,
  requireChallenge: false,
  agentCount: 0,
  activeSessionCount: 0,
  relayVersion: "",
  isRelayOperator: false,
  headerAuthTrusted: false,
  isSpectator: true,
  dashboardToken: null,
  setDashboardToken: () => {},
  refresh: () => {},
};


export const RelayCtx = createContext<RelayCtxValue>(DEFAULT);

// P35-1: viewer identity now comes from relayEnv.VIEWER_ID, not a second
// private read of the same env var with its own idea of what "unset" means.


export function useRelayCtx(): RelayCtxValue {
  return useContext(RelayCtx);
}
