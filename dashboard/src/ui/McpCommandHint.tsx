import { useState } from "react";
import Text from "@components/Text";

/**
 * Shows the exact MCP tool call an agent would run to take a write action
 * (join a session, challenge another agent, ...). The dashboard itself never
 * performs writes — this is the bridge from "look" to "do" without building
 * a parallel write UI.
 */
export function McpCommandHint({ label, tool, args }: { label: string; tool: string; args: Record<string, string> }) {
  const [copied, setCopied] = useState(false);
  const argStr = Object.entries(args)
    .map(([k, v]) => `${k}="${v}"`)
    .join(", ");
  const command = `${tool}(${argStr})`;

  const copy = () => {
    navigator.clipboard?.writeText(command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="e-mcp-hint">
      <Text className="e-faint e-mcp-hint__label">{label}</Text>
      <code className="e-mcp-hint__code" onClick={copy} title="Click to copy">
        {command}
      </code>
      {copied && <Text className="e-mcp-hint__copied">copied</Text>}
    </div>
  );
}
