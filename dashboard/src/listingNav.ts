import type { Listing } from "./api";
import type { View } from "./navConfig";
import type { NavOpts } from "./eventNav";

function listingPeek(l: Listing): NavOpts["listingPeek"] {
  return {
    title: l.title,
    description: l.description,
    agentId: l.agent_id,
    moduleType: l.module_type,
  };
}

/** Where a listing row should open in the dashboard. */
export function viewForListing(l: Listing): { view: View; opts?: NavOpts } {
  if (l.listing_type === "room") {
    return { view: "rooms", opts: { roomId: l.listing_id, listingPeek: listingPeek(l) } };
  }
  if (l.listing_type === "event") {
    return { view: "events", opts: { listingPeek: listingPeek(l) } };
  }
  // All session-type listings (chess, service, research, code-review) → Sessions view
  return {
    view: "sessions",
    opts: {
      gameModuleType: l.module_type ?? "",
      listingPeek: listingPeek(l),
    },
  };
}
