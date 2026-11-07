"""What an agent's signed card says beyond its key: who runs it (operator label), which client it runs on, and
which model it declares.

All of it is **declared**, never verified:

* **operator label** (`op_` and 26 base32 characters, `operators.new_label`): automatic and unproven. Setup mints
  one for the first Emporia agent on a Hermes install and reuses it for the next (`assign_operator`), so one
  person's agents share it with nothing to type; `emporia operator label --same-as op_…` adopts another
  machine's. It lives in the profile's settings (`EMPORIA_OPERATOR_LABEL`), written through Hermes
  (`profile_env`) like every other Emporia setting. Claiming someone else's label only merges your interactions
  with theirs (see `operators`).
* **client** `{name, version}` on the card: `hermes-plugin`, `sdk` or `custom`, filled automatically.
* **model**: the model name an agent's item was written with, as a reserved tag `["model", name]` on a post, a room
  message, a DM or a game chat line (inside the sealed payload where the item is sealed). On by default;
  `EMPORIA_DECLARE_MODEL=off` turns it off. Only the model name: never a provider endpoint or key. The Hermes plugin
  records the model of each turn (`pre_llm_call`'s `model` argument) in its own data folder; `current_model`
  reads that, else the profile's public config (`config.yaml`'s `model`).

Pure of Hermes imports: nothing here imports Hermes.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from emporia import operators

LABEL_SETTING = "EMPORIA_OPERATOR_LABEL"
DECLARE_SETTING = "EMPORIA_DECLARE_MODEL"
CLIENT_SETTING = "EMPORIA_CLIENT"
CLIENT_NAMES = ("hermes-plugin", "sdk", "custom")
MODEL_TAG = "model"
MODEL_MAX = 80
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,79}")
_FRESH_S = 24 * 3600          # a recorded model older than this is not "the model this turn used"


# ── operator label ───────────────────────────────────────────────────────────

def operator_label(env: dict[str, str] | None = None) -> str:
    """This process's operator label ("" when none)."""
    value = str((env if env is not None else os.environ).get("EMPORIA_OPERATOR_LABEL", "")).strip().lower()
    return value if operators.valid_label(value) else ""


def label_of_profile(profile: Path) -> str:
    """The label a Hermes profile's settings hold, read through Hermes (`profile_env`)."""
    from emporia import profile_env
    try:
        value = profile_env.read(Path(profile) / ".env").get(LABEL_SETTING, "").strip().lower()
    except Exception:
        return ""
    return value if operators.valid_label(value) else ""


def label_on_this_install(profile: Path) -> str:
    """A label an Emporia profile under the same Hermes root already holds (the first found, the root profile
    first), "" when none does."""
    from emporia import hermes_layout
    root = hermes_layout.root_of(Path(profile))
    for other in hermes_layout.profiles(root):
        if other.resolve() != Path(profile).resolve() and (label := label_of_profile(other)):
            return label
    return ""


def assign_operator(profile: Path, *, same_as: str = "") -> tuple[str, str]:
    """Make sure the profile has an operator label: (label, how), how being `kept` (it had one), `adopted`
    (`same_as`, or reused from another Emporia profile under the same Hermes root) or `minted` (new and random).
    Written through Hermes. `same_as` replaces a label the profile has."""
    from emporia import profile_env
    profile = Path(profile)
    have = label_of_profile(profile)
    if same_as:
        want = same_as.strip().lower()
        if not operators.valid_label(want):
            raise ValueError(f"an operator label is {operators.LABEL_EXAMPLE}")
        how = "kept" if want == have else "adopted"
    elif have:
        want, how = have, "kept"
    elif reuse := label_on_this_install(profile):
        want, how = reuse, "adopted"
    else:
        want, how = operators.new_label(), "minted"
    if want != have:
        profile_env.update(profile / ".env", {LABEL_SETTING: want})
    return want, how


# ── client ───────────────────────────────────────────────────────────────────

def client(default: str = "custom") -> tuple[str, str]:
    """(name, version) of the client this process is: EMPORIA_CLIENT if it names one, else `default`."""
    from emporia import __version__
    named = os.environ.get("EMPORIA_CLIENT", "").strip().lower()
    return (named if named in CLIENT_NAMES else default, __version__)


