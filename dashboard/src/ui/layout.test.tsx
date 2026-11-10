// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { navById } from "../navConfig";
import { OverviewIntro } from "../views/OverviewView";
import { PageHeading, ViewBoundary } from "./layout";

afterEach(cleanup);

describe("PageHeading", () => {
  it("names the place and the section, and says what it shows", () => {
    render(<PageHeading item={navById("season")} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("Arena › Season");
    expect(screen.getByText(/season board/)).toBeTruthy();
  });

  it("names the viewed agent on the Agent's pages, and 'Agent' when there is none", () => {
    render(<PageHeading item={navById("settings")} agent="alpha" />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("alpha › Settings");
    cleanup();
    render(<PageHeading item={navById("settings")} agent={null} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("Agent › Settings");
    cleanup();
    render(<PageHeading item={navById("games")} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("Arena");
    expect(screen.getByRole("heading", { level: 1 }).textContent).not.toContain("›");
  });

  it("draws nothing for an unknown section", () => {
    const { container } = render(<PageHeading item={undefined} />);
    expect(container.textContent).toBe("");
  });
});

describe("ViewBoundary", () => {
  it("names the section that failed instead of blanking the dashboard", () => {
    const Boom = () => {
      throw new Error("bad payload");
    };
    const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
    render(<ViewBoundary name="Agents"><Boom /></ViewBoundary>);
    quiet.mockRestore();
    expect(screen.getByRole("alert").textContent).toContain("Agents could not be shown");
    expect(screen.getByRole("alert").textContent).toContain("bad payload");
  });

  it("renders its section when nothing fails", () => {
    render(<ViewBoundary name="Agents"><p>all fine</p></ViewBoundary>);
    expect(screen.getByText("all fine")).toBeTruthy();
  });
});

describe("OverviewIntro", () => {
  it("is one line: the dashboard only watches, the money is testnet, and a way to the Docs", () => {
    const go = vi.fn();
    render(<OverviewIntro go={go} />);
    const text = screen.getByText(/only watches/).textContent ?? "";
    expect(text).toMatch(/testnet/);
    // Escrow is opt-in: payments go agent to agent, and the relay holds nothing.
    expect(text).toMatch(/agent to agent; the relay\s+never holds the money/);
    expect(text).not.toMatch(/escrow|Hermes agents|marketplace/i);
    const links = screen.getAllByRole("button");
    expect(links).toHaveLength(1);
    fireEvent.click(links[0]);
    expect(go).toHaveBeenCalledWith("docs");
  });

  it("does not call a relay that reports real money a testnet, and promises nothing", () => {
    render(<OverviewIntro go={vi.fn()} realMoney />);
    const text = screen.getByText(/only watches/).textContent ?? "";
    expect(text).toMatch(/this relay moves real money\./);
    expect(text).not.toMatch(/testnet|test money|reward|prize/);
  });
});
