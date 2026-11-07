/**
 * The questions "ask agent" types into the composer.
 *
 * They carry ids only — never a title, description or message another agent
 * wrote. Whatever the person sends goes to their agent as *their* words; relay
 * text pasted in here would be another agent's words with the person's
 * authority. The agent looks the item up with its own tools, where the relay's
 * text arrives as tool output, which it treats as data.
 */

const ID = /[^A-Za-z0-9_.:@-]/g;
const clean = (s: string | undefined | null) => (s ?? "").replace(ID, "").slice(0, 80);

export function gamePrompt(s: { session_id: string; module_type: string; status: string }): string {
  const kind = clean(s.module_type).replace("emporia:", "").replace(/:v\d$/, "");
  if (kind === "service") return `About deal ${clean(s.session_id)} (${clean(s.status)}): `;
  return `About ${kind} session ${clean(s.session_id)} (${clean(s.status)}): `;
}

export function roomPrompt(r: { room_id: string }): string {
  return `About room ${clean(r.room_id)}: `;
}

/** Home's "Next up": the whole list, or one item by its tool and the session it is about. */
export function nextPrompt(it?: { tool: string; kind: string; session_id?: string }): string {
  if (!it) return "Run whats_next and tell me what you would do first: ";
  const on = it.session_id ? ` in session ${clean(it.session_id)}` : "";
  return `whats_next lists a ${clean(it.kind)} for you (${clean(it.tool)}${on}). Look it up and tell me what you would do: `;
}
