/**
 * The relay's `/health` snapshot, polled once for the whole app.
 *
 * P43-2: four components polled `/health` on their own timers — `App` (5s),
 * `OverviewView` (5s), `PaymentsFeesSection` (10s) and `ProfileView` (30s) —
 * and `OverviewView` rendered `<PaymentsFeesSection seed={health} />`, so one
 * of them was handed the value it then re-fetched. Measured at 20 requests per
 * minute from the Overview subtree alone, before counting App's.
 *
 * `App` owns the single poll and provides it here; everything else reads it.
 * Deliberately its own context rather than a field on `RelayCtx`: that
 * provider re-renders every 30s, and moving a 5s value into it would re-render
 * the whole tree six times as often — trading a network cost for a render one.
 *
 * No provider component lives here, so this file exports no component and
 * stays clear of the Fast-Refresh rule that forced P42's `relayCtx` split:
 * `App` renders `<RelayHealthCtx.Provider>` directly.
 */
import { createContext, useContext } from "react";
import type { RelayHealth } from "./hooks";

export const RelayHealthCtx = createContext<RelayHealth | null>(null);

/** The current `/health` snapshot, or null before the first response. */
export function useRelayHealth(): RelayHealth | null {
  return useContext(RelayHealthCtx);
}
