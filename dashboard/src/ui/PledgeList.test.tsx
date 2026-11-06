// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { counterparty, evidenceLine, mergePledges, whenLine, type Pledge } from "../pledgeFormat";
import { PledgeList } from "./PledgeList";

// The sheet's pledges, under its Word line (GET /pledges): kind, who with (or "a private deal"),
// the status the relay computed with its evidence, and when it is due. Read-only.
const state: { data: unknown; error: string | null } = { data: null, error: null };
vi.mock("../hooks", () => ({ useInterval: () => [state.data, false, state.error] }));
vi.mock("../api", () => ({ api: { pledges: vi.fn() } }));

afterEach(() => {
  cleanup();
  state.data = null;
  state.error = null;
});

const kept: Pledge = {
  pledge_id: "pl_1", maker_id: "alpha", to_agent: "bob", kind: "deliver_by", public: true, made_at: "2026-11-05T10:00:00Z",
  status: "kept", resolved_by: "evidence", resolved_at: "2026-11-05T11:00:00Z",
  evidence: { delivered_at: "2026-11-05T11:00:00Z", by: "2026-11-05T12:00:00Z" },
};
const privateBroken: Pledge = {
  pledge_id: "pl_2", maker_id: "alpha", to_agent: "carol", kind: "respond_within", public: false, made_at: "2026-11-05T09:00:00Z",
  status: "broken", resolved_by: "evidence", resolved_at: "2026-11-05T09:30:00Z",
  evidence: { delivered_at: "2026-11-05T09:00:00Z", answered_at: null, within_s: 600 },
};
const openToMe: Pledge = {
  pledge_id: "pl_3", maker_id: "dave", to_agent: "alpha", kind: "reveal_within", public: true, made_at: "2026-11-05T12:00:00Z",
  status: "open", due_at: "2026-11-05T13:00:00Z", waiting_on: "maker",
};
const disputed: Pledge = {
  pledge_id: "pl_4", maker_id: "alpha", to_agent: "erin", kind: "refund", public: true, made_at: "2026-11-04T12:00:00Z",
  status: "disputed", resolved_by: "attestation", resolved_at: "2026-11-05T08:00:00Z",
  evidence: { attested: "broken", contested_at: "2026-11-05T08:00:00Z" },
};
const voided: Pledge = {
  pledge_id: "pl_5", maker_id: "alpha", to_agent: "bob", kind: "tell", public: true, made_at: "2026-11-03T12:00:00Z",
  status: "void", resolved_at: "2026-11-03T13:00:00Z", evidence: { why: "no move committed after the pledge" },
};

describe("pledge wording", () => {
  it("names the counterparty, or 'a private deal'", () => {
    expect(counterparty(kept, "alpha")).toBe("to bob");
    expect(counterparty(openToMe, "alpha")).toBe("from dave");
    expect(counterparty(privateBroken, "alpha")).toBe("a private deal");
  });

  it("puts the evidence in a line", () => {
    expect(evidenceLine(kept)).toMatch(/^delivered .* · due by /);
    expect(evidenceLine(privateBroken)).toMatch(/no answer · within 10 min/);
    expect(evidenceLine(disputed)).toMatch(/attested broken by the counterparty · contested/);
    expect(evidenceLine(voided)).toBe("no move committed after the pledge");
    expect(evidenceLine(openToMe)).toBe("");
  });

  it("says when an open one is due", () => {
    expect(whenLine(openToMe)).toMatch(/^due /);
    expect(whenLine({ ...openToMe, due_at: null })).toBe("waiting on the maker");
  });

  it("merges made and received, once each, newest first", () => {
    expect(mergePledges([kept, privateBroken], [openToMe, kept]).map((p) => p.pledge_id)).toEqual(["pl_3", "pl_1", "pl_2"]);
  });
});

describe("the pledges list", () => {
  it("lists kind, counterparty, status with evidence, and due time for every status", () => {
    state.data = [{ pledges: [kept, privateBroken, disputed, voided] }, { pledges: [openToMe] }];
    render(<PledgeList agentId="alpha" />);
    expect(screen.getByRole("table", { name: "Pledges of alpha" })).toBeTruthy();
    for (const s of ["kept", "broken", "disputed", "void", "open"]) expect(screen.getByText(s)).toBeTruthy();
    expect(screen.getByText("a private deal")).toBeTruthy();
    expect(screen.queryByText(/carol/)).toBeNull();
    expect(screen.getByText("from dave")).toBeTruthy();
    expect(screen.getByText("no move committed after the pledge")).toBeTruthy();
  });

  it("says when there are none", () => {
    state.data = [{ pledges: [] }, { pledges: [] }];
    render(<PledgeList agentId="alpha" />);
    expect(screen.getByText(/No pledges yet/)).toBeTruthy();
  });

  it("shows nothing on a relay without pledges (404), and says a failed read failed", () => {
    state.error = "Error: 404 /pledges";
    expect(render(<PledgeList agentId="alpha" />).container.innerHTML).toBe("");
    cleanup();
    state.error = "Error: 500 /pledges";
    render(<PledgeList agentId="alpha" />);
    expect(screen.getByText(/did not load — not an empty list/)).toBeTruthy();
  });
});
