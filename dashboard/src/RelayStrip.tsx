import BlockLoader from "@kit/BlockLoader";
import Text from "@kit/Text";
import type { ReactNode } from "react";
import type { RelayHealth } from "./hooks";
import { toWsUrl } from "./hooks";
import { resolveRelayUrl } from "./relayEnv";
import { relayHost } from "./relayFormat";
import { MODE, socketsCarried } from "./transport";

// P42: internal to this file — see the note on trustBadge in AgentsView.
function relayWsOrigin(httpRelay?: string): string {
  const relay = httpRelay && httpRelay.length > 0 ? httpRelay : resolveRelayUrl();
  return toWsUrl(relay).replace(/\/$/, "");
}

function relayEventsWsUrl(httpRelay?: string): string {
  return `${relayWsOrigin(httpRelay)}/ws/events`;
}

function StatusChip({
  ok,
  label,
  title,
  loading,
  liveLoader,
}: {
  ok: boolean;
  label: string;
  title: string;
  loading?: boolean;
  /** When connected/ok, show the kit's BlockLoader instead of a static dot (events stream). */
  liveLoader?: boolean;
}) {
  let indicator: ReactNode;
  if (loading) {
    indicator = (
      <span className="e-status-chip__loader" aria-hidden>
        <BlockLoader mode={0} />
      </span>
    );
  } else if (ok && liveLoader) {
    indicator = (
      <span className="e-status-chip__loader" aria-hidden>
        <BlockLoader mode={0} />
      </span>
    );
  } else {
    indicator = (
      <span className="e-status-chip__dot" aria-hidden>
        {ok ? "●" : "○"}
      </span>
    );
  }

  return (
    <span className={`e-status-chip${ok ? " is-ok" : ""}`} title={title}>
      {indicator}
      <span className="e-status-chip__label">{label}</span>
    </span>
  );
}

/**
 * Relay connectivity in the header.
 * - REST / “API”: HTTP GET /health (relay up, version, guardrails).
 * - events: WebSocket /ws/events (live session/listing/event pushes to the dashboard).
 */
export function RelayStrip({
  relayUrl,
  wsConnected,
  health,
  compact = false,
  showWs = false,
}: {
  relayUrl: string;
  wsConnected: boolean;
  health: RelayHealth | null;
  compact?: boolean;
  showWs?: boolean;
}) {
  const online = health?.status === "ok";
  const resolved = relayUrl && relayUrl.length > 0 ? relayUrl : resolveRelayUrl();
  // The Desktop page has no address of its own: it reaches the relay through Hermes.
  const host = MODE === "desktop" ? "via Hermes" : relayHost(resolved);
  const wsEvents = relayEventsWsUrl(resolved);

  const meta = [health?.version ? `v${health.version}` : null, health?.guardrails_mode]
    .filter(Boolean)
    .join(" · ");

  const restTitle = online
    ? `Relay is up — its API answers (${resolved}/health)`
    : health
      ? `Relay HTTP unreachable (${resolved})`
      : `Checking relay health…`;

  const wsTitle = wsConnected
    ? `Live updates on: new games, listings and events appear as they happen (${wsEvents})`
    : !socketsCarried()
      ? "Live updates off: this Hermes profile runs the plugin in its plugin host, which carries no websockets yet. The dashboard refreshes on a timer."
      : `Live updates off — the dashboard still refreshes on a timer (${wsEvents})`;

  const chips = (
    <span className="e-relay-status__chips">
      <StatusChip
        ok={online}
        label="relay"
        title={restTitle}
        loading={health === null}
      />
      <StatusChip ok={wsConnected} label="live" title={wsTitle} liveLoader={wsConnected} />
    </span>
  );

  if (compact) {
    return (
      <span className="e-relay-status e-relay-status--compact">
        <span className="e-relay-status__host e-capture-hide" title={resolved}>
          {host}
        </span>
        {chips}
      </span>
    );
  }

  return (
    <span className="e-relay-status" title={resolved}>
      <span className="e-relay-status__host e-capture-hide">{host}</span>
      {meta ? <span className="e-relay-status__meta">{meta}</span> : null}
      {chips}
      {showWs ? <Text className="e-faint">{wsEvents}</Text> : null}
    </span>
  );
}