/** The select is as wide as the name it shows (up to 20 characters, then an ellipsis), not as the longest
 *  agent in its list: the header gives room for the one name, and the name is never cut short. */
export function selectWidth(name: string): string {
  return `${Math.min(Math.max(name.length, 6), 20) + 3}ch`;
}
