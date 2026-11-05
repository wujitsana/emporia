import type { Listing } from "./api";
import { sessionSection, type NavOpts, type NavTarget } from "./eventNav";

function listingPeek(l: Listing): NavOpts["listingPeek"] {
  return {
    title: l.title,
    description: l.description,
    agentId: l.agent_id,
    moduleType: l.module_type,
  };
}

/** Where a listing row should open in the dashboard. */
export function viewForListing(l: Listing): { view: NavTarget; opts?: NavOpts } {
  if (l.listing_type === "room") {
    return { view: "rooms", opts: { roomId: l.listing_id, listingPeek: listingPeek(l) } };
  }
  if (l.listing_type === "event") {
    return { view: "tournaments", opts: { listingPeek: listingPeek(l) } };
  }
  // A game challenge opens Games; a service, code review or research offer opens Deals.
  return {
    view: sessionSection(l.module_type),
    opts: {
      gameModuleType: l.module_type ?? "",
      listingPeek: listingPeek(l),
    },
  };
}
