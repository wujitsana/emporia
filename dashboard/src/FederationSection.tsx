import Text from "@components/Text";
import { useInterval } from "./hooks";
import { api } from "./api";
import { SlimCard } from "./ui/cards";
import { relayHost } from "./RelayStrip";

/** Federation gossip — configured peers and the outcome of the last pull. */
export function FederationSection() {
  const [status] = useInterval(() => api.federationPeers(), 15_000);

  if (status?.standalone) {
    return (
      <SlimCard
        title="Federation"
        foot="No FEDERATED_RELAYS configured — this node runs standalone. Listings stay local."
      >
        <Text className="e-faint">Standalone node — not federated with any peer relay.</Text>
      </SlimCard>
    );
  }

  return (
    <SlimCard
      title="Federation"
      foot="Each node pulls peers' /gaming/v1/federate/listings on a timer; origin_relay prevents gossip loops."
    >
      <div className="e-kpi-row">
        <div className="e-kpi">
          <span className="e-kpi__val">{status?.peers.length ?? 0}</span>
          <span className="e-kpi__lbl">peers</span>
        </div>
        <div className="e-kpi">
          <span className="e-kpi__val">{status?.imported_listing_count ?? 0}</span>
          <span className="e-kpi__lbl">imported listings</span>
        </div>
      </div>
      {status?.peers && status.peers.length > 0 && (
        <ul className="e-settlement-list">
          {status.peers.map((p) => (
            <li key={p.url} className="e-settlement-list__item">
              <Text className="e-dim">{relayHost(p.url)}</Text>
              <Text className={p.ok ? undefined : "e-faint"}>
                {p.ok === null ? "not synced yet" : p.ok ? `${p.imported} imported` : "unreachable"}
              </Text>
              <Text className="e-faint">
                {p.synced_at ? new Date(p.synced_at).toLocaleTimeString() : "—"}
              </Text>
            </li>
          ))}
        </ul>
      )}
    </SlimCard>
  );
}
