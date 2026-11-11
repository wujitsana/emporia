import { useLabIdeas } from "../labIdeas";
import { goToHash, hashForLab } from "../dashboardRoute";
import { refText } from "../labsFormat";
import { relativeTime } from "../postCardModel";
import type { Navigate } from "../navigation";
import type { CardSpec } from "./homeCardsModel";

/**
 * A post's place in a lab: chip ADOPTED or IN LAB, and one line under the body that opens the lab.
 * A post with no marker draws nothing (never "not adopted").
 */
export function useIdeaMarker(post: { post_id: string }, _navigate?: Navigate): { line?: CardSpec["marker"]; chip?: { text: string; outline?: boolean } } {
  const place = useLabIdeas().get(post.post_id);
  if (!place) return {};
  const open = () => goToHash(hashForLab(place.lab));
  if (place.state === "adopted") {
    const what = place.ref ? `${refText(place.ref)}${place.event ? ` ${place.event}` : ""}` : "";
    const when = place.at ? ` · ${relativeTime(place.at)}` : "";
    return { chip: { text: "ADOPTED" }, line: { text: `Adopted in ${place.labName}${what ? ` · ${what}` : ""}${when}`, onOpen: open } };
  }
  return { chip: { text: "IN LAB", outline: true }, line: { text: `Being discussed in the lab ${place.labName}`, onOpen: open } };
}
