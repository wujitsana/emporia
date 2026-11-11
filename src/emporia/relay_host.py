"""A relay for agents on other machines: its own Docker install (`emporia relay host`).

A relay on this machine's own Python listens on 127.0.0.1 only. One that others
reach runs in a container, behind https, with its settings in its own folder —
compose.yaml, Caddyfile and a .env — not in a Hermes profile's .env. Its keys and
database live in a Docker volume; its secrets (an operator key given by
value) in `secrets/`, one file each, mounted read-only — never environment
variables, which `docker inspect` shows (P200).

Two ways to run it:

* **Docker** — compose.yaml with the relay and Caddy. Docker runs on the host,
  and Hermes itself often runs in a container that cannot reach it, so every
  path in compose.yaml is relative to its folder: the person runs `docker
  compose` there, on the host. A Hermes agent that administers the relay
  (optional) runs in Docker beside it too.
* **A system service** — for a server kept just for the relay, without Docker:
  a systemd unit running this checkout's relay as its own system user, and
  Caddy installed on the server in front of it. Its keys and database go in the
  folder's `data/`, which only that user reads; its .env belongs to root. A
  Hermes agent on the same server (optional) is its operator and reaches it
  only over http — its tools cannot read the relay's keys or change its settings.

Either way the relay itself listens on 127.0.0.1; only the https proxy faces
the internet.
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


def in_container() -> bool:
    """Running inside a container (Docker, Podman). Only root can make these files."""
    return any(Path(p).exists() for p in ("/.dockerenv", "/run/.containerenv"))


def ways_to_run(in_container: bool, docker: bool, systemd: bool) -> tuple[list[str], str]:
    """The ways `relay host` can run a relay from here, and why, when there is no choice.

    Inside a container (Hermes usually is) a system service would be the
    container's, not the server's, and its paths would not exist on the host:
    Docker on the host is the way, with the commands shown to run there."""
    if in_container:
        return ["docker"], ("Hermes runs in a container here, so the relay goes in Docker on the host "
                            "(a system service would be the container's, not the server's).")
    if docker and not systemd:
        return ["docker"], "Docker is here and systemd is not: it runs in Docker."
    if systemd and not docker:
        return ["service"], "No Docker here: it runs as a system service (or install Docker and re-run)."
    return ["docker", "service"], ""


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
    user: "0:0"   # reads secrets/ whoever owns it, then drops to the image's user
    env_file:
      - .env
    ports:
      # This machine only. Others reach it through https{"" if https else " — your own proxy"}.
      - "127.0.0.1:{LOCAL_PORT}:8088"
    environment:
      - EMPORIA_RELAY_ENV_FILE=/app/relay.env
      # P270: believe X-Forwarded-For from whoever reaches it. Only Caddy (or your
      # proxy, through the port above, bound to this machine) and this stack can;
      # without it every client counts as the proxy's address and shares one
      # rate limit (10 sign-ins a minute for the whole internet).
      - FORWARDED_ALLOW_IPS=*
    volumes:
      - emporia-data:/data     # its identity, operator key and database: never delete
      - ./.env:/app/relay.env:ro   # who may use it, re-read when it changes
      - ./secrets:/run/secrets:ro  # one file per secret: emporia relay config set NAME
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


def caddyfile(domain: str, upstream: str = "relay:8088") -> str:
    return f"{MARK}\n{domain} {{\n\treverse_proxy {upstream}\n}}\n"


# ─── Beside Hermes: a snippet for the person's existing compose file ───────────
#
# The host has only Docker (no Emporia checkout), and Hermes already runs from a compose
# file of the person's. `relay host --into-compose` writes services/volumes/networks to
# paste into that file. The relay image builds from a git URL pinned to one commit, so the
# host needs nothing but network access to the repository. The relay sits on its own
# network; Hermes's is not on it. Its settings are in a folder of their own under the
# data volume, mounted read-only.

SNIPPET = "compose.snippet.yaml"
REPO_URL = "https://github.com/wujitsana/emporia.git"
# 2025.4 or later: `tunnel run --token-file` (the token as a file, not in `docker inspect`);
# `tunnel ready` (the healthcheck) needs 2024.11 or later.
CLOUDFLARED_IMAGE = "cloudflare/cloudflared:2026.10.4"
# The compose network the relay and its front door share. Only the front door has a fixed
# address on it, which is the one address whose client-address header the relay believes.
EDGE_SUBNET = "172.29.231.0/24"
PROXY_IP = "172.29.231.10"
RELAY_UID = 10001
CLOUDFLARED_UID = 65532
FRONT_DOORS = (
    ("tunnel", "Cloudflare Tunnel — a connector container dials out to Cloudflare; no port is open on the VPS, its IP "
               "stays hidden, and Cloudflare absorbs floods and can rate-limit before they arrive (recommended)"),
    ("caddy", "Caddy on ports 80 and 443 — your VPS faces the internet itself, with automatic certificates; no "
              "Cloudflare account needed. Pair it with a firewall"),
)


def source_pin(repo: Path) -> tuple[str, str]:
    """(git URL, full 40-hex commit) the relay image is built from.

    The runtime pin (hermes_plugin/emporia/emporia_source.json) when it names a commit,
    else this checkout's HEAD. The host builds from that URL, so the commit must be
    pushed there."""
    import json
    import subprocess

    url, sha = REPO_URL, ""
    try:
        pin = json.loads((repo / "hermes_plugin" / "emporia" / "emporia_source.json").read_text())
        if pin.get("repo"):
            url = pin["repo"].rstrip("/") + ".git"
        sha = str(pin.get("sha") or "")
    except (OSError, ValueError):
        pass
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
        sha = out.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("no commit to build the relay from: this is not a git checkout, and the runtime pin "
                         "names none (--commit takes a full 40-character commit)")
    return url, sha


def host_path(folder: Path, data_mount: str = "/opt/data", host_data: str = "${HOME}/.hermes") -> str:
    """Where the host sees `folder`, when Hermes's data volume is `data_mount` here and
    `host_data` there."""
    try:
        rel = folder.resolve().relative_to(data_mount)
    except ValueError:
        return str(folder)
    return f"{host_data}/{rel}" if str(rel) != "." else host_data


def snippet_yaml(front_door: str, host_dir: str, repo_url: str, sha: str, domain: str) -> str:
    """The services, volumes and networks to add to the person's compose file.
    `host_dir`: the settings folder as the host sees it (compose expands ${HOME})."""
    if front_door not in ("tunnel", "caddy"):
        raise ValueError(f"front door: tunnel or caddy, not {front_door!r}")
    tunnel = front_door == "tunnel"
    door = f"""
  # Front door: a connector that dials out to Cloudflare. It publishes no port. In the
  # Cloudflare dashboard the tunnel's public hostname {domain} points at http://emporia-relay:8088.
  emporia-cloudflared:
    container_name: emporia-cloudflared
    image: {CLOUDFLARED_IMAGE}
    restart: unless-stopped
    user: "{CLOUDFLARED_UID}:{CLOUDFLARED_UID}"
    command: ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:2000", "run", "--token-file", "/run/tunnel/token"]
    volumes:
      - {host_dir}/cloudflared:/run/tunnel:ro   # the tunnel token: one file, `token`
    networks:
      emporia-edge:
        ipv4_address: {PROXY_IP}
    read_only: true
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 128m
    cpus: 0.5
    pids_limit: 64
    healthcheck:
      test: ["CMD", "cloudflared", "tunnel", "--metrics", "127.0.0.1:2000", "ready"]
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
    depends_on:
      emporia-relay:
        condition: service_healthy
    logging:
      driver: json-file
      options: {{max-size: "10m", max-file: "3"}}
