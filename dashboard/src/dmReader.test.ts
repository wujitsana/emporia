import { describe, expect, it } from "vitest";
import { readerConfigFromUrl } from "./dmReader";

describe("readerConfigFromUrl (P125)", () => {
  it("takes the token and port from the reader's link", () => {
    expect(readerConfigFromUrl("?reader=abcdefghijklmnop1234&reader_port=8090"))
      .toEqual({ token: "abcdefghijklmnop1234", port: 8090 });
    expect(readerConfigFromUrl("?reader=abcdefghijklmnop1234")?.port).toBe(8089);
  });
  it("ignores anything that is not a reader token", () => {
    expect(readerConfigFromUrl("")).toBeNull();
    expect(readerConfigFromUrl("?reader=short")).toBeNull();
    expect(readerConfigFromUrl("?reader=<script>alert(1)</script>xxxxx")).toBeNull();
  });
});
