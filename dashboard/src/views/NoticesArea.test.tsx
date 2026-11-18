// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NoticesArea } from "./NoticesArea";

const h = vi.hoisted(() => ({ inHermes: true, body: { notices: [] as unknown[] }, status: 200, asked: [] as string[] }));

vi.mock("../transport", async (orig) => {
  const real = await orig<typeof import("../transport")>();
  return {
    ...real,
    get IN_HERMES() { return h.inHermes; },
    consoleUrl: (p: string) => `/api/plugins/emporia${p}`,
    httpFetch: vi.fn(async (url: string) => {
      h.asked.push(url);
      return new Response(JSON.stringify(h.body), { status: h.status });
    }),
  };
});

beforeEach(() => { h.inHermes = true; h.status = 200; h.asked = []; h.body = { notices: [] }; });
afterEach(cleanup);

describe("Home: notices area", () => {
  it("shows the plugin's notices as text, a command set apart, and asks only for them (read-only)", async () => {
    h.body = { notices: [
      { id: "runtime", text: "Emporia's plugin moved to 2.1.0 (aaaaaaaaaaaa), its runtime did not. Run `hermes emporia update`." },
      { id: "moves", text: "3 game moves waited today because the daily turn cap was reached." },
      { id: "replies", text: "A reply or message has waited over 30 minutes in the inbox and no round is scheduled." },
    ] };
    render(<NoticesArea />);
    expect(await screen.findByRole("heading", { name: "Notices" })).toBeTruthy();
    expect(document.querySelectorAll("li")).toHaveLength(3);
    expect(document.querySelector("[data-notice=runtime] code")?.textContent).toBe("hermes emporia update");
    expect(screen.getByText(/3 game moves waited today/)).toBeTruthy();
    expect(h.asked).toEqual(["/api/plugins/emporia/_notices"]);
    expect(document.querySelector("button, a, input, form")).toBeNull();      // nothing to click: it only shows
  });

  it("draws nothing when there is nothing to say, outside Hermes, or when the read fails", async () => {
    const { container, rerender } = render(<NoticesArea />);
    await waitFor(() => expect(h.asked).toHaveLength(1));
    expect(container.textContent).toBe("");
    h.inHermes = false;
    rerender(<NoticesArea key="outside" />);
    expect(h.asked).toHaveLength(1);                                          // no second request outside Hermes
    h.inHermes = true;
    h.status = 503;
    h.body = { notices: [{ id: "x", text: "ignored" }] };
    rerender(<NoticesArea key="failing" />);
    await waitFor(() => expect(h.asked).toHaveLength(2));
    expect(container.textContent).toBe("");
  });

  it("ignores rows that are not {id, text}", async () => {
    h.body = { notices: [{ id: "a", text: "fine" }, { id: 3, text: "no" }, null, { text: "no id" }] };
    render(<NoticesArea />);
    await screen.findByText("fine");
    expect(document.querySelectorAll("li")).toHaveLength(1);
  });
});
