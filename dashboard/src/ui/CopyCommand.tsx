import { useState } from "react";
import Text from "@kit/Text";
import { gateNote, type OperatorCommand } from "../operatorCommands";

/**
 * A command to copy into a terminal — the dashboard's way to act: it shows the exact
 * command, a person runs it. The same look as a setting's command (Settings view).
 */
export function CopyCommand({ command, label }: { command: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(command).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }, () => undefined);
  };
  return (
    <span className="e-setting-cmd e-op-cmd">
      {label && <Text className="e-faint e-op-cmd__label">{label}</Text>}
      <code>{command}</code>
      <button type="button" className="e-setting-copy" onClick={copy} aria-label={`Copy: ${command}`}>
        {copied ? "copied" : "copy"}
      </button>
    </span>
  );
}

/** A thing's commands, each with what it is, and one line on who may run them. */
export function CommandList({ commands }: { commands: OperatorCommand[] }) {
  if (!commands.length) return null;
  const gates = [...new Set(commands.map((c) => c.gate))].filter((g) => g !== "read");
  return (
    <div className="e-op-cmds">
      {commands.map((c) => <CopyCommand key={c.command} command={c.command} label={c.label} />)}
      {gates.map((g) => <Text key={g} className="e-faint e-op-cmds__note">{gateNote(g)}</Text>)}
    </div>
  );
}
