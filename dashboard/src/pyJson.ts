/**
 * Python's canonical signing JSON, byte for byte (moved here from
 * integrityCheck.ts so keyLog.ts can use it too).
 */

/**
 * Python's `sign()`/`verify()` (identity.py) serialize the signed payload as
 * `json.dumps(payload, sort_keys=True, separators=(",", ":"))` -- compact,
 * alphabetically-sorted keys, and critically `ensure_ascii=True` (the
 * `json.dumps` default): every non-ASCII character is escaped as `\uXXXX`
 * rather than emitted as a literal UTF-8 byte sequence. `JSON.stringify`
 * does the opposite (literal UTF-8, no non-ASCII escaping), so a message
 * containing anything outside printable ASCII (emoji, accented letters, any
 * non-Latin script) would serialize differently in JS than in Python and
 * verification would falsely fail on an honestly-signed message. This
 * escapes exactly the way Python's ASCII-only JSON encoder does, including
 * surrogate-pair handling for astral characters -- which falls out for free
 * here since a JS string already *is* a sequence of UTF-16 code units, the
 * same representation Python computes by hand for `\uXXXX` escaping.
 */
function pyJsonEscapeString(s: string): string {
  let out = "";
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    const code = s.charCodeAt(i);
    if (ch === '"') out += '\\"';
    else if (ch === "\\") out += "\\\\";
    else if (code === 0x08) out += "\\b";
    else if (code === 0x0c) out += "\\f";
    else if (code === 0x0a) out += "\\n";
    else if (code === 0x0d) out += "\\r";
    else if (code === 0x09) out += "\\t";
    else if (code < 0x20 || code > 0x7e) out += "\\u" + code.toString(16).padStart(4, "0");
    else out += ch;
  }
  return out;
}

export type JsonValue = string | number | boolean | null | JsonValue[] | { [k: string]: JsonValue };

/** General `json.dumps(value, sort_keys=True, separators=(",", ":"))` port --
 * covers every payload shape this file needs to reconstruct:
 * `signed_room_message_payload()`/`signed_dm_thread_payload()` (flat
 * string dicts) and `federation_signable_bytes()`'s payload (a string field
 * plus a list of dicts containing strings and ints, used by the room-chain
 * signature below). Recurses so key order is sorted at every nesting level,
 * matching Python's `sort_keys=True` applying uniformly, not just at the top. */
export function pyCanonicalJson(value: JsonValue): string {
  if (value === null) return "null";
  if (typeof value === "string") return `"${pyJsonEscapeString(value)}"`;
  if (typeof value === "number") return String(value);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (Array.isArray(value)) return `[${value.map(pyCanonicalJson).join(",")}]`;
  const keys = Object.keys(value).sort();
  return `{${keys.map((k) => `"${pyJsonEscapeString(k)}":${pyCanonicalJson(value[k])}`).join(",")}}`;
}
