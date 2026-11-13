import Text from "@kit/Text";
import { relayHost } from "../relayFormat";
import { useRelayCtx } from "../relayCtx";

/** The viewed agent could not sign in to this relay: said once at the top of every view. The relay answers 401 both for an agent it does not know and for an expired or missing dashboard session, so the note names both. */
export function NotOnRelayNote() {
  const { viewerId, notRegistered, relayUrl } = useRelayCtx();
  if (!viewerId || !notRegistered) return null;
  return (
    <div role="note">
      <Text className="e-dim">
        {viewerId} could not sign in to {relayHost(relayUrl) || "this relay"}: it may not be registered there yet
        (<code>hermes emporia setup</code>), or its dashboard session has ended.
      </Text>
    </div>
  );
}
