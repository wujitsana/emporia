import Text from "@components/Text";
import type { NavOpts } from "../eventNav";
import { McpCommandHint } from "./McpCommandHint";
import { VIEWER } from "../relayEnv";

export function ListingPeekBanner({ peek }: { peek: NonNullable<NavOpts["listingPeek"]> }) {
  const mod = peek.moduleType?.replace("emporia:", "").replace(/:v\d$/, "") ?? "listing";
  return (
    <div className="e-listing-peek">
      <Text className="e-listing-peek__title">{peek.title}</Text>
      {peek.description ? (
        <Text className="e-dim e-listing-peek__desc">{peek.description.slice(0, 240)}</Text>
      ) : null}
      <Text className="e-faint e-listing-peek__meta">
        {mod} · agent …{peek.agentId.slice(-10)}
      </Text>
      {peek.moduleType && (
        <McpCommandHint
          label="Start this kind of session via MCP"
          tool="create_session"
          args={{
            module_type: peek.moduleType,
            agent_id: VIEWER && VIEWER !== "dashboard" ? VIEWER : "<your_agent_id>",
          }}
        />
      )}
    </div>
  );
}