"""The one place the relay's configuration is resolved (P55).

Before this module there were **four** module-level constants naming the
database — in `relay_server`, `rooms`, `entitlements` and `engine.game_registry`
— and their defaults disagreed, by filename *and* by directory:

    relay_server.DATABASE_PATH         ~/.hermes/emporia.sqlite3
    rooms.ROOM_DB_PATH                 ~/.hermes/emporia_relay.sqlite3
    entitlements.ENTITLEMENTS_DB_PATH  ~/.hermes/emporia_relay.sqlite3

They converged only because importing `relay_server` ran the profile-dotenv
merge as a side effect, populating `EMPORIA_DB_PATH` before the other two were
read. Import `rooms` or `entitlements` first and they captured a *different
database* — a second, empty store, silently. Nothing enforced the order.

Two changes make that class of bug unreachable rather than merely unlikely:

1. **One resolver.** `database_path()` is the only reader of `EMPORIA_DB_PATH`
   in the package, enforced by a test. There is no second default left to
   disagree with, so the modules cannot drift apart no matter who imports whom.

2. **The dotenv merge belongs here, not in `relay_server`.** Every module that
   needs configuration imports this one, so the merge runs before the first
   resolution in *any* process — including the MCP server, which previously ran
   without it and was the system's second, undocumented configuration strategy.
   The merge is `setdefault`-only (see `_merge_dotenv`), so widening its reach
   can fill gaps and can never override a value an operator set explicitly.

Deliberately dependency-free: this module imports nothing from `emporia`, so it
can sit underneath `rooms` and `entitlements` (which are themselves independent
of `relay_server` on purpose — see `entitlements`'s module docstring).
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_DB_FILENAME = "~/.hermes/emporia.sqlite3"

_dotenv_loaded = False


def _merge_dotenv(path: Path) -> None:
    """Merge `path` into os.environ with **setdefault semantics**.

    A value already present in the environment always wins — *including an
    explicitly empty one*; a blank value in the file is ignored rather than
    used to clear one. That is what makes it safe for this to run in every
    process: it can only fill a gap.

    P71: "present" used to mean `os.environ.get(key)` — truthiness, not
    presence — so an explicitly empty value did not win and the file refilled
    it. That made `VAR= emporia-relay` unable to switch off anything the
    profile `.env` had set, which is precisely what the Stripe live-key
    refusal (P62/P65) tells an operator to do: "Not using Stripe at all —
    unset STRIPE_SECRET_KEY." From a Hermes profile that sets it, that advice
    could not be followed per process; the only route was editing the shared
    profile file, which is the operator's real credential store. Found by
    running a two-relay federation harness, not by reading this function —
    three restarts, each refused for the same reason.

    An explicit empty string is a deliberate statement ("this relay has no
    Stripe key"), and a config layer that silently overrules it is answering a
    question the operator already answered.
    """
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            key = k.strip()
            val = v.strip().strip('"').strip("'")
            if not val:
                continue
            if key not in os.environ:
                os.environ[key] = val


def load_profile_dotenv() -> None:
    """Load the Hermes profile `.env` first, then the emporia repo `.env`.

    Idempotent — safe to call from any entry point, and called once at import
    below so that no caller has to remember. When emporia lives under
    `profiles/<name>/emporia`, the repo-level `.env` must not shadow the
    profile, which is why the two are walked separately rather than stopping at
    the first `.env` found (the original walk did stop, and the repo file won).
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    # P108: the test suite sets this. Pinning variables one by one kept
    # missing the next one an operator added (STRIPE_API_VERSION, then the
    # x402 and platform-plugin settings), each leaking a live value in.
    if os.getenv("EMPORIA_NO_DOTENV", "").strip() == "1":
        return
    # P109: an agent profile names its own .env. The walk below starts at
    # this *code's* location, so an agent whose MCP server runs from another
    # profile's checkout (alpha and beta running hackathon_hermes/emporia) got
    # that profile's .env: its key directory, Nous token and Stripe secret.
    # Hermes passes an MCP server only its config `env`, never the profile's
    # .env, so the installer writes EMPORIA_DOTENV into that env.
    explicit = os.getenv("EMPORIA_DOTENV", "").strip()
    if explicit:
        _merge_dotenv(Path(explicit).expanduser())
        return
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config.yaml").exists():
            _merge_dotenv(parent / ".env")
            break
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "src" / "emporia").is_dir():
            _merge_dotenv(parent / ".env")
            break


load_profile_dotenv()


def database_path() -> Path:
    """The single SQLite file every store in this relay writes to.

    Resolved on each call rather than cached: `relay_server` captures it once
    into `DATABASE_PATH` at import, and a cached-at-first-call value could
    disagree with that if anything resolved before the dotenv merge. Reading an
    env var and expanding a path costs nothing next to opening the connection
    it is about to be used for.
    """
    return Path(os.getenv("EMPORIA_DB_PATH", DEFAULT_DB_FILENAME)).expanduser()
