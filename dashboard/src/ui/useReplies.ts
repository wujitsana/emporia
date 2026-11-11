import { useEffect, useState } from "react";
import { api, type FeedPost } from "../api";

/* Replies are read once per post (and count) and kept: a 15 s refresh of the Feed must not re-ask. */
const THREADS = new Map<string, FeedPost[]>();

export function useReplies(post_id: string, count: number): FeedPost[] {
  const key = `${post_id}:${count}`;
  const [loaded, setLoaded] = useState<{ key: string; list: FeedPost[] } | null>(null);
  useEffect(() => {
    if (count <= 0 || THREADS.has(key)) return;
    let live = true;
    api.postThread(post_id)
      .then((t) => { THREADS.set(key, t.thread ?? []); if (live) setLoaded({ key, list: t.thread ?? [] }); })
      .catch(() => {});
    return () => { live = false; };
  }, [post_id, count, key]);
  if (count <= 0) return [];
  return THREADS.get(key) ?? (loaded?.key === key ? loaded.list : []);
}
