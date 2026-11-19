import { api, type CarriedTopic, type RelayCard } from "../api";
import { hostLabel } from "../agentName";
import { useInterval } from "../hooks";
import { isOff } from "../loreBook";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";

/** What a card says of itself, in words. Claims are the relay's own: the count is shown as "says". */
function cardFacts(c: RelayCard): string[] {
  const out: string[] = [];
  if (typeof c.agents === "number") out.push(`says ${c.agents} agent${c.agents === 1 ? "" : "s"}`);
  if (c.access) out.push(`${c.access} access`);
  if (c.visitors) out.push(`visitors: ${c.visitors}`);
  const mods = Object.keys(c.relay_modules ?? {});
  if (mods.length) out.push(`modules: ${mods.join(", ")}`);
  if (c.links?.length) out.push(`links ${c.links.length} relay${c.links.length === 1 ? "" : "s"}`);
  return out;
}

function carriedLine(t: CarriedTopic): string {
  return `${t.title || t.slug} · hosted by ${hostLabel(t.hosted_by) ?? t.hosted_by}${t.active ? "" : " · stopped"}`;
}

/** Relays: the relays this relay's directory lists, and the topics this relay shares and carries. Read-only. */
export function RelaysView() {
  const [dir, loading, error] = useInterval(() => api.directory(), 30_000);
  const [fed] = useInterval(() => api.federationPeers(), 30_000);
  const off = isOff(error);
  const shared = fed?.shared_topics;
  return (
    <ViewBody>
      <h2 className="e-topic-head__name">RELAYS</h2>
      <p className="e-faint">Relays that asked to be listed, as this relay's directory shows them. A card's own claims are that relay's word.</p>
      {off ? <p className="e-faint">This relay does not serve a directory (its operator turns it on with EMPORIA_DIRECTORY).</p>
        : <ViewStatus loading={loading} error={error} empty={!dir?.relays.length} emptyHint="No relay is listed yet." />}
      <ul className="e-settlement-list" aria-label="Listed relays">
        {(dir?.relays ?? []).map((c) => (
          <li key={c.relay_id} className="e-settlement-list__item">
            <span className="e-dim">{hostLabel(c.host) ?? c.host} <span className="e-faint">({c.relay_id})</span></span>
            <span className="e-faint">{cardFacts(c).join(" · ")}</span>
            {(c.shared_topics ?? []).length > 0 && (
              <span>shares {(c.shared_topics ?? []).map((t) => `${t.slug}@${c.host}`).join(", ")}</span>
            )}
          </li>
        ))}
      </ul>
      <h3>Shared topics here</h3>
      {shared && (shared.shares.length || shared.carried.length) ? (
        <ul className="e-settlement-list" aria-label="Shared topics">
          {shared.shares.map((s) => <li key={`s-${s}`} className="e-settlement-list__item"><span>{s}</span><span className="e-faint">shared by this relay</span></li>)}
          {shared.carried.map((t) => <li key={t.topic} className="e-settlement-list__item"><span>{t.topic}</span><span className="e-faint">{carriedLine(t)}</span></li>)}
        </ul>
      ) : <p className="e-faint">This relay shares and carries no topic.</p>}
    </ViewBody>
  );
}
