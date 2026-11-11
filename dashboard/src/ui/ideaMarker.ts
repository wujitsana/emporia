import type { PostCardData } from "../postCardModel";
import type { Navigate } from "../navigation";
import type { CardSpec } from "./homeCardsModel";

/** A post's place in a lab (adopted, or being discussed there); nothing for a post with no marker. */
export function useIdeaMarker(_post: Pick<PostCardData, "post_id">, _navigate?: Navigate): { line?: CardSpec["marker"]; chip?: { text: string; outline?: boolean } } {
  return {};
}
