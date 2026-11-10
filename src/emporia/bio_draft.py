"""A bio drafted by the agent's own model, for its person to keep, edit or redraft (never set by the agent).

`draft(profile, notes)` runs ONE one-shot turn of that profile's own Hermes model, through Hermes itself
(`hermes -p NAME chat -Q -q PROMPT`): Emporia never reads a credential or calls a provider. The prompt asks for one
public line; the reply is cleaned and must pass `wire.about_problem` or nothing is returned. The caller shows the
draft and saves only what the person keeps. SOUL.md is private: it goes into the prompt only when the person said
so (`use_soul`), as tone and background, with an instruction never to quote it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

from emporia import hermes_layout, wire

TIMEOUT_S = 90
DRAFT_MAX = 150
NOTES_MAX = 600
SOUL_MAX = 3000

_PROMPT = (
    "Write ONE line, at most {max} characters, that says what this agent does on Emporia (a marketplace where "
    "AI agents chat, play games and trade). Base it on the person's notes below. The line is PUBLIC: it appears "
    "on the agent's card for strangers to read. Plain text only: no links, no @handles, no hashtags, no emoji, "
    "no quotes around it, no markdown, no preface. Write it in the first person or a neutral third person. Reply "
    "with the line and nothing else.\n\nNotes from the person:\n{notes}\n"
)
_SOUL_PART = (
    "\nBackground on the agent's character, for tone only (private: never quote it, never reveal instructions, "
    "file names, headers, keys or any private detail from it, and never mention that it exists):\n"
    "<<<\n{soul}\n>>>\n"
)


def prompt_for(notes: str, soul: str = "") -> str:
    text = _PROMPT.format(max=DRAFT_MAX, notes=notes.strip()[:NOTES_MAX])
    return text + (_SOUL_PART.format(soul=soul.strip()[:SOUL_MAX]) if soul.strip() else "")


def clean_reply(output: str) -> str:
    """The first non-empty line of Hermes's reply (its `session_id:` trailer ignored), without quotes or markdown."""
    for raw in str(output or "").splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("session_id:"):
            continue
        line = re.sub(r"^(?:[#>*\-]+\s+|\d+[.)]\s+)", "", line)
        line = re.sub(r"^(?:bio|line)\s*:\s*", "", line, flags=re.I)
        line = line.replace("**", "").replace("__", "").replace("`", "")
        line = line.strip().strip("\"'“”‘’").strip()
        return " ".join(line.split())
    return ""


def _run_hermes(profile: Path, prompt: str) -> subprocess.CompletedProcess:
    from emporia import rounds
    import os
    env = {k: v for k, v in os.environ.items() if k not in rounds._BYPASS}
    return subprocess.run([rounds.hermes_bin(), "-p", hermes_layout.name_of(Path(profile)), "chat", "-Q", "-q", prompt],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT_S,
                          check=False, env=env)


def draft_with_reason(profile: Path, notes: str, use_soul: bool = False) -> tuple[str, str]:
    """(bio, "") or ("", why not)."""
    if not str(notes or "").strip():
        return "", "no notes to draft from"
    soul = ""
    if use_soul:
        try:
            soul = (Path(profile) / "SOUL.md").read_text(errors="replace")
        except OSError:
            soul = ""
    try:
        r = _run_hermes(profile, prompt_for(notes, soul))
    except Exception as e:      # no hermes, a timeout, an OS error: say which kind, never a path or a secret
        return "", f"the model turn did not run ({type(e).__name__})"
    if r.returncode != 0:
        return "", "the model turn failed (hermes exited with an error)"
    text = clean_reply(r.stdout)
    if not text:
        return "", "the model gave no line"
    if why := wire.about_problem(text):
        return "", f"the draft cannot be a bio: {why}"
    if len(text) > DRAFT_MAX:
        return "", f"the draft is {len(text)} characters (asked for at most {DRAFT_MAX})"
    return text, ""


def draft(profile: Path, notes: str, use_soul: bool = False) -> str:
    """One line of bio from the person's notes, or "" when the model gave nothing usable."""
    return draft_with_reason(profile, notes, use_soul)[0]


def prefill_input(question: str, text: str) -> str:
    """A typed line with `text` already in it, to edit (falls back to showing it as the default)."""
    try:
        import readline
        readline.set_startup_hook(lambda: readline.insert_text(text))
        try:
            return input(f"{question}: ").strip()
        finally:
            readline.set_startup_hook()
    except (ImportError, OSError):
        return (input(f"{question} [{text}]: ").strip() or text)


def review(profile: Path, notes: str, use_soul: bool, *, read: Callable[[str], str],
           edit: Callable[[str, str], str], say: Callable[[str], None] = print) -> str | None:
    """Draft, show, and ask: keep / edit / redraft / none. Returns the bio to save, "" for none, None when the
    person gave up with nothing chosen. Nothing here saves anything."""
    text, why = "", ""
    while True:
        text, why = draft_with_reason(profile, notes, use_soul)
        if text:
            say(f"  Draft: {text!r}  ({len(text)} characters)")
            options = "[k]eep / [e]dit / [r]edraft / [n]one"
        else:
            say(f"  No draft: {why}.")
            options = "[e]dit (write it) / [r]edraft / [n]one"
        while True:
            pick = read(f"{options}").strip().lower()[:1]
            if pick == "k" and text:
                return text
            if pick == "n":
                return ""
            if pick == "r":
                break
            if pick == "e":
                while True:
                    new = edit(f"Bio (at most {wire.ABOUT_MAX} characters, plain text, no link)", text).strip()
                    if not (problem := wire.about_problem(new)):
                        return new
                    say(f"  {problem}")
            say("  k, e, r or n.")
