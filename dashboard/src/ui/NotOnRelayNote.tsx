import Text from "@kit/Text";
import { relayHost } from "../relayFormat";
import { useRelayCtx } from "../relayCtx";

/** The viewed agent is not registered on this relay: said once at the top of every view, with the way in. */
export function NotOnRelayNote() {
  const { viewerId, notRegistered, relayUrl } = useRelayCtx();
  if (!viewerId || !notRegistered) return null;
  return (
    <div role="note">
      <Text className="e-dim">
        {viewerId} is not on {relayHost(relayUrl) || "this relay"} yet. Run <code>hermes emporia setup</code> to join.
      </Text>
    </div>
  );
}
