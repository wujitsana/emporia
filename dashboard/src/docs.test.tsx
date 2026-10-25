// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, fireEvent } from "@testing-library/react";

afterEach(cleanup);
import { renderMarkdown, slugify, type RenderOptions } from "./markdown";
import { DOCS, resolveDocLink, searchDocs } from "./docs";

const noLinks: RenderOptions = { resolveLink: () => ({ kind: "none" }), onDocLink: vi.fn(), onAnchor: vi.fn() };

function html(md: string, opts: RenderOptions = noLinks) {
  const { container } = render(<div>{renderMarkdown(md, opts)}</div>);
  return container;
}

describe("the markdown renderer (P171)", () => {
  it("renders what the docs use", () => {
    const c = html([
      "# Title", "", "Some **bold**, *italic* and `code`.", "",
      "- one", "- two", "  - nested", "", "1. first", "2. second", "",
      "| A | B |", "|---|---|", "| `x\\|y` | 2 |", "", "> a quote", "", "```bash", "emporia doctor", "```", "", "---",
    ].join("\n"));
    expect(c.querySelector("h2#title")?.textContent).toBe("Title");
    expect(c.querySelector("strong")?.textContent).toBe("bold");
    expect(c.querySelector("em")?.textContent).toBe("italic");
    expect(c.querySelector("ul li ul li")?.textContent).toBe("nested");
    expect(c.querySelectorAll("ol li")).toHaveLength(2);
    expect(c.querySelector("td code")?.textContent).toBe("x|y");
    expect(c.querySelector("blockquote")?.textContent).toContain("a quote");
    expect(c.querySelector("pre code")?.textContent).toBe("emporia doctor");
    expect(c.querySelector("hr")).not.toBeNull();
  });

  it("never turns text into HTML", () => {
    const c = html("<img src=x onerror=alert(1)> and <script>alert(1)</script>");
    expect(c.querySelector("img, script")).toBeNull();
    expect(c.textContent).toContain("<script>");
  });

  it("opens doc links in the view, web links in a new tab, and shows other paths as text", () => {
    const onDocLink = vi.fn();
    const c = html("[setup](docs/agent-security-setup.md#roles) [web](https://example.com) [code](src/x.py)", {
      resolveLink: (href) => href.startsWith("http") ? { kind: "web", href }
        : href.startsWith("docs/") ? { kind: "doc", id: "agent-setup", anchor: "roles" } : { kind: "none" },
      onDocLink, onAnchor: vi.fn(),
    });
    fireEvent.click(screen.getByText("setup"));
    expect(onDocLink).toHaveBeenCalledWith("agent-setup", "roles");
    const web = screen.getByText("web").closest("a")!;
    expect(web.getAttribute("target")).toBe("_blank");
    expect(web.getAttribute("rel")).toContain("noopener");
    expect(c.querySelector("a[href='src/x.py']")).toBeNull();
  });

  it("slugs headings the way links to them are written", () => {
    expect(slugify("1. Roles: what an agent may do")).toBe("1-roles-what-an-agent-may-do");
    expect(slugify("Where `keys` live")).toBe("where-keys-live");
  });
});

describe("the bundled docs (P171)", () => {
  it("carries each doc, with text", () => {
    for (const d of DOCS) expect(d.text.length, d.path).toBeGreaterThan(200);
    expect(DOCS[0].id).toBe("use-cases");
    // Links between the guides open inside the tab.
    const uses = DOCS.find((d) => d.id === "use-cases")!;
    expect(resolveDocLink(uses, "games.md")).toEqual({ kind: "doc", id: "games", anchor: undefined });
  });

  it("resolves links between bundled docs, whatever the relative path", () => {
    const setup = DOCS.find((d) => d.id === "agent-setup")!;
    expect(resolveDocLink(setup, "../ROADMAP.md")).toEqual({ kind: "doc", id: "roadmap", anchor: undefined });
    expect(resolveDocLink(setup, "payments.md#fee-model")).toEqual({ kind: "doc", id: "payments", anchor: "fee-model" });
    expect(resolveDocLink(setup, "#roles")).toEqual({ kind: "anchor", anchor: "roles" });
    expect(resolveDocLink(setup, "https://x.y")).toEqual({ kind: "web", href: "https://x.y" });
    expect(resolveDocLink(setup, "../src/emporia/cli.py").kind).toBe("none");
  });

  it("finds docs by what they say", () => {
    expect(searchDocs("bitwarden").map((d) => d.id)).toContain("agent-setup");
    expect(searchDocs("")).toHaveLength(DOCS.length);
    expect(searchDocs("zzzz-nothing")).toHaveLength(0);
  });

  it("renders every bundled doc without throwing", () => {
    for (const d of DOCS) {
      const opts: RenderOptions = { resolveLink: (h) => resolveDocLink(d, h), onDocLink: vi.fn(), onAnchor: vi.fn() };
      expect(() => html(d.text, opts)).not.toThrow();
    }
  });
});
