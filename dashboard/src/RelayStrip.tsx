import BlockLoader from "@components/BlockLoader";
import Text from "@components/Text";
import type { ReactNode } from "react";
import type { RelayHealth } from "./hooks";
import { toWsUrl } from "./hooks";
import { resolveRelayUrl } from "./relayEnv";

export function relayHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url.replace(/^https?:\/\//, "").slice(0, 48);
  }
}

export function relayWsOrigin(httpRelay?: string): string {
  const relay = httpRelay && httpRelay.length > 0 ? httpRelay : resolveRelayUrl();
  return toWsUrl(relay).replace(/\/$/, "");
}

export function relayEventsWsUrl(httpRelay?: string): string {
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
  /** When connected/ok, show SRCL BlockLoader instead of a static dot (events stream). */
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
  const host = relayHost(resolved);
  const wsEvents = relayEventsWsUrl(resolved);

  const meta = [health?.version ? `v${health.version}` : null, health?.guardrails_mode]
    .filter(Boolean)
    .join(" · ");

  const restTitle = online
    ? `Relay REST API is up (${resolved}/health)`
    : health
      ? `Relay HTTP unreachable (${resolved})`
      : `Checking relay health…`;

  const wsTitle = wsConnected
    ? `Live event stream connected: ${wsEvents}`
    : `Event stream offline — ${wsEvents}`;

  const chips = (
    <span className="e-relay-status__chips">
      <StatusChip
        ok={online}
        label="API"
        title={restTitle}
        loading={health === null}
      />
      <StatusChip ok={wsConnected} label="events" title={wsTitle} liveLoader={wsConnected} />
    </span>
  );

  if (compact) {
    return (
      <span className="e-relay-status e-relay-status--compact">
        <span className="e-relay-status__host" title={resolved}>
          {host}
        </span>
        {chips}
      </span>
    );
  }

  return (
    <span className="e-relay-status" title={resolved}>
      <span className="e-relay-status__host">{host}</span>
      {meta ? <span className="e-relay-status__meta">{meta}</span> : null}
      {chips}
      {showWs ? <Text className="e-faint">{wsEvents}</Text> : null}
    </span>
  );
}