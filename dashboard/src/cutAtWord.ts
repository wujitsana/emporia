/** Cut `s` to at most `max` characters, ellipsis included, at the last word boundary (a word longer
 * than that is cut to fit). Mirrors the relay's `emporia.textcut.cut_at_word`. */
export function cutAtWord(s: string, max: number): string {
  if (max < 1 || s.length <= max) return s;
  const room = max - 1; // the ellipsis takes one
  const space = s.lastIndexOf(" ", room);
  const head = space > 0 ? s.slice(0, space).trimEnd() : "";
  return `${head || s.slice(0, room)}…`;
}
