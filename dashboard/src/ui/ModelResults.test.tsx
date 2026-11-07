// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModelResults } from "./ModelResults";
import { wsSubprotocols } from "../transport";

const state = { data: null as unknown, error: null as string | null };
vi.mock("../hooks", () => ({ useInterval: () => [state.data, false, state.error] }));
vi.mock("../api", () => ({ api: { modelResults: vi.fn() } }));

afterEach(cleanup);

describe("ModelResults", () => {
  it("shows results by declared model, says they are declared, and counts clients", () => {
    state.data = {
      scope: "finished public games on this relay", declared: true,
      models: [{ model: "nous/hermes-4", games: 3, wins: 2, losses: 1, draws: 0 }],
      clients: { "hermes-plugin": 4, sdk: 1 },
    };
    state.error = null;
    render(<ModelResults />);
    expect(screen.getByText("nous/hermes-4")).toBeTruthy();
    expect(screen.getByText(/declared, not verified/)).toBeTruthy();
    expect(screen.getByText(/hermes-plugin 4 · sdk 1/)).toBeTruthy();
  });

  it("says when no game has revealed a model", () => {
    state.data = { scope: "x", declared: true, models: [], clients: {} };
    render(<ModelResults />);
    expect(screen.getByText(/No game has revealed a declared model yet/)).toBeTruthy();
  });

  it("says when the relay did not answer instead of showing an empty table", () => {
    state.data = null;
    state.error = "down";
    render(<ModelResults />);
    expect(screen.getByText(/unavailable/)).toBeTruthy();
  });
});

describe("wsSubprotocols", () => {
  it("carries the token in the handshake's subprotocols, never in a URL", () => {
    expect(wsSubprotocols("abc.def")).toEqual(["emporia.v1", "emporia.token.abc.def"]);
    expect(wsSubprotocols(null)).toBeUndefined();
  });
});
