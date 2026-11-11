import { useEffect, useState } from "react";
import { api } from "./api";

/*
 * Topic names by slug, read once from the topic list and shared, so every card writes a topic the same way:
 * `Ideas`, never `ideas` in one place and `Ideas` in another. A slug stays the fallback until the list is in.
 */
const NAMES = new Map<string, string>();
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();

function load(): void {
  if (loading) return;
  try {
    loading = Promise.resolve(api.agoraTopics())
      .then((d) => { for (const t of d?.topics ?? []) NAMES.set(t.slug, t.name); listeners.forEach((f) => f()); })
      .catch(() => { loading = null; });
  } catch {
    loading = null;
  }
}

/** The name of the topic with this slug, or the slug while the names load (or when there is no such topic). */
export function topicLabel(slug: string | null | undefined): string {
  return slug ? (NAMES.get(slug) ?? slug) : "";
}

/** Re-renders when the names arrive; returns `topicLabel`. */
export function useTopicLabel(): (slug: string | null | undefined) => string {
  const [, bump] = useState(0);
  useEffect(() => {
    load();
    const f = () => bump((n) => n + 1);
    listeners.add(f);
    return () => { listeners.delete(f); };
  }, []);
  return topicLabel;
}

/** Test hook: forget what was read. */
export function resetTopicNames(): void { NAMES.clear(); loading = null; }
