"""The signer apart from the agents (`emporia signer host`, P207).

Run as the agents' own user, the signer is practice: the rules hold, but a
rogue agent could read its folder. Apart from them it is a wall. Which "apart":

* **Hermes in Docker** — the signer in a container of its own beside Hermes's,
  from this checkout's image. The two share one named volume, holding only the
  socket (`emporia-signer-socket`, mounted at /run/emporia-signer in both); the
  signer's folder is a volume Hermes never mounts. An `admin` service runs
  `emporia signer add|pending|approve|audit` on the host, mounting Hermes's data
  for that one run — nothing of the signer's ever enters Hermes's container.
* **Hermes on the host, the signer in Docker** (Linux, macOS, Windows) — a
  host process cannot reach a socket inside a Docker volume, and Docker Desktop
  passes none across its VM, so the signer listens on a port published on the
  host's 127.0.0.1 only (8790); the token decides who may ask. Hermes's data is
  mounted at its own path for `admin`, so the profiles' paths still match.
* **Hermes on the host, without Docker** — a systemd service under its own
  system user, `emporia-signer`: its folder in /var/lib (0700, that user's
  alone), its socket in /run/emporia-signer (0755). The agents run as the person
  and can reach the socket, nothing else.

The socket's folder must not be writable by the agents: one that could replace
the socket could pose as the signer and collect the operator's admin token.
"""

from __future__ import annotations

import re
from pathlib import Path

from emporia.relay_host import MARK, _write

SOCKET_DIR = "/run/emporia-signer"
SOCKET = f"{SOCKET_DIR}/signer.sock"
URL = f"unix://{SOCKET}"
SOCKET_VOLUME = "emporia-signer-socket"
SERVICE = "emporia-signer.service"
SERVICE_USER = "emporia-signer"
STATE_DIR = "/var/lib/emporia-signer"


PORT = 8790
PORT_URL = f"http://127.0.0.1:{PORT}"


def data_source(mount: str, mountinfo: str | None = None) -> tuple[str, str]:
    """P211: what Hermes's data folder (`mount`, inside its container) is on the
    host — ("bind", host path), ("volume", its name) or ("", "") when it cannot
    tell. From /proc/self/mountinfo: a guess to confirm, never relied on blind."""
    try:
        lines = (mountinfo if mountinfo is not None else Path("/proc/self/mountinfo").read_text()).splitlines()
    except OSError:
        return "", ""
    for line in lines:
        f = line.split()
        if len(f) > 4 and f[4] == mount:
            if m := re.fullmatch(r".*/volumes/([^/]+)/_data", f[3]):
                return "volume", m.group(1)
            if f[3] != "/":
                return "bind", f[3]
    return "", ""


