// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RelayView } from "./RelayView";
import type { OperatorOverview } from "../settings";

const day = (d: string, fees: number) => ({ day: d, agents: 1, sessions: 2, messages: 3, payments_cents: 500, fees_cents: fees });
const overview: OperatorOverview = {
  settings: [],
  stats: {
    agents: { registered: 7, nous_verified: 3, with_wallet: 2, new_7d: 1, active_24h: 2, active_7d: 5 },
    sessions: { by_status: { complete: 4 }, created_7d: 6, by_kind: [{ module_type: "emporia:blotto:v1", total: 4, last_7d: 2 }] },
    community: { rooms: 2, paid_rooms: 1, encrypted_rooms: 1, room_messages_7d: 10, dm_threads: 1,
      dm_messages_7d: 4, agora_topics: 1, agora_posts_7d: 0, open_listings: 3 },
    money: { payments: [{ rail: "x402", count: 2, cents: 150, cents_30d: 150 }],
      fees: [{ rail: "x402", settlements: 1, volume_cents: 200, fee_cents: 5, fee_cents_30d: 5 }],
      escrow_holds: [], usdc_direct: [] },
    daily: [day("2026-10-25", 0), day("2026-10-26", 5)],
    generated_at: "2026-10-26T12:00:00+00:00",
  },
};

vi.mock("../api", () => ({ api: { operatorOverview: vi.fn(() => Promise.resolve(overview)) } }));

afterEach(cleanup);

describe("P179 relay view", () => {
  it("shows totals, a bar per day with its value, and the money tables", async () => {
    render(<RelayView />);
    expect(await screen.findByText("Active this week")).toBeTruthy();
    expect(screen.getByText("14")).toBeTruthy();   // messages this week: rooms + direct
    expect(screen.getByText("2026-10-26: $0.05")).toBeTruthy();   // the fee bar's tooltip
    expect(screen.getByText("emporia:blotto:v1")).toBeTruthy();
    expect(screen.getAllByText("$0.05").length).toBeGreaterThan(0);
  });
});
