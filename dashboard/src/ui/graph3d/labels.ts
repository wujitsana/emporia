/** Label placement for the graph kit: pure, no DOM. */
interface Box { x: number; y: number; w: number; h: number }
const CHAR = 5.9;
const LINE = 12;
const overlaps = (a: Box, b: Box) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;
/** Offsets tried in turn for a label next to its dot; the first that leaves every earlier label alone wins. */
const SLOTS = [[8, -4], [8, 12], [-8, -4], [-8, 12], [8, -16], [-8, -16], [8, 24], [-8, 24], [8, -28], [-8, -28]] as const;
/** Places labels so no two overlap: `wants` in priority order; a label that fits nowhere is left out (null). */
export function placeLabels(wants: readonly { x: number; y: number; text: string }[], width: number, height: number): ({ x: number; y: number; end: boolean } | null)[] {
  const taken: Box[] = [];
  return wants.map((w) => {
    const bw = w.text.length * CHAR;
    for (const [dx, dy] of SLOTS) {
      const left = dx < 0;
      const box = { x: left ? w.x + dx - bw : w.x + dx, y: w.y + dy - LINE + 2, w: bw, h: LINE };
      if (box.x < 0 || box.y < 0 || box.x + bw > width || box.y + LINE > height) continue;
      if (taken.some((t) => overlaps(box, t))) continue;
      taken.push(box);
      return { x: w.x + dx, y: w.y + dy, end: left };
    }
    return null;
  });
}