""" if tunnel else f"""
  # Front door: Caddy on 80 and 443 gets and renews the certificate. It overwrites
  # X-Forwarded-For with the address it saw (Caddyfile), and is the only address the relay
  # believes that header from.
  emporia-caddy:
    image: {CADDY_IMAGE}
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - {host_dir}/Caddyfile:/etc/caddy/Caddyfile:ro
      - emporia-caddy-data:/data   # certificates
    tmpfs:
      - /config
      - /tmp
    networks:
      emporia-edge:
        ipv4_address: {PROXY_IP}
    read_only: true
    cap_drop: [ALL]
    cap_add: [NET_BIND_SERVICE]   # 80 and 443 only
    security_opt: ["no-new-privileges:true"]
    mem_limit: 256m
    cpus: 0.5
    pids_limit: 128
    depends_on:
      emporia-relay:
        condition: service_healthy
    logging:
      driver: json-file
      options: {{max-size: "10m", max-file: "3"}}
"""
    header = "cf-connecting-ip" if tunnel else "x-forwarded-for"
    return f"""{MARK} — re-running it rewrites this file.
# Include this file from your Hermes compose file (an `include:` entry), or run:
#   docker compose -f <your compose file> -f {host_dir}/{SNIPPET} up -d
# Paths may use ${{HOME}}: with `sudo docker compose` that is /root, so run
# `sudo HOME=$HOME docker compose ...` or write absolute paths here.
services:
  # The relay: built from one pinned commit, on a network of its own that Hermes is not on,
  # published on no port. Hermes's agents reach it at its public https address.
  emporia-relay:
    container_name: emporia-relay
    build: {repo_url}#{sha}
    image: emporia-relay:{sha[:12]}
    restart: unless-stopped
    user: "{RELAY_UID}:{RELAY_UID}"
    read_only: true
    tmpfs:
      - /tmp
    cap_drop: [ALL]
    security_opt: ["no-new-privileges:true"]
    mem_limit: 1536m
    cpus: 2.0
    pids_limit: 256
    env_file:
      - {host_dir}/.env
    environment:
      - EMPORIA_RELAY_ENV_FILE=/app/relay.env
      # Believe {header} from the front door's one address, and from nobody else.
      - EMPORIA_CLIENT_IP_HEADER={header}
      - EMPORIA_TRUSTED_PROXIES={PROXY_IP}
    expose:
      - "8088"
    volumes:
      - emporia-data:/data   # its identity, operator key and database: never delete
      - {host_dir}/.env:/app/relay.env:ro   # who may use it; re-read when it changes
      - {host_dir}/secrets:/run/secrets:ro  # one file per secret (an operator key by value)
    networks:
      - emporia-edge
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=3)"]
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
    logging:
      driver: json-file
      options: {{max-size: "10m", max-file: "5"}}
{door}
volumes:
  emporia-data:
{"" if tunnel else "  emporia-caddy-data:" + chr(10)}
networks:
  emporia-edge:
    driver: bridge
    ipam:
      config:
        - subnet: {EDGE_SUBNET}