def compose_yaml(build: str, hermes_in_docker: bool = True, hermes_mount: str = "/opt/data",
                 hermes_volume: str = "") -> str:
    """`build` is the checkout, relative to the compose file. Hermes in Docker
    shares the socket volume; Hermes on the host reaches a port on 127.0.0.1.
    `hermes_mount`: where Hermes's container has its data (its HERMES_HOME), so
    the admin service sees the profiles at the paths their settings name;
    `hermes_volume`: that data's named volume, when it is one."""
    env = f"""      - EMPORIA_SIGNER_DIR=/data/signer
      - EMPORIA_SIGNER_SOCKET={SOCKET}
      - EMPORIA_NO_DOTENV=1"""
    if hermes_in_docker:
        reach = f"""      - {SOCKET_VOLUME}:{SOCKET_DIR}       # the socket, shared with Hermes — nothing else is"""
        command, ports, admin_url = '["emporia", "signer", "serve"]', "", ""
        hermes_home = hermes_mount
        data = f"hermes-data:{hermes_mount}" if hermes_volume else f"${{HERMES_DATA}}:{hermes_mount}"
        volumes_tail = f"""  {SOCKET_VOLUME}:
    name: {SOCKET_VOLUME}
""" + (f"""  hermes-data:                           # Hermes's own data volume
    external: true
    name: {hermes_volume}
""" if hermes_volume else "")
    else:
        reach = ""
        command = f'["emporia", "signer", "serve", "--port", "{PORT}"]'
        ports = f"""    ports:
      - "127.0.0.1:{PORT}:{PORT}"               # this machine only
"""
        admin_url = f"\n      - EMPORIA_SIGNER_URL=http://signer:{PORT}"
        data, hermes_home = "${HERMES_DATA}:${HERMES_DATA}", "${HERMES_DATA}"
        volumes_tail = ""
    return f"""{MARK} — re-running it rewrites this file.
# Start it first (it creates the socket volume Hermes then mounts):
#   docker compose up -d signer
# Its operator, on the host — Hermes's container never sees its folder:
#   docker compose run --rm admin add NAME      docker compose run --rm admin pending
#   docker compose run --rm admin approve ID    docker compose run --rm admin audit
name: emporia-signer
services:
  signer:
    build: {build}
    image: emporia-relay
    restart: unless-stopped
    user: "10001"
    command: {command}
    environment:
{env}
{ports}    volumes:
      - signer-data:/data                       # agents' wallet keys and limits: back it up, never delete it
{reach}
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"

  # The operator's commands. As root for `add`, which reads an agent's keys from
  # Hermes's data and hands the new files to the signer's user.
  admin:
    profiles: ["admin"]
    image: emporia-relay
    user: "0"
    entrypoint: ["emporia", "signer"]
    environment:
{env}
      - HERMES_HOME={hermes_home}{admin_url}
    volumes:
      - signer-data:/data
{reach}
      - {data}      # Hermes's data folder on the host (.env beside this file)

volumes:
  signer-data:
{volumes_tail}"""


def hermes_compose_lines(service: str = "gateway") -> str:
    """What Hermes's own compose file needs: the socket volume, nothing more."""
    return f"""services:
  {service}:            # your Hermes service
    volumes:
      - {SOCKET_VOLUME}:{SOCKET_DIR}
volumes:
  {SOCKET_VOLUME}:
    external: true"""


def service_unit(exe: Path) -> str:
    return f"""{MARK} — re-running it rewrites this file.
# Install: sudo cp {SERVICE} /etc/systemd/system/ && sudo systemctl daemon-reload
#          sudo systemctl enable --now emporia-signer
[Unit]
Description=Emporia signer — agents' wallet keys apart from the agents
After=network-online.target
Wants=network-online.target

[Service]
User={SERVICE_USER}
Environment=EMPORIA_SIGNER_DIR={STATE_DIR}
Environment=EMPORIA_SIGNER_SOCKET={SOCKET}
Environment=EMPORIA_NO_DOTENV=1
StateDirectory=emporia-signer
StateDirectoryMode=0700
RuntimeDirectory=emporia-signer
RuntimeDirectoryMode=0755
ExecStart={exe} signer serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
"""


def service_env() -> str:
    """The variables the operator's own `emporia signer …` commands need (via sudo)."""
    return f"EMPORIA_SIGNER_DIR={STATE_DIR} EMPORIA_SIGNER_SOCKET={SOCKET}"


def write_docker(folder: Path, repo: Path, hermes_data: str, hermes_in_docker: bool = True,
                 hermes_mount: str = "/opt/data", hermes_volume: str = "") -> list[Path]:
    import os
    folder.mkdir(parents=True, exist_ok=True)
    rel = os.path.relpath(repo, folder)
    files = _write({folder / "compose.yaml": compose_yaml(rel if rel.startswith(".") else f"./{rel}",
                                                          hermes_in_docker, hermes_mount, hermes_volume)})
    env = folder / ".env"
    if not env.exists():
        env.write_text(f"{MARK}\n# Hermes's data folder on the host — the one mounted at /opt/data in its container.\n"
                       f"HERMES_DATA={hermes_data}\n")
        files.append(env)
    return files


def write_service(folder: Path, exe: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    return _write({folder / SERVICE: service_unit(exe)})
