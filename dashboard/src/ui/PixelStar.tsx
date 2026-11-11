/**
 * The brand's star, drawn on a 7 x 7 pixel grid like the wordmark's face (Silkscreen has no star glyph,
 * so a typed ✶ fell back to another font and looked off). Inline SVG: it takes the text's colour
 * (`currentColor`), is as tall as the capitals beside it and is hidden from screen readers.
 */
const CELLS = [
  [3, 0],
  [1, 1], [3, 1], [5, 1],
  [2, 2], [3, 2], [4, 2],
  [0, 3], [1, 3], [2, 3], [3, 3], [4, 3], [5, 3], [6, 3],
  [2, 4], [3, 4], [4, 4],
  [1, 5], [3, 5], [5, 5],
  [3, 6],
] as const;

export function PixelStar({ className = "e-pixel-star", size }: { className?: string; size?: number | string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 7 7"
      width={size}
      height={size}
      shapeRendering="crispEdges"
      fill="currentColor"
      aria-hidden="true"
      focusable="false"
    >
      {CELLS.map(([x, y]) => <rect key={`${x}-${y}`} x={x} y={y} width="1" height="1" />)}
    </svg>
  );
}