"""


def into_compose_caddyfile(domain: str) -> str:
    return (f"{MARK}\n{domain} {{\n\treverse_proxy emporia-relay:8088 {{\n"
            f"\t\theader_up X-Forwarded-For {{remote_host}}\n\t}}\n}}\n")


def write_into_compose_files(folder: Path, front_door: str, host_dir: str, repo_url: str, sha: str,
                             domain: str) -> list[Path]:
    """The snippet, its folder (settings, secrets/, and the tunnel's or Caddy's own), and
    the Caddyfile. The settings folder is world-traversable and `.env` readable: the
    relay runs as another user than whoever made them, and nothing secret is in `.env`."""
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    (folder / "secrets").mkdir(mode=0o700, exist_ok=True)
    files = {folder / SNIPPET: snippet_yaml(front_door, host_dir, repo_url, sha, domain)}
    if front_door == "tunnel":
        (folder / "cloudflared").mkdir(mode=0o700, exist_ok=True)
    else:
        files[folder / "Caddyfile"] = into_compose_caddyfile(domain)
    return _write(files)


def into_compose_steps(front_door: str, host_dir: str, domain: str, needs_key: bool, secrets: list[str]) -> list[str]:
    """What the person does by hand, in order, with the exact commands. The relay's
    uid owns what only it may read; the tunnel's owns the token. `needs_key`: escrow
    is on and has no operator key yet — money alone (x402, paid directly) needs none."""
    tunnel = front_door == "tunnel"
    door = "emporia-cloudflared" if tunnel else "emporia-caddy"
    if tunnel:
        steps = [
            f"Cloudflare: Zero Trust > Networks > Tunnels. Create a tunnel, or open an existing one (Configure shows "
            f"the token again). Copy only the token, the long string that starts eyJ, not the whole command. "
            f"Public hostname {domain} -> http://emporia-relay:8088. The domain's DNS must be on Cloudflare, proxied "
            f"(orange cloud). Turn WebSockets on.",
            f"Save the token on the host. Run this, paste only the token, press Enter, then Ctrl-D. Nothing "
            f"echoes, and the token is a secret: never share it.\n"
            f"        sudo install -d -o {CLOUDFLARED_UID} -g {CLOUDFLARED_UID} -m 700 {host_dir}/cloudflared\n"
            f"        sudo install -m 400 -o {CLOUDFLARED_UID} -g {CLOUDFLARED_UID} /dev/stdin {host_dir}/cloudflared/token",
        ]
    else:
        steps = [f"DNS: an A record for {domain} to this VPS. Firewall: allow 22, 80 and 443 only\n"
                 f"        sudo ufw default deny incoming && sudo ufw allow 22,80,443/tcp && sudo ufw enable"]
    steps.append(f"In your Hermes compose file, uncomment the `include:` line that lists {host_dir}/{SNIPPET} (or add "
                 f"it under `include:`). Do not copy the services, volumes or networks over.")
    if secrets or needs_key:
        steps.append(f"Give the relay's user its secrets, and nobody else:\n"
                     f"        sudo chown -R {RELAY_UID}:{RELAY_UID} {host_dir}/secrets && sudo chmod 700 {host_dir}/secrets"
                     f" && sudo chmod 600 {host_dir}/secrets/*")
    if needs_key:
        steps.append("Make the escrow operator key inside the relay's volume, then put the address it prints in the "
                     f"relay's .env ({host_dir}/.env) as EMPORIA_OPERATOR_ADDRESS=0x…:\n"
                     "        docker compose run --rm --no-deps emporia-relay python -m emporia.operator_signer generate")
    start = f"docker compose up -d --build emporia-relay {door}"
    text = f"Start it (the first build pulls the pinned commit from GitHub):\n        {start}"
    if "${HOME}" in host_dir:
        text += (f"\n        If your docker needs sudo, `sudo docker compose` sets ${{HOME}} to /root and the snippet "
                 f"paths break. Run instead:\n        sudo HOME=$HOME {start}\n"
                 f"        Or add your user to the docker group and drop sudo.")
    steps.append(text)
    steps.append(f"Check: curl -s https://{domain}/health   (status ok). Then move your agents to it: "
                 f"emporia relay use https://{domain} NAME …")
    if tunnel:
        steps.append("Optional, in Cloudflare: Security > WAF > Rate limiting rules (docs/deploy-relay.md). Keep Bot "
                     "Fight Mode, Browser Integrity Check and \"I'm Under Attack\" off for this host: every client "
                     "is an agent, and they would lock all of them out.")
    else:
        steps.append("Behind nginx or Traefik instead of Caddy: set EMPORIA_CLIENT_IP_HEADER to the header your proxy "
                     "sets, and EMPORIA_TRUSTED_PROXIES to its address, in the relay's .env.")
    return steps


SERVICE = "emporia-relay.service"
SERVICE_USER = "emporia-relay"


def service_unit(exe: Path, repo: Path, folder: Path, user: str) -> str:
    return f"""{MARK} — re-running it rewrites this file.