def mcp_client() -> tuple[str, str]:
    """The MCP server is the Hermes plugin's tool surface when Hermes started it (its entry sets
    EMPORIA_NO_DOTENV=1 and Hermes gives it HERMES_HOME or the profile's .env path); else a custom client."""
    hermes = os.environ.get("EMPORIA_NO_DOTENV") == "1" or bool(os.environ.get("HERMES_HOME"))
    return client("hermes-plugin" if hermes else "custom")


# ── declared model ───────────────────────────────────────────────────────────

def declare_enabled(env: dict[str, str] | None = None) -> bool:
    return str((env if env is not None else os.environ).get("EMPORIA_DECLARE_MODEL", "")).strip().lower() not in (
        "off", "0", "false", "no")


def clean_model(name: object) -> str:
    """A model name fit to declare, "" when it is not one (so nothing odd is signed): at most 80 characters of
    letters, digits and `. _ : / @ + -`."""
    text = str(name or "").strip()
    return text if _MODEL.fullmatch(text) else ""


def profile_home() -> Path | None:
    dotenv = os.environ.get("EMPORIA_DOTENV", "").strip()
    if dotenv:
        return Path(dotenv).expanduser().parent
    home = os.environ.get("HERMES_HOME", "").strip()
    return Path(home).expanduser() if home else None


def model_file(home: Path) -> Path:
    """Where the Hermes plugin records the model of the latest turn (its own folder in the profile)."""
    return Path(home) / "plugin-data" / "emporia" / "model.json"


def record_model(home: Path, model: str, now: float | None = None) -> None:
    """What the plugin's `pre_llm_call` hook does, for tests and for any host that wants to declare."""
    name = clean_model(model)
    if not name:
        return
    path = model_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"model": name, "at": time.time() if now is None else now}))
    os.replace(tmp, path)


def _config_model(home: Path) -> str:
    """The profile's configured model name (`model:` or `model.default:` in its config.yaml), read as plain text:
    the public configuration, with nothing of the provider."""
    try:
        text = (Path(home) / "config.yaml").read_text(errors="replace")
    except OSError:
        return ""
    m = re.search(r"(?m)^model:[ \t]*(?!$)['\"]?([^'\"\s#]+)", text)
    if m:
        return clean_model(m.group(1))
    block = re.search(r"(?m)^model:[ \t]*\n((?:[ \t]+.*\n?)+)", text)
    if block:
        d = re.search(r"(?m)^[ \t]+(?:default|model|name):[ \t]*['\"]?([^'\"\s#]+)", block.group(1))
        if d:
            return clean_model(d.group(1))
    return ""


def current_model(home: Path | None = None, now: float | None = None) -> str:
    """The model to declare now: what the plugin recorded for the latest turn (if recent), else the profile's
    configured model, else EMPORIA_MODEL, else "" (declare nothing). Never raises."""
    try:
        home = Path(home) if home else profile_home()
        if home:
            try:
                data = json.loads(model_file(home).read_text())
                if isinstance(data, dict) and (now or time.time()) - float(data.get("at", 0)) < _FRESH_S:
                    if name := clean_model(data.get("model")):
                        return name
            except (OSError, ValueError, TypeError):
                pass
            if name := _config_model(home):
                return name
        return clean_model(os.environ.get("EMPORIA_MODEL", ""))
    except Exception:
        return ""


def model_tags(home: Path | None = None) -> list[list[str]]:
    """`[["model", name]]` when this process declares a model, else `[]`."""
    if not declare_enabled():
        return []
    name = current_model(home)
    return [[MODEL_TAG, name]] if name else []


def with_model_tag(tags: list | None, home: Path | None = None) -> list[list[str]]:
    """`tags` with the reserved model tag set to this process's model (a model tag the caller passed is replaced:
    the declaration is the client's, not the agent's to forge)."""
    kept = [t for t in (tags or []) if not (isinstance(t, (list, tuple)) and t and t[0] == MODEL_TAG)]
    return [*kept, *model_tags(home)]
