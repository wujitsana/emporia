// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AgentMoireAvatar } from "./AgentMoireAvatar";
import { AgentNavCtx } from "./agentNavCtx";
import { AgentName } from "./ui/AgentName";

afterEach(cleanup);

describe("every agent glyph and name opens its page", () => {
  it("links through the one context, without opening the row it sits in", () => {
    const navigate = vi.fn();
    const row = vi.fn();
    render(
      <AgentNavCtx.Provider value={navigate}>
        <div onClick={row}>
          <AgentMoireAvatar agentId="beta" />
          <AgentName id="gamma">gamma</AgentName>
        </div>
      </AgentNavCtx.Provider>,
    );
    fireEvent.click(screen.getByRole("link", { name: "Open beta's page" }));
    fireEvent.click(screen.getByText("gamma"));
    expect(navigate).toHaveBeenNthCalledWith(1, "agents", { agentId: "beta" });
    expect(navigate).toHaveBeenNthCalledWith(2, "agents", { agentId: "gamma" });
    expect(row).not.toHaveBeenCalled();
  });
  it("is plain without the context, and on the page's own glyph", () => {
    render(<><AgentMoireAvatar agentId="beta" /><AgentName id="gamma">gamma</AgentName></>);
    expect(screen.queryByRole("link")).toBeNull();
    cleanup();
    render(<AgentNavCtx.Provider value={vi.fn()}><AgentMoireAvatar agentId="beta" link={false} /></AgentNavCtx.Provider>);
    expect(screen.queryByRole("link")).toBeNull();
  });
});
