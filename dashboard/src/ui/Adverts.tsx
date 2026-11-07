import Text from "@kit/Text";

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
