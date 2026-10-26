"""A relay for agents on other machines: its own Docker install (`emporia relay host`).

A relay on this machine's own Python listens on 127.0.0.1 only. One that others
reach runs in a container, behind https, with its settings in its own folder —
compose.yaml, Caddyfile and a .env — not in a Hermes profile's .env. Its keys and
database live in a Docker volume.

Docker runs on the host, and Hermes itself often runs in a container that cannot
reach it, so every path in compose.yaml is relative to its folder: the person
runs `docker compose` there, on the host, wherever that folder is mounted. A
Hermes agent that administers the relay (optional) runs in Docker beside it too.
"""

from __future__ import annotations

import re
from pathlib import Path

MARK = "# Made by `emporia relay host`"
# The address this machine's own agents and dashboard reach it on; 8088 stays
# free for a relay run with `emporia relay start`.
LOCAL_PORT = 8089
CADDY_IMAGE = "caddy:2.8"
# The relay's address from a container on the same stack (the admin agent).
INSIDE_URL = "http://relay:8088"


def valid_domain(domain: str) -> bool:
    return bool(re.fullmatch(r"(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain))


def compose_yaml(build: str, https: bool, admin: str = "") -> str:
    """`build` and `admin` (a Hermes profile folder) are relative to the compose file."""
    proxy = f"""
  # https in front of the relay: Caddy gets and renews the certificate itself.
  https:
    image: {CADDY_IMAGE}
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    depends_on:
      - relay
""" if https else ""
    agent = f"""
  # A Hermes agent that administers the relay, in Docker beside it, never on
  # the host: docker compose --profile admin up -d. On this stack's own
  # network, it reaches the relay at {INSIDE_URL} and nothing on the host.
  admin:
    profiles: ["admin"]
    image: ${{HERMES_IMAGE:-nousresearch/hermes-agent:latest}}
    restart: unless-stopped
    depends_on:
      - relay
    volumes:
      - {admin}:/opt/data
    command: ["gateway", "run"]
""" if admin else ""
    return f"""{MARK} — re-running it rewrites this file.
# Start: docker compose up -d     Logs: docker compose logs -f relay
name: emporia-relay
services:
  relay:
    build: {build}
    image: emporia-relay
    restart: unless-stopped
    env_file:
      - .env
    ports:
      # This machine only. Others reach it through https{"" if https else " — your own proxy"}.
      - "127.0.0.1:{LOCAL_PORT}:8088"
    volumes:
      - emporia-data:/data     # its identity, operator key and database: never delete
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
{proxy}{agent}
volumes:
  emporia-data:
{"  caddy-data:" if https else ""}
"""


def caddyfile(domain: str) -> str:
    return f"{MARK}\n{domain} {{\n\treverse_proxy relay:8088\n}}\n"


def write_files(folder: Path, repo: Path, domain: str, admin: Path | None = None) -> list[Path]:
    """compose.yaml and (with a domain) the Caddyfile. Refuses to replace a file
    it did not make."""
    import os

    folder.mkdir(parents=True, exist_ok=True)
    def rel(path: Path) -> str:   # "./" so compose binds a folder, not a named volume
        r = os.path.relpath(path, folder)
        return r if r.startswith(".") else f"./{r}"

    files = {folder / "compose.yaml": compose_yaml(rel(repo), bool(domain), rel(admin) if admin else "")}
    if domain:
        files[folder / "Caddyfile"] = caddyfile(domain)
    for path in files:
        if path.exists() and MARK not in path.read_text():
            raise FileExistsError(f"{path} was not made by `emporia relay host` — move it, or pick another folder")
    for path, text in files.items():
        path.write_text(text)
    return list(files)


def settings(domain: str, require_nous: bool) -> dict[str, str]:
    """The relay's own settings, before payments."""
    return {
        "EMPORIA_RELAY_URL": f"https://{domain}" if domain else f"http://127.0.0.1:{LOCAL_PORT}",
        "EMPORIA_REQUIRE_NOUS": "1" if require_nous else "0",
    }


def operator_address(output: str) -> str:
    m = re.search(r"Address: (0x[0-9a-fA-F]{40})", output)
    return m.group(1) if m else ""
