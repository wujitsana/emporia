import BlockLoader from "@kit/BlockLoader";
import Text from "@kit/Text";
import type { ReactNode } from "react";
import type { RelayHealth } from "./hooks";
import { toWsUrl } from "./hooks";
import { resolveRelayUrl } from "./relayEnv";
import { relayLabel } from "./relayLabel";
import { socketsCarried } from "./transport";
import { moneyChip } from "./moneyChip";

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
  labelClass,
  title,
  loading,
  liveLoader,
}: {
  ok: boolean;
  label: string;
  labelClass?: string;
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
      <span className={`e-status-chip__label${labelClass ? ` ${labelClass}` : ""}`}>{label}</span>
    </span>
  );
}

/**
 * Relay connectivity in the header.
 * - REST / “API”: HTTP GET /health (relay up, its name, version).
 * - events: WebSocket /ws/events (live session pushes to the dashboard).
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
  // The relay's own name (its public host, or "local relay"), as it says it in /health. Never the
  // address this page came from: in the Hermes tab that is the Hermes dashboard's.
  const name = relayLabel(health);
  const wsEvents = relayEventsWsUrl(resolved);

  const meta = health?.version ? `v${health.version}` : "";

  const restTitle = online
    ? "The relay is up and answering"
    : health
      ? "Can't reach the relay"
      : `Checking relay health…`;

  const wsTitle = wsConnected
    ? "Live updates on: new games and deals appear as they happen"
    : !socketsCarried()
      ? "Live updates off: this Hermes profile runs the plugin in its plugin host, which carries no websockets yet. The dashboard refreshes on a timer."
      : "Live updates off: the dashboard still refreshes on a timer";

  // P6: what kind of money the amounts on screen are — test money on a testnet relay.
  const money = moneyChip(health);
  const chips = (
    <span className="e-relay-status__chips">
      {money ? (
        <span className={`e-money-chip e-money-chip--${money.kind}`} title={money.title}>
          {money.label}<span className="e-sr"> — {money.title}</span>
        </span>
      ) : null}
      <StatusChip
        ok={online}
        label={name}
        labelClass="e-status-chip__label--relay"
        title={`${name}. ${restTitle}`}
        loading={health === null}
      />
      <StatusChip ok={wsConnected} label="live" labelClass="e-status-chip__label--live" title={wsTitle} liveLoader={wsConnected} />
    </span>
  );

  if (compact) {
    return (
      <span className="e-relay-status e-relay-status--compact">
        {chips}
      </span>
    );
  }

  return (
    <span className="e-relay-status">
      {meta ? <span className="e-relay-status__meta">{meta}</span> : null}
      {chips}
      {showWs ? <Text className="e-faint">{wsEvents}</Text> : null}
    </span>
  );
}