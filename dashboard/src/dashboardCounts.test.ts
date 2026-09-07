import { describe, expect, it } from "vitest";
import {
  countsFromSnapshot,
  failedFromSnapshot,
  sectionsFromSnapshot,
  type OverviewSnapshot,
} from "./dashboardCounts";

// P43-2: this module drives every nav metric and the whole Overview hub, and
// had no test — because fetching and deriving were fused, there was nothing to
// call without mocking the network. Splitting them for the request fix is what
// made these reachable.

const ok = <T,>(value: T): PromiseSettledResult<T> => ({ status: "fulfilled", value });
const failed = <T,>(): PromiseSettledResult<T> => ({
  status: "rejected",
  reason: new Error("relay down"),
});

function snapshot(over: Partial<OverviewSnapshot> = {}): OverviewSnapshot {
  return {
    listings: ok({ listings: [], count: 0, has_more: false }),
    sessions: ok({ sessions: [], count: 0, has_more: false }),
    rooms: ok({ rooms: [], count: 0, has_more: false }),
    events: ok({ events: [], count: 0, has_more: false }),
    agents: ok({ agents: [], count: 0, has_more: false }),
    settlements: ok({ settlements: [], count: 0, has_more: false }),
    agoraTopics: ok({ topics: [], count: 0, has_more: false }),
    dmThreads: ok([]),
    ...over,
  } as OverviewSnapshot;
}

describe("countsFromSnapshot", () => {
  it("renders 'N+' when the relay says there are more rows past its page cap", () => {
    // N6 review R2: `count` is always the returned page size, so it can never
    // signal truncation on its own. A flat number here silently under-reports.
    const counts = countsFromSnapshot(
      snapshot({ agents: ok({ agents: [], count: 50, has_more: true }) })
    );
    expect(counts.agents).toBe("50+");
  });

  it("distinguishes a failed request from an empty one", () => {
    // null, not 0 — a relay that is down must not read as a relay with nothing
    // on it. This is the whole reason the snapshot holds settled results.
    const counts = countsFromSnapshot(snapshot({ rooms: failed() }));
    expect(counts.rooms).toBeNull();
    expect(counts.listings).toBe(0);
  });

  it("counts only emporia: modules as game sessions", () => {
    const counts = countsFromSnapshot(
      snapshot({
        sessions: ok({
          sessions: [
            { module_type: "emporia:chess", created_at: "2026-01-01T00:00:00Z" },
            { module_type: "emporia:go", created_at: "2026-01-02T00:00:00Z" },
            { module_type: "service", created_at: "2026-01-03T00:00:00Z" },
          ],
          count: 3,
          has_more: false,
        }),
      } as unknown as Partial<OverviewSnapshot>)
    );
    expect(counts.gameSessions).toBe(2);
    expect(counts.sessions).toBe(3);
  });
});

describe("sectionsFromSnapshot", () => {
  it("says so in words when a collection is empty", () => {
    const byView = new Map(sectionsFromSnapshot(snapshot()).map((r) => [r.view, r]));
    expect(byView.get("listings")?.lines).toEqual(["No open listings"]);
    expect(byView.get("messages")?.lines).toEqual(["No DM threads"]);
  });

  it("caps each card at three lines regardless of how much came back", () => {
    const rows = sectionsFromSnapshot(
      snapshot({
        listings: ok({
          listings: Array.from({ length: 9 }, (_, i) => ({
            title: `listing ${i}`,
            listing_type: "service",
            payment_mode: "free",
          })),
          count: 9,
          has_more: false,
        }),
      } as unknown as Partial<OverviewSnapshot>)
    );
    expect(rows.find((r) => r.view === "listings")?.lines).toHaveLength(3);
  });

  it("renders a failed leg as the empty state rather than throwing", () => {
    const rows = sectionsFromSnapshot(snapshot({ agents: failed() }));
    expect(rows.find((r) => r.view === "agents")?.lines).toEqual(["No registered agents"]);
  });
});

describe("failedFromSnapshot — rejected is not empty (P69)", () => {
  const ok = <T,>(value: T): PromiseSettledResult<T> => ({ status: "fulfilled", value });
  const bad = (why: string): PromiseSettledResult<never> =>
    ({ status: "rejected", reason: new Error(why) });

  const allOk = () => ({
    listings: ok([]),
    sessions: ok({ sessions: [] }),
    rooms: ok({ rooms: [] }),
    events: ok({ events: [] }),
    agents: ok({ agents: [] }),
    settlements: ok({ settlements: [] }),
    agoraTopics: ok({ topics: [] }),
    dmThreads: ok([]),
  }) as unknown as Parameters<typeof failedFromSnapshot>[0];

  it("names nothing when every collection answered, however empty", () => {
    // The distinction the whole change rests on: eight empty collections are
    // not eight failures, and used to render identically to them.
    expect(failedFromSnapshot(allOk()).size).toBe(0);
  });

  it("names only the collection that rejected", () => {
    const snap = { ...allOk(), rooms: bad("500 from /rooms") } as Parameters<
      typeof failedFromSnapshot
    >[0];
    const failed = failedFromSnapshot(snap);
    expect([...failed]).toEqual(["rooms"]);
  });

  it("names each of several independent failures", () => {
    const snap = {
      ...allOk(),
      agents: bad("timeout"),
      settlements: bad("403"),
    } as Parameters<typeof failedFromSnapshot>[0];
    expect([...failedFromSnapshot(snap)].sort()).toEqual(["agents", "fees"]);
  });

  it("counts a rejected collection as null, which is why it needed naming", () => {
    // countsFromSnapshot cannot tell these apart — that is the bug, and this
    // pins the reason failedFromSnapshot has to exist alongside it.
    const rejected = countsFromSnapshot({
      ...allOk(), rooms: bad("500"),
    } as Parameters<typeof countsFromSnapshot>[0]);
    const empty = countsFromSnapshot(allOk() as Parameters<typeof countsFromSnapshot>[0]);
    expect(rejected.rooms).toBeNull();
    expect(empty.rooms).toBe(0);
  });
});
