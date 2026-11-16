// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RelaysView } from "./RelaysView";

const fetchMock = vi.fn();
const reply = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } }));
const card = { relay_id: "rl_b", relay_url: "http://b.test", host: "b.test", agents: 7, access: "open", visitors: "welcome",
  shared_topics: [{ slug: "town", title: "Town" }], links: ["rl_a"] };

beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("relays view", () => {
  it("lists the directory with self-reported counts as 'says', and only reads", async () => {
    fetchMock.mockImplementation((url: string) => String(url).includes("/directory/v1/relays")
      ? reply({ origin_relay: "http://a.test", relays: [card] })
      : reply({ relay_url: "x", standalone: false, peers: [], shared_topics: { shares: ["town"], carried: [] } }));
    render(<RelaysView />);
    await waitFor(() => expect(screen.getByText(/says 7 agents/)).toBeTruthy());
    expect(screen.getByText(/shares town@b.test/)).toBeTruthy();
    expect(screen.getByText("shared by this relay")).toBeTruthy();
    for (const call of fetchMock.mock.calls) expect((call[1]?.method ?? "GET").toUpperCase()).toBe("GET");
  });
  it("says so when the relay serves no directory", async () => {
    fetchMock.mockImplementation((url: string) => String(url).includes("/directory/")
      ? reply({ detail: "not found" }, 404) : reply({ relay_url: "x", standalone: true, peers: [] }));
    render(<RelaysView />);
    await waitFor(() => expect(screen.getByText(/does not serve a directory/)).toBeTruthy());
  });
});
