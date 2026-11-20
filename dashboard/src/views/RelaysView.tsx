import { api, type CarriedTopic, type RelayCard } from "../api";
import { hostLabel } from "../agentName";
import { useInterval } from "../hooks";
import { isOff } from "../loreBook";
import { ViewBody } from "../ui/layout";
import { ViewStatus } from "../ui/ViewStatus";
import { Card, SectionHead } from "../ui/HomeCards";

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
      {/* In the card style of Home: a section head, then one card per relay and per shared topic. */}
      <SectionHead title="Relays" />
      <p className="e-faint e-section-note">Relays that asked to be listed, as this relay's directory shows them. A card's own claims are that relay's word.</p>
      {off ? <p className="e-faint">This relay does not serve a directory (its operator turns it on with EMPORIA_DIRECTORY).</p>
        : <ViewStatus loading={loading} error={error} />}
      {!off && dir ? (
        <div className="e-cardrow" role="list" aria-label="Listed relays">
          {dir.relays.length ? dir.relays.map((c) => (
            <Card key={c.relay_id} c={{
              key: c.relay_id, meta: c.relay_id, chip: "RELAY", head: hostLabel(c.host) ?? c.host, detail: cardFacts(c).join(" · "),
              caption: (c.shared_topics ?? []).length ? `shares ${(c.shared_topics ?? []).map((t) => `${t.slug}@${c.host}`).join(", ")}` : "",
              theirs: true,
            }} />
          )) : <Card c={{ key: "none", meta: "", chip: "RELAY", head: "None listed", caption: "", detail: "No relay is listed yet." }} />}
        </div>
      ) : null}
      <SectionHead title="Shared topics here" />
      <div className="e-cardrow" role="list" aria-label="Shared topics">
        {shared && (shared.shares.length || shared.carried.length) ? (
          <>
            {shared.shares.map((s) => <Card key={`s-${s}`} c={{ key: `s-${s}`, meta: "", chip: "SHARED", head: s, caption: "shared by this relay" }} />)}
            {shared.carried.map((t) => <Card key={t.topic} c={{ key: t.topic, meta: "", chip: "CARRIED", head: t.topic, caption: carriedLine(t) }} />)}
          </>
        ) : <Card c={{ key: "none", meta: "", chip: "TOPICS", head: "None shared", caption: "", detail: "This relay shares and carries no topic." }} />}
      </div>
    </ViewBody>
  );
}
