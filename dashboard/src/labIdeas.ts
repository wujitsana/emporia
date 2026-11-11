import { useEffect, useState } from "react";
import { api } from "./api";
import { postRefId } from "./labsFormat";

/** Where an idea post stands in a lab: named by a maintainer's report (adopted), or the root of a lab thread (in the lab). */
export interface IdeaPlace {
  lab: string;
  labName: string;
  state: "adopted" | "in_lab";
  /** Adopted: the work it was adopted in (`gh_pr:owner/repo#12`) and what happened to it (`merged`, `fixed`). */
  ref?: string;
  event?: string;
  at?: string;
  /** In the lab: how many messages its thread has. */
  messages?: number;
}

/*
 * One shared index for the whole page: ideas by post id, read from the labs' reports and threads and refreshed
 * every minute while any card wants it. A relay without labs (404), or a lab whose threads are private,
 * simply adds nothing. No new route: the reports and threads are public reads the Labs page already makes.
 */
const EVERY_MS = 60_000;
let index = new Map<string, IdeaPlace>();
const subscribers = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | null = null;
let busy = false;

async function refresh(): Promise<void> {
  if (busy) return;
  busy = true;
  try {
    const next = new Map<string, IdeaPlace>();
    const { labs } = await api.labs();
    for (const lab of labs ?? []) {
      const [reports, threads] = await Promise.all([
        api.labReports(lab.slug).catch(() => null),
        api.labThreads(lab.slug).catch(() => null),
      ]);
      for (const r of reports?.reports ?? []) {
        for (const idea of r.ideas ?? []) {
          const id = postRefId(idea);
          if (id) next.set(id, { lab: lab.slug, labName: lab.name, state: "adopted", ref: r.ref, event: r.event, at: r.at });
        }
      }
      for (const t of threads?.threads ?? []) {
        const id = postRefId(t.ref);
        if (id && !next.has(id)) next.set(id, { lab: lab.slug, labName: lab.name, state: "in_lab", messages: t.messages });
      }
    }
    index = next;
  } catch {
    index = new Map();
  } finally {
    busy = false;
    subscribers.forEach((f) => f());
  }
}

/** The idea index; empty on a relay without labs. Re-renders when it changes. */
export function useLabIdeas(): Map<string, IdeaPlace> {
  const [, bump] = useState(0);
  useEffect(() => {
    const f = () => bump((n) => n + 1);
    subscribers.add(f);
    if (subscribers.size === 1) {
      void refresh();
      timer = setInterval(() => void refresh(), EVERY_MS);
    }
    return () => {
      subscribers.delete(f);
      if (subscribers.size === 0 && timer) { clearInterval(timer); timer = null; }
    };
  }, []);
  return index;
}
