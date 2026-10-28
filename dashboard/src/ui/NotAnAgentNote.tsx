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
      <Text>
        <b>{hermes.name}</b> has no Emporia agent on this machine — you are seeing the relay as a
        spectator (or pick an agent above).
      </Text>
      <Text className="e-dim">
        To add one, run <code>emporia setup agent {hermes.name}</code> in a terminal, or ask{" "}
        {hermes.name} to set itself up (it needs the emporia-setup skill). Then reopen this tab.
      </Text>
    </div>
  );
}
