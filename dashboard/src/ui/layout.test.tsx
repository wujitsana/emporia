// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { navById } from "../navConfig";
import { OverviewIntro } from "../views/OverviewView";
import { PageHeading, ViewBoundary } from "./layout";

afterEach(cleanup);

describe("PageHeading", () => {
  it("names the section and says what it shows", () => {
    render(<PageHeading item={navById("season")} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("Season");
    expect(screen.getByText(/improving Emporia/)).toBeTruthy();
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
  it("says the dashboard only watches, that the money is testnet, and points to the games first", () => {
    const go = vi.fn();
    render(<OverviewIntro go={go} />);
    const text = screen.getByText(/only watches/).textContent ?? "";
    expect(text).toMatch(/testnet/);
    const links = screen.getAllByRole("button");
    expect(links.map((l) => l.textContent)).toEqual([
      expect.stringContaining("Games"), expect.stringContaining("Season"), expect.stringContaining("Listings"),
    ]);
    fireEvent.click(links[0]);
    expect(go).toHaveBeenCalledWith("games");
  });
});
