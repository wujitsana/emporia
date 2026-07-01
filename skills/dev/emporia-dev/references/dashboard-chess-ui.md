# Emporia dashboard — chess replay UI

Operator preferences captured for **Games** (`GamesView` / `ChessReplayPanel`) and **Sessions** (`SessionsView`).

## Column wrapper (alignment)

- **`.e-chess-main`** — padding from sidebar rail (~24px left, ~20px right).
- **`.e-chess-column`** — `display: flex; flex-direction: column; width: fit-content; max-width: 100%; gap: ~12px`.
- Keeps **movelist** and **`ChessPlayersBar`** the same width as the board column. Without this, a full-width player bar centers names on the main pane and they sit **too far right** of the board.

## Layout order (main pane)

1. `.e-chess-stage` — `Chessboard`, `ChessTransport`, `ChessSpeedSlider` only.
   - `padding-top: ~10px` above board.
   - Grid `gap: ~6px` between board, transport, speed.
2. `.e-chess-moves-block` — optional `moves · click to seek` + `ChessMoveLine`.
3. `ChessPlayersBar` — after moves; `width: 100%` of column.

## Player bar

- **Component:** `dashboard/src/ui/chessMatch.tsx` → `ChessPlayersBar`.
- **No** `white` / `black` label spans — only **♔** / **♚** and agent names (`shortAgent(..., 22)`).
- **CSS:** `.e-chess-players__piece` ~1.55rem; `.e-chess-players__name` ~0.95rem.
- `aria-label` on each side for screen readers (`White: {id}`).

## Controls density

| Rule | Approx values |
|------|----------------|
| `.e-chess-ctrl-row` margin-top | 8px |
| `.e-chess-ctrl-row` gap | 4px |
| `.e-chess-speed` padding-top | 6px |
| `.e-chess-icon-btn__glyph` padding | 6px |

## Sessions footer

- `turn · {current_agent} ♔|♚` — not `(white|black)`.

## Sidebar list

- `shortAgent(white, 10) v shortAgent(black, 10)` — no color words.

## Rebuild

```bash
cd dashboard && npm run build:embedded
```

Hard-refresh `/ui/`.