import Text from "@components/Text";
import { hermesProfile } from "../transport";

/**
 * P231: the tab opened in a Hermes profile that has no Emporia agent. It used to
 * show alpha's view under that profile's name; now it says so, shows the relay
 * as a spectator sees it, and says how to set the agent up. Setting up is a
 * person's job in a terminal (it creates keys and may create wallets), or the
 * agent's own with the emporia-setup skill — never a button here.
 */
export function NotAnAgentNote() {
  const hermes = hermesProfile();
  if (!hermes || hermes.isAgent) return null;
  return (
    <div className="e-listing-peek" role="note">
      <Text className="e-dim">
        {hermes.name} is not an Emporia agent: spectator view. To add it:{" "}
        <code>emporia setup agent {hermes.name}</code>
      </Text>
    </div>
  );
}
