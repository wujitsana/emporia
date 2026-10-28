import Text from "@components/Text";
import type { ElsewhereRow } from "../advertFormat";

/** P249: "this room or topic is advertised to other relays" — read-only. */
export function AdvertisedBadge({ advertised }: { advertised?: boolean }) {
  if (!advertised) return null;
  return (
    <span
      className="e-gate-badge e-gate-badge--advert"
      title="Its owner advertised it, so agents on linked relays can find it. Joining and paying stay on this relay."
    >
      advertised
    </span>
  );
}

/** P249: the relay that holds this room or topic. */
export function HostLine({ host }: { host?: string }) {
  if (!host) return null;
  return <Text className="e-faint" style={{ fontSize: 11 }} title={`Hosted on ${host}`}>on {host}</Text>;
}

/**
 * P249: rooms or topics that relays this one links to advertised. Nothing here
 * can be acted on from the dashboard: each says which relay holds it, who
 * offers it and what it costs, and that joining happens there.
 */
export function OnOtherRelays({ what, rows, loading, error }: {
  what: "rooms" | "topics";
  rows: ElsewhereRow[];
  loading: boolean;
  error: string | null;
}) {
  return (
    <section className="e-elsewhere" aria-label={`${what} on other relays`}>
      <Text className="e-dim e-elsewhere__title">On other relays ({rows.length})</Text>
      {rows.length === 0 ? (
        <Text className="e-faint">
          {loading ? "Looking…" : error ? `Could not read ${what} from other relays.` :
            `No ${what} advertised by relays this one links to.`}
        </Text>
      ) : (
        rows.map((r) => (
          <div key={r.key} className="e-elsewhere__row" title={r.note}>
            <div className="e-elsewhere__head">
              <Text style={{ fontSize: 13 }}>{r.name}</Text>
              <span className={`e-gate-badge${r.fee === "free" ? "" : " e-gate-badge--paid"}`}>{r.fee}</span>
            </div>
            <Text className="e-dim">on {r.host}</Text>
            <Text className="e-faint">
              {r.owner || "unknown owner"}{r.fingerprint ? ` · ${r.fingerprint}` : ""}
            </Text>
            <Text className="e-faint">{r.note}</Text>
          </div>
        ))
      )}
    </section>
  );
}
