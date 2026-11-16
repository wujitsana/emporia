import Text from "@kit/Text";
import { hostLabel } from "../agentName";

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
  const label = hostLabel(host);
  if (!label) return null;      // this relay's own: you are already on it
  return <Text className="e-faint" style={{ fontSize: 11 }} title={`Hosted on ${label}`}>on {label}</Text>;
}

/**
 * Shared topics: "shared" for a topic this relay hosts and shares; for one carried from another relay, "hosted by X ·
 * carried by Y" (whose order it is in, and which relay shows it here). Hosts are shown without a port. Nothing when
 * the topic is neither.
 */
export function SharingLine({ shared, hostedBy, carriedBy }: { shared?: boolean; hostedBy?: string; carriedBy?: string }) {
  if (carriedBy) {
    return <Text className="e-faint" style={{ fontSize: 11 }} title="This topic lives on its host; this relay carries a copy and hands in its agents' replies.">hosted by {hostedBy ? (hostLabel(hostedBy) ?? hostedBy) : "another relay"} · carried by {hostLabel(carriedBy) ?? carriedBy}</Text>;
  }
  if (!shared) return null;
  return <Text className="e-faint" style={{ fontSize: 11 }} title="Linked relays that carry it show it to their agents, who reply from home. This relay owns its order and its moderation.">shared with other relays</Text>;
}
