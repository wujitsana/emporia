// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { changeInHermes, groupItems, kindOf, READ_ONLY_NOTE, secretState, settingsCommand, type SettingItem } from "../settings";
import { DESKTOP_PLACE, HERMES_PLACE, hermesBasePath, hermesPlace } from "../hermesPlace";
import { setDesktopBridge } from "../desktop/bridge";
import { SettingRow, SettingsSections } from "./SettingsView";

const item = (group: string, label: string, over: Partial<SettingItem> = {}): SettingItem =>
  ({ group, label, value: "", about: "", command: "", level: "ok", person_only: false, ...over });

afterEach(() => {
  cleanup();
  setDesktopBridge(null);
});

describe("P178 settings", () => {
  it("keeps groups in the order the console sent them", () => {
    const groups = groupItems([item("Relay", "a"), item("Role", "b"), item("Relay", "c")]);
    expect(groups.map(([g, l]) => [g, l.map((i) => i.label)])).toEqual([["Relay", ["a", "c"]], ["Role", ["b"]]]);
  });

  it("says in one line that it is read-only and the agent can't change these", () => {
    expect(READ_ONLY_NOTE).toBe(
      "Read-only here. Change a setting in Hermes's settings page or with the command shown; the agent can't change these.");
  });

  it("points to the one command that changes an agent's settings, with its profile", () => {
    expect(settingsCommand("lambda")).toBe("emporia agent settings lambda");
  });

  it("draws a switch, the option chosen among its options, and a number with its unit — and marks the default", () => {
    render(<SettingsSections profile="lambda" items={[
      item("Sharing", "Declare its model", { kind: "switch", value: "on", current: "on", recommended: "on", is_default: true }),
      item("Who can reach the agent", "Wake mode", { kind: "choice", value: "direct", current: "direct",
        options: ["closed", "direct", "open"], recommended: "direct", is_default: true }),
      item("Who can reach the agent", "Messages per conversation", { kind: "number", value: "20 per hour", current: "20" }),
      item("Money & limits", "Largest single payment", { kind: "money", value: "$0.50 per payment", current: "50" }),
    ]} />);
    expect(screen.getByRole("img", { name: "Declare its model: on" })).toBeTruthy();
    expect(screen.getByText("direct")).toBeTruthy();
    expect(screen.getByText(/recommended/)).toBeTruthy();
    expect(screen.getByText(/other options: closed, open/)).toBeTruthy();
    expect(screen.queryAllByRole("listitem").length).toBe(0);
    expect(screen.queryAllByRole("button").length).toBe(0);
    expect(screen.getByText("20 per hour")).toBeTruthy();
    expect(screen.getByText("$0.50 per payment")).toBeTruthy();
    expect(screen.getAllByText("default").length).toBe(2);
    expect(screen.getAllByRole("heading").map((h) => h.textContent)).toEqual(
      ["Sharing", "Who can reach the agent", "Money & limits"]);
  });

  it("shows a secret only as set or not set, whatever the report holds", () => {
    const leaked = item("Keys & signer", "Signer token", { kind: "secret", value: "tok-123456", current: "tok-123456" });
    expect(secretState(leaked)).toBe("not set");
    expect(kindOf(item("USDC", "Operator key", { value: "set" }))).toBe("secret");
    const { container } = render(<SettingRow item={leaked} place={{ text: HERMES_PLACE }} />);
    expect(container.textContent).not.toContain("tok-123456");
    expect(container.textContent).toContain("not set");
  });

  it("links a setting Hermes edits to Hermes's page, scoped to the agent's profile", () => {
    const wake = item("Who can reach the agent", "Wake mode", { env: "EMPORIA_WAKE", command: "emporia wake open" });
    render(<SettingRow item={wake} place={hermesPlace("lambda", "hermes")} />);
    const link = screen.getByRole("link", { name: /Change in Hermes/ });
    expect(link.getAttribute("href")).toBe("/channels?profile=lambda");
    expect(screen.getByText("EMPORIA_WAKE")).toBeTruthy();
    expect(screen.getByText("emporia wake open")).toBeTruthy();
  });

  it("names the place in words where no link can be made, and opens Desktop's page there", () => {
    expect(hermesPlace("lambda", "console")).toEqual({ text: HERMES_PLACE });
    expect(hermesPlace(".hermes", "hermes").href).toBe("/channels");
    expect(hermesBasePath("hermes/")).toBe("/hermes");
    const went: string[] = [];
    setDesktopBridge({ rest: () => Promise.reject(new Error("no")), socket: () => () => undefined,
      navigate: (p) => went.push(p) });
    const place = hermesPlace("lambda", "desktop");
    expect(place.text).toBe(DESKTOP_PLACE);
    place.open?.();
    expect(went).toEqual(["/messaging"]);
  });

  it("keeps a person-only command, with its note, and no Hermes link", () => {
    const mainnet = item("Money & limits", "Mainnet (real money)", { kind: "switch", current: "off", value: "locked",
      env: "EMPORIA_X402_MAINNET", command: "emporia wallet mainnet lambda", person_only: true });
    expect(changeInHermes(mainnet)).toBe(false);
    render(<SettingRow item={mainnet} place={hermesPlace("lambda", "hermes")} />);
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/Only a person runs this/)).toBeTruthy();
    expect(screen.getByText("emporia wallet mainnet lambda")).toBeTruthy();
  });

  it("draws no control: the only buttons copy a command", () => {
    const { container } = render(<SettingsSections profile="lambda" items={[
      item("Sharing", "X verification post", { kind: "switch", current: "off", value: "off", env: "EMPORIA_X_LINK",
        command: "hermes -p lambda config set EMPORIA_X_LINK on" }),
    ]} />);
    expect(container.querySelectorAll("input, select, textarea").length).toBe(0);
    expect([...container.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["copy"]);
  });
});
