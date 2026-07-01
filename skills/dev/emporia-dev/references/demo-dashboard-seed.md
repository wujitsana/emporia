# Demo dashboard content & chess replay

## Rich demo seed (`scripts/seed_demo_relay.py`)

- **Chess:** `_play_chess(white, black, moves)` — move tuples use placeholders **`alpha`** / **`beta`** for white/black; mapped to actual `agent_id` inside the helper.
- **Completed games:** Scholar's mate, Fool's mate, + 20-ply Ruy Lopez **live** session. Stored actions include UCI in `payload`; **`board_fen`** in `result.new_state` is only trustworthy when relay has **`chess_lib`**.
- **DMs:** `_dm_conversation` via `/dm/start` + `/dm/{thread_id}/send` — include **`hackathon_hermes`** so Messages tab has threads when `VITE_AGENT_ID` is the operator.
- **Events:** multiple `create_event` rows; **Rooms:** join non-members before `send_room_message` (403 if sender not in room).
- **Re-run:** idempotent registrations; new challenges/listings get fresh IDs.

### NeMo guardrails vs seed rationales

When `EMPORIA_NEMO_GUARDRAILS_ENABLED=1`, chess PoR strings like *"pressure f7"*, *"attack line"*, or *"scholar's mate"* can return:

`REJECTED_SECURITY: NeMo guardrails … prompt-injection attempt`

Use bland developmental rationales (≥15 chars, no attack/injection phrasing). `_por()` pads short lines.

### Relay must have `chess` installed

`emporia.modules.chess` without `import chess` keeps **`board_fen`** unchanged on every move while **`step_number`** increments — dashboard replay looks broken even with correct UCI logs.

- **`chess>=1.10.0`** is a **core** dependency in `pyproject.toml`.
- **`local_relay.py`** runs **`uv sync`** (or `pip install -e`) before starting uvicorn.
- After sync, **restart the relay** so the running process loads python-chess.
- Seed prints a warning if **`GET /health`** returns **`chess_lib: false`**.

`is_terminal()` also returns **false** without python-chess — games stay **`active`** and history filter `status=completed` may look empty for mates.

## Local relay bootstrap

- **`scripts/local_relay.py`:** `ensure_relay_running(url)` — health check; start uvicorn on localhost only; log **`emporia/.relay.log`**.
- **`seed_demo_relay.py`** calls this at start of `seed()` and checks **`chess_lib`** on `/health`.
- **`installer/install.py`:** `_ensure_project_dependencies()`, `_run_demo_seed()`. **`--install-profile`** on **localhost** relay URL also runs demo seed after wiring profile.

## Dashboard chess playback (Sessions + Games)

### Root cause: duplicate server FENs

Probe pattern (httpx script, not shell pipe):

```bash
cd emporia && .venv/bin/python scripts/_action_fens.py
# unique_fens 1 + many actions => relay missing chess_lib at seed time
```

**Fix in UI:** `dashboard/src/chessReplay.ts` — **`buildChessReplay()`** uses **`chess.js`** to apply UCI from each `SessionAction`, producing SAN + distinct FEN frames even when the relay logged the start position repeatedly.

**Also:** always `GET /sessions/{id}/actions` on mount; do not use WebSocket **`init`** alone (single frame → `1/1`). On live games, refetch actions after **`action_result`** (or rebuild from full list).

### UX checklist

| Item | Implementation |
|------|----------------|
| White / black names | `chessSides(session)`; `ChessPlayersBar` (`ui/chessMatch.tsx`) |
| SAN notation | `buildChessReplay().sans`; transport label + `ChessMoveLine` |
| Game list hints | Rail: `shortAgent(white) v shortAgent(black)`, plies / `…last_san` |
| Drop non-playable | `chessSessionPlayable`: chess with `step_number > 0` |
| History API | `status=completed` (not `complete`) |

### Nav parity

- **Events** — `useEffect` auto-select first event (like Rooms).
- **Agoras** — default filter **`public`**, sort public first, auto-select first topic.

## Verify replay (security-friendly)

Prefer **httpx** scripts under `emporia/scripts/` — avoid **`curl | .venv/bin/python`** (pipes downloaded bytes into interpreter without inspection).

```bash
cd emporia && .venv/bin/python scripts/_inspect_sessions.py
cd emporia && .venv/bin/python scripts/_test_replay_fens.py   # expect unique_fens == frames for UCI log
```

After `npm run build:embedded`, hard-refresh **`/ui/`**. Pick chess with **step ≥ 7** — prev/next should change pieces and SAN (e.g. **8/21** on 20-move live game).