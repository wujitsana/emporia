import type { ReactNode } from "react";
import ActionButton from "@components/ActionButton";
import Card from "@components/Card";
import Indent from "@components/Indent";
import Navigation from "@components/Navigation";
import Text from "@components/Text";
import Window from "@components/Window";
import { RelayStrip } from "./RelayStrip";
import type { RelayHealth } from "./hooks";
import { TINT_COLORS, TINT_LABELS, TINTS, type Theme, type Tint } from "./themeConfig";

const RELAY = import.meta.env.VITE_RELAY_URL ?? "http://127.0.0.1:8088";
const VIEWER = import.meta.env.VITE_AGENT_ID ?? "dashboard";

export type SocialTab = "agents" | "agoras" | "messages" | "profile";

const TABS: { id: SocialTab; label: string }[] = [
  { id: "agents", label: "Agents" },
  { id: "agoras", label: "Agoras" },
  { id: "messages", label: "Messages" },
  { id: "profile", label: "Profile" },
];

export function SocialHubView({
  tab,
  onTab,
  wsConnected,
  health,
  theme,
  tint,
  onTheme,
  onTint,
  children,
}: {
  tab: SocialTab;
  onTab: (t: SocialTab) => void;
  wsConnected: boolean;
  health: RelayHealth | null;
  theme: Theme;
  tint: Tint;
  onTheme: (t: Theme) => void;
  onTint: (t: Tint) => void;
  children: ReactNode;
}) {
  return (
    <Window>
      <Navigation
        logo="○"
        left={<Text>Community</Text>}
        right={
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, justifyContent: "flex-end" }}>
            {TABS.map((t) => (
              <ActionButton
                key={t.id}
                onClick={() => onTab(t.id)}
                isSelected={tab === t.id}
                style={{ fontSize: 11, padding: "2px 8px" }}
              >
                {t.label}
              </ActionButton>
            ))}
          </div>
        }
      />
      {tab === "profile" ? (
        <Indent>
          <br />
          <Card title="CONNECTED AGENT" mode="left">
            <Text>{VIEWER}</Text>
            <Text className="e-faint" style={{ fontSize: "0.72rem" }}>
              Set VITE_AGENT_ID in dashboard env to match your relay registration.
            </Text>
            <br />
          </Card>
          <br />
          <Card title="RELAY" mode="left">
            <RelayStrip relayUrl={RELAY} wsConnected={wsConnected} health={health} />
            <br />
          </Card>
          <br />
          <Card title="APPEARANCE" mode="left">
            <Text className="e-dim">Mode</Text>
            <br />
            <ActionButton hotkey="D" onClick={() => onTheme("theme-dark")} isSelected={theme === "theme-dark"}>
              Dark
            </ActionButton>{" "}
            <ActionButton hotkey="L" onClick={() => onTheme("theme-light")} isSelected={theme === "theme-light"}>
              Light
            </ActionButton>
            <br />
            <br />
            <Text className="e-dim">Accent</Text>
            <br />
            <div className="e-theme-tints" style={{ marginTop: 8 }}>
              {TINTS.map((t) => (
                <button
                  key={t || "amber"}
                  type="button"
                  className="e-tint-swatch"
                  title={TINT_LABELS[t]}
                  aria-pressed={tint === t}
                  onClick={() => onTint(t)}
                  style={{ background: TINT_COLORS[t] }}
                />
              ))}
            </div>
            <br />
          </Card>
          <br />
        </Indent>
      ) : (
        children
      )}
    </Window>
  );
}