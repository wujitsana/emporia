/** Cut `s` to at most `max` characters at the last word boundary, closed with an ellipsis (a word longer
 * than `max` is cut at `max`). Mirrors the relay's `emporia.textcut.cut_at_word`. */
export function cutAtWord(s: string, max: number): string {
  if (max < 1 || s.length <= max) return s;
  const space = s.lastIndexOf(" ", max);
  const head = space > 0 ? s.slice(0, space).trimEnd() : "";
  return `${head || s.slice(0, max)}…`;
}
