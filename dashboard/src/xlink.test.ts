import { describe, expect, it } from "vitest";
import { aboutBook, accountUrl, bioLine, cleanHandle, composerUrl, linkStatus, postOnX, proofUrl } from "./xlink";

describe("an X link on a card", () => {
  it("accepts only X handles, lowercased, without the @", () => {
    expect(cleanHandle("@Kestrel_X")).toBe("kestrel_x");
    for (const bad of ["", "has space", "waytoolonghandle_123", "https://x.com/me", "a.b", "ünï", "@@x"]) {
      expect(cleanHandle(bad)).toBe("");
    }
  });

  it("builds every link from the handle and the post id, never from a stored URL", () => {
    expect(accountUrl({ handle: "kestrel_x" })).toBe("https://x.com/kestrel_x");
    expect(proofUrl({ handle: "kestrel_x", post_id: "1850000000000000001" })).toBe("https://x.com/kestrel_x/status/1850000000000000001");
    expect(proofUrl({ handle: "kestrel_x", post_id: "bio" })).toBe("https://x.com/kestrel_x");
    // a post id that is not digits (a hostile card) never becomes part of a path
    expect(proofUrl({ handle: "kestrel_x", post_id: "../evil?x=1" })).toBe("https://x.com/kestrel_x");
  });

  it("says claimed until another operator confirms, then how many", () => {
    expect(linkStatus({ status: "claimed", confirmed_by: 0 })).toBe("claimed");
    expect(linkStatus({ status: "confirmed", confirmed_by: 1 })).toBe("confirmed by 1 other operator's agent");
    expect(linkStatus({ status: "confirmed", confirmed_by: 3 })).toBe("confirmed by 3 other operators' agents");
    expect(linkStatus({ status: "confirmed", confirmed_by: 0 })).toBe("claimed");      // never more than the record says
  });

  it("opens X's own composer with the console's text, encoded", () => {
    const text = "> emporia ~ a entered\nsigned ▮\nemporia·a@h·TOKEN";
    expect(composerUrl(text)).toBe(`https://x.com/intent/post?text=${encodeURIComponent(text)}`);
    expect(composerUrl(text)).not.toContain("\n");
    const out = postOnX({ handle: "a", claim_line: "l", style: "plain", post_text: text, post_length: 40, post_limit: 280,
      bio_text: "l", intent_url: "https://evil.example/" });
    expect(out.url.startsWith("https://x.com/intent/post?text=")).toBe(true);           // never the server's URL
  });

  it("shows a bio as one plain line, cut where a list is tight", () => {
    expect(bioLine({ about: "  plays   blotto\n  well " })).toBe("plays blotto well");
    expect(bioLine({ about: "x".repeat(100) }, 20)).toHaveLength(20);
    expect(bioLine({})).toBe("");
    expect(aboutBook([{ agent_id: "a", about: "hi" }, { agent_id: "b" }])).toEqual({ a: "hi" });
    expect(aboutBook([{ agent_id: "b" }])).toBeUndefined();
  });
});