# Install: sudo cp {folder / SERVICE} /etc/systemd/system/
#          sudo systemctl daemon-reload && sudo systemctl enable --now emporia-relay
[Unit]
Description=Emporia relay
After=network-online.target
Wants=network-online.target

[Service]
User={user}
WorkingDirectory={repo}
EnvironmentFile={folder / ".env"}
ExecStart={exe}
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={folder / "data"}

[Install]
WantedBy=multi-user.target
"""


def service_settings(folder: Path) -> dict[str, str]:
    """Where a service relay keeps its state, and that it reads only its own .env."""
    data = folder / "data"
    return {
        "EMPORIA_NO_DOTENV": "1",                 # never a Hermes profile's .env
        "EMPORIA_RELAY_HOST": "127.0.0.1",
        "EMPORIA_RELAY_PORT": str(LOCAL_PORT),
        "EMPORIA_KEYS_DIR": str(data / "keys"),
        "EMPORIA_DB_PATH": str(data / "emporia.sqlite3"),
        "EMPORIA_LOG_DIR": str(data / "logs"),
    }


def write_service_files(folder: Path, exe: Path, repo: Path, domain: str, user: str) -> list[Path]:
    (folder / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    files = {folder / SERVICE: service_unit(exe, repo, folder, user)}
    if domain:
        files[folder / "Caddyfile"] = caddyfile(domain, f"127.0.0.1:{LOCAL_PORT}")
    return _write(files)


def write_files(folder: Path, repo: Path, domain: str, admin: Path | None = None) -> list[Path]:
    """compose.yaml and (with a domain) the Caddyfile. Refuses to replace a file
    it did not make."""
    import os

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "secrets").mkdir(mode=0o700, exist_ok=True)
    def rel(path: Path) -> str:   # "./" so compose binds a folder, not a named volume
        r = os.path.relpath(path, folder)
        return r if r.startswith(".") else f"./{r}"

    files = {folder / "compose.yaml": compose_yaml(rel(repo), bool(domain), rel(admin) if admin else "")}
    if domain:
        files[folder / "Caddyfile"] = caddyfile(domain)
    return _write(files)


def _write(files: dict[Path, str]) -> list[Path]:
    """Refuses to replace a file `emporia relay host` did not make."""
    for path in files:
        if path.exists() and MARK not in path.read_text():
            raise FileExistsError(f"{path} was not made by `emporia relay host` — move it, or pick another folder")
    for path, text in files.items():
        path.write_text(text)
    return list(files)


def settings(domain: str) -> dict[str, str]:
    """The relay's own settings, before payments."""
    return {
        "EMPORIA_RELAY_URL": f"https://{domain}" if domain else f"http://127.0.0.1:{LOCAL_PORT}",
    }


def operator_address(output: str) -> str:
    m = re.search(r"Address: (0x[0-9a-fA-F]{40})", output)
    return m.group(1) if m else ""
