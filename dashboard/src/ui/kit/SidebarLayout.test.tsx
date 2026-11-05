// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import SidebarLayout from "./SidebarLayout";
import { FlatRailItem } from "../FlatRailItem";

// On a phone a split view is list *then* detail: a rail row opens its detail on
// a screen of its own, a back control returns to the list, and a filter row
// narrows the list without leaving it. (The CSS decides what is visible at
// which width; the component decides which pane is current.)
afterEach(cleanup);

function view(initialPane?: "list" | "detail") {
  const r = render(
    <SidebarLayout
      initialPane={initialPane}
      sidebar={
        <div>
          <FlatRailItem keepList onClick={() => {}}>chess</FlatRailItem>
          <FlatRailItem onClick={() => {}}>alpha v beta</FlatRailItem>
        </div>
      }
    >
      <p>the board</p>
    </SidebarLayout>,
  );
  const root = () => r.container.firstElementChild as HTMLElement;
  return { root };
}

describe("SidebarLayout panes", () => {
  it("starts on the list", () => {
    const { root } = view();
    expect(root().dataset.paneShown).toBe("list");
  });

  it("opens the detail when a rail row is picked, and goes back to the list", () => {
    const { root } = view();
    fireEvent.click(screen.getByText("alpha v beta"));
    expect(root().dataset.paneShown).toBe("detail");
    fireEvent.click(screen.getByText(/back to list/));
    expect(root().dataset.paneShown).toBe("list");
  });

  it("stays on the list when a filter row is picked", () => {
    const { root } = view();
    fireEvent.click(screen.getByText("chess"));
    expect(root().dataset.paneShown).toBe("list");
  });

  it("can start on the detail (a deep link to one item)", () => {
    const { root } = view("detail");
    expect(root().dataset.paneShown).toBe("detail");
  });

  it("the back control is a real button, and changes only what is shown", () => {
    view("detail");
    const back = screen.getByText(/back to list/).closest("button");
    expect(back?.getAttribute("type")).toBe("button");
    expect(screen.getByText("the board")).toBeTruthy();
  });
});
