import { useEffect, useState } from "react";
import { api } from "../api";

/** The few fields of a post a lab page quotes (its kind, its first lines, who wrote it). */
export interface PostBrief { kind: string; label: string; text: string; author: string }

const BRIEFS = new Map<string, PostBrief | null>();
const PENDING = new Set<string>();

/** A post read once and kept (a lab page names many posts; each is asked for once). `undefined` while loading, `null` if unreadable. */
export function usePostBrief(id: string | null): PostBrief | null | undefined {
  const [, bump] = useState(0);
  useEffect(() => {
    if (!id || BRIEFS.has(id) || PENDING.has(id)) return;
    PENDING.add(id);
    let live = true;
    Promise.resolve(api.postThread(id))
      .then((p) => { BRIEFS.set(id, { kind: p.kind, label: p.label, text: p.text, author: p.author_id }); })
      .catch(() => { BRIEFS.set(id, null); })
      .finally(() => { PENDING.delete(id); if (live) bump((n) => n + 1); });
    return () => { live = false; };
  }, [id]);
  return id ? BRIEFS.get(id) : null;
}
