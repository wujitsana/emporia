import { destinationOf, type NavItem } from "./navConfig";
import { bareAgentName } from "./agentName";

/**
 * The page title, "Agora › Feed" (the place, then the section). On the Agent's pages the place is the
 * viewed agent's name ("alpha › Settings"; "Agent › Settings" when none). A place with one page (Home,
 * Arena, Docs) is just its name.
 */
export function pageTitle(item: NavItem, agent: string | null | undefined): string {
  const dest = destinationOf(item.id);
  const place = dest.id === "agent" ? (agent ? bareAgentName(agent) : "Agent") : dest.label;
  return dest.views.length === 1 || place.toLowerCase() === item.label.toLowerCase() ? item.label : `${place} › ${item.label}`;
}
