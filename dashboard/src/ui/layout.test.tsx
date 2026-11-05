// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { navById } from "../navConfig";
import { OverviewIntro } from "../views/OverviewView";
import { PageHeading, ViewBoundary } from "./layout";

afterEach(cleanup);

describe("PageHeading", () => {
  it("names the section and says what it shows", () => {
    render(<PageHeading item={navById("gamify")} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain("Gamify");
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
  it("says the dashboard only watches, and points to the season first", () => {
    const go = vi.fn();
    render(<OverviewIntro go={go} />);
    expect(screen.getByText(/only watches/)).toBeTruthy();
    const links = screen.getAllByRole("button");
    expect(links[0].textContent).toContain("Gamify");
    fireEvent.click(links[0]);
    expect(go).toHaveBeenCalledWith("gamify");
  });
});
