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
# A snippet staged beside an old-layout one the live relay still includes.
STAGED_SNIPPET = "compose.snippet.staged.yaml"
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
                         "names none (a full 40-character commit is needed)")
    return url, sha


def host_path(folder: Path, data_mount: str = "/opt/data", host_data: str = "${HOME}/.hermes") -> str:
    """Where the host sees `folder`, when Hermes's data volume is `data_mount` here and
    `host_data` there."""
    try:
        rel = folder.resolve().relative_to(data_mount)
    except ValueError:
        return str(folder)
    return f"{host_data}/{rel}" if str(rel) != "." else host_data


# The install folder as the operator's own shell spells it. The wizard runs in the Hermes
# container and cannot know the host's home, so the printed commands expand it on the host.
INSTALL_DIR = "$HOME/.emporia"
# Where the staged snippet carries the install folder; the printed install step swaps in the real path.
DIR_PLACEHOLDER = "__EMPORIA_DIR__"


def install_dir_problem(host_dir: str) -> str:
    """Why `host_dir` cannot be the install folder, or "" if it can. It is where the relay's
    files live on the host: the default `$HOME/.emporia`, or an absolute path outside the Hermes
    data folder (the Hermes container can read and edit everything in that one)."""
    if host_dir == INSTALL_DIR:
        return ""
    padded = host_dir.rstrip("/") + "/"
    if "/.hermes/" in padded or "$HOME/.hermes" in host_dir or "${HOME}/.hermes" in host_dir \
            or padded.startswith("/opt/data/"):
        return (f"--host-dir {host_dir} is inside the Hermes data folder. The Hermes container can read and edit "
                f"everything there, including the relay's settings and secrets. Use a folder outside it, "
                f"like the default ({INSTALL_DIR}).")
    if not host_dir.startswith("/") or "$" in host_dir:
        return f"--host-dir must be an absolute path on the host, like /srv/emporia-relay (not {host_dir!r})."
    if not re.fullmatch(r"[A-Za-z0-9/._-]+", host_dir):
        return (f"--host-dir {host_dir!r} has a character outside A-Z a-z 0-9 / . _ - : the printed commands "
                f"cannot carry it. Use a plainer folder name, like /srv/emporia-relay.")
    return ""


def snippet_yaml(front_door: str, host_dir: str, repo_url: str, sha: str, domain: str) -> str:
    """The services, volumes and networks to add to the person's compose file.
    `host_dir`: the install folder on the host. The wizard passes DIR_PLACEHOLDER, and the printed
    install step swaps in the real path: nothing here may rely on ${HOME}, which `sudo docker
    compose` expands to /root."""
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
# Installed in {host_dir}, outside the Hermes data folder. Every path below is absolute and
# none uses a variable, so it does not matter what HOME is when docker compose runs. Include
# this file from your Hermes compose file (an `include:` entry), or run:
#   docker compose -f <your compose file> -f {host_dir}/{SNIPPET} up -d
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
      - {host_dir}/data:/data   # its identity, operator key and database: never delete
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
{"" if tunnel else "volumes:" + chr(10) + "  emporia-caddy-data:" + chr(10) + chr(10)}networks:
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
                             domain: str, snippet_name: str = SNIPPET, keep: dict | None = None) -> list[Path]:
    """The staged snippet (and the Caddyfile). The wizard can only write into this folder, inside the
    Hermes data folder, so these are copies: the printed steps install them into the install folder
    and remove them. Nothing secret is written here. The relay's database, secrets/ and the
    tunnel's token live in the install folder only."""
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    text = snippet_yaml(front_door, host_dir, repo_url, sha, domain)
    for service, values in (keep or {}).items():
        text = _keep_service_values(text, service, values)
    files = {folder / snippet_name: text}
    if front_door != "tunnel":
        files[folder / "Caddyfile"] = into_compose_caddyfile(domain)
    return _write(files)


_KEPT_KEYS = ("container_name", "mem_limit", "cpus", "pids_limit")


def _keep_service_values(text: str, service: str, values: dict) -> str:
    """Set container_name and the limits of one service in a rendered snippet to `values`."""
    out, inside = [], False
    for line in text.splitlines():
        m = re.match(r"^  ([a-z-]+):\s*$", line)
        if m:
            inside = m.group(1) == service
        elif re.match(r"^\S", line):
            inside = False
        if inside:
            for k in _KEPT_KEYS:
                if k in values and re.match(rf"^    {k}: ", line):
                    line = f"    {k}: {values[k]}"
        out.append(line)
    return "\n".join(out) + "\n"


def old_layout(text: str) -> dict | None:
    """What a snippet made before the install folder moved out of the Hermes data folder says: its
    pinned commit and repo, its front door, its domain, and each service's container name and limits.
    None when `text` is not one of ours or already has the new layout."""
    import yaml
    if MARK not in text:
        return None
    try:
        spec = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    services = spec.get("services") or {}
    relay = services.get("emporia-relay")
    if not isinstance(relay, dict):
        return None
    mounts = [str(v) for v in relay.get("volumes") or []]
    old = (any(".hermes" in m or "${HOME}" in m for m in mounts)
           or "emporia-data:/data" in mounts or "emporia-data" in (spec.get("volumes") or {}))
    if not old:
        return None
    build = str(relay.get("build") or "")
    url, _, sha = build.partition("#")
    door = "tunnel" if "emporia-cloudflared" in services else "caddy" if "emporia-caddy" in services else ""
    m = re.search(r"public hostname (\S+) points at", text)
    keep = {name: {k: svc[k] for k in _KEPT_KEYS if k in svc}
            for name, svc in services.items() if isinstance(svc, dict)}
    return {"url": url, "sha": sha if re.fullmatch(r"[0-9a-f]{40}", sha) else "", "front_door": door,
            "domain": m.group(1) if m else "", "keep": keep}


STAGING_DEFAULT = "$HOME/.hermes/emporia-relay"
SESSION_NOTE = ("Keep this one terminal for all the steps: D and S stay set in it. In a new terminal, run the "
                "folder lines again before anything else.")
COMPOSE_HOME_NOTE = ("If your own Hermes compose file uses ${HOME} in its paths, run compose as sudo HOME=\"$HOME\" "
                     "docker compose (and the rest of the command) or put absolute paths in that file. This snippet "
                     "never needs it. Compose needs sudo here: the relay's .env is readable by root and the relay "
                     "only.")
_PROSE = "     "      # prose sits at 5 spaces; commands at 8, so pasting a block never runs prose
_CMD = "        "
_DIR_CHECK = ('case "$D$S" in *[!A-Za-z0-9/._-]*) echo "STOP: D or S has a character outside A-Z a-z 0-9 / . _ -";; '
              '*) echo "folders ok: $D  <-  $S";; esac')


def _cmds(lines: list[str]) -> str:
    return "\n".join(f"{_CMD}{l}" for l in lines)


def _vars_lines(host_dir: str, staging: str) -> list[str]:
    return ["# Where the relay lives. A root-only VPS gives /root/.emporia, which is fine.",
            f"D=\"{host_dir}\"",
            "# Where the wizard staged its files, inside the Hermes folder.",
            f"S=\"{staging.replace('${HOME}', '$HOME')}\"",
            _DIR_CHECK]


def _vars_step(host_dir: str, staging: str) -> str:
    return ("Fix the folders once, in your own bash shell (as your normal user, not inside sudo -i). Every later "
            "command uses them, already expanded before sudo runs, so sudo's own HOME never matters:\n"
            + _cmds(_vars_lines(host_dir, staging)) + f"\n{_PROSE}{SESSION_NOTE}")


def _subshell(body: list[str], extra: str = "") -> list[str]:
    return ["(", "set -euo pipefail", ': "${D:?}" "${S:?}"' + extra, *body, ")"]


def into_compose_steps(front_door: str, host_dir: str, domain: str, needs_key: bool, secrets: list[str],
                       staging: str = STAGING_DEFAULT, snippet_name: str = SNIPPET) -> list[str]:
    """What the person does by hand, in order, with the exact commands. `host_dir` is the install
    folder and `staging` the folder the wizard wrote into, as the host's shell spells them; the
    commands use "$D" and "$S", guarded so an empty one stops the command instead of aiming it at /.
    The relay's uid owns what only it may read; the tunnel's owns the token. `needs_key`: escrow is on
    and has no operator key yet; money alone (x402, paid directly) needs none."""
    tunnel = front_door == "tunnel"
    door = "emporia-cloudflared" if tunnel else "emporia-caddy"
    steps = [_vars_step(host_dir, staging), _install_step(front_door, snippet_name)]
    if tunnel:
        steps.append(
            f"Cloudflare: Zero Trust > Networks > Tunnels. Create a tunnel, or open an existing one (Configure shows "
            f"the token again). Copy only the token, the long string that starts eyJ, not the whole command. "
            f"Public hostname {domain} -> http://emporia-relay:8088. The domain's DNS must be on Cloudflare, proxied "
            f"(orange cloud). Turn WebSockets on.")
        steps.append(
            "Save the token on the host. Run the two lines, paste only the token, press Enter, then Ctrl-D. The "
            "token is a secret: never share it. It goes straight into the install folder, never into the Hermes "
            "folder.\n" + _cmds([
                f"sudo install -d -o {CLOUDFLARED_UID} -g {CLOUDFLARED_UID} -m 700 \"${{D:?}}/cloudflared\"",
                f"sudo install -m 400 -o {CLOUDFLARED_UID} -g {CLOUDFLARED_UID} /dev/stdin \"${{D:?}}/cloudflared/token\""]))
    else:
        steps.append(f"DNS: an A record for {domain} to this VPS. Firewall: allow 22, 80 and 443 only\n"
                     f"        sudo ufw default deny incoming && sudo ufw allow 22,80,443/tcp && sudo ufw enable")
    steps.append(_include_step())
    if secrets or needs_key:
        steps.append("Give the relay's user its secrets, and nobody else (one file per secret in the install "
                     "folder's secrets/):\n" + _cmds([
                         f"sudo chown -R {RELAY_UID}:{RELAY_UID} \"${{D:?}}/secrets\"",
                         "sudo chmod 700 \"${D:?}/secrets\"",
                         "sudo find \"${D:?}/secrets\" -type f -exec chmod 600 {} +"]))
    if needs_key:
        steps.append("Make the escrow operator key inside the relay's data folder, then put the address it prints in the "
                     "relay's .env (\"$D/.env\") as EMPORIA_OPERATOR_ADDRESS=0x...:\n"
                     "        sudo docker compose run --rm --no-deps emporia-relay python -m emporia.operator_signer generate")
    steps.append("Start it, from the folder of your Hermes compose file (the first build pulls the pinned commit "
                 "from GitHub):\n"
                 f"        sudo docker compose up -d --build emporia-relay {door}\n"
                 f"{_PROSE}{COMPOSE_HOME_NOTE}")
    steps.append(f"Check: curl -s https://{domain}/health   (status ok). Then move your agents to it: "
                 f"emporia relay use https://{domain} NAME ...")
    steps.append(_tail_step(front_door))
    steps.append(_settings_step())
    return steps


def _tail_step(front_door: str) -> str:
    if front_door == "tunnel":
        return ("Optional, in Cloudflare: Security > WAF > Rate limiting rules (docs/deploy-relay.md). Keep Bot "
                "Fight Mode, Browser Integrity Check and \"I'm Under Attack\" off for this host: every client "
                "is an agent, and they would lock all of them out.")
    return ("Behind nginx or Traefik instead of Caddy: set EMPORIA_CLIENT_IP_HEADER to the header your proxy "
            "sets, and EMPORIA_TRUSTED_PROXIES to its address, in the relay's .env.")


def _include_step() -> str:
    return ("In your Hermes compose file, add the snippet under include: with its absolute path. This prints the "
            f"line to add:\n        echo \"include: [ ${{D:?}}/{SNIPPET} ]\"\n"
            f"{_PROSE}Do not copy the services, volumes or networks over.")


def _settings_step() -> str:
    return ("Change a setting later, only this way: edit the relay's .env in the install folder, on the host, with "
            "sudo, then restart the relay from the folder of your Hermes compose file. (In a new terminal, run the "
            "folder lines from step 1 first.) The staged copies are gone by then, so there is nothing else to "
            "re-install:\n" + _cmds(['sudo nano "${D:?}/.env"', "sudo docker compose up -d emporia-relay"]))


def _install_step(front_door: str, snippet_name: str) -> str:
    """Copy what the wizard staged into the install folder with owners and modes, check each file
    arrived, and only then remove the staged copies. One subshell: it stops at the first failure."""
    body = [
        'sudo install -d -o root -g root -m 755 "$D"',
        f'sudo sed "s#{DIR_PLACEHOLDER}#$D#g" "$S/{snippet_name}" | sudo tee "$D/{SNIPPET}" >/dev/null',
        f'sudo chmod 644 "$D/{SNIPPET}"',
        'sudo grep -qF -- "- $D/data:/data" "$D/' + SNIPPET + '" || { echo "STOP: the snippet did not install"; exit 1; }',
        f'sudo install -o root -g {RELAY_UID} -m 640 "$S/.env" "$D/.env"',
        f'sudo install -d -o {RELAY_UID} -g {RELAY_UID} -m 700 "$D/secrets" "$D/data"',
        'sudo cp -a "$S/secrets/." "$D/secrets/"',
        f'sudo chown -R {RELAY_UID}:{RELAY_UID} "$D/secrets"',
        'sudo find "$D/secrets" -type f -exec chmod 600 {} +',
    ]
    staged = ['"$S/.env"', f'"$S/{snippet_name}"', '"$S/secrets"']
    if front_door != "tunnel":
        body.append('sudo install -o root -g root -m 644 "$S/Caddyfile" "$D/Caddyfile"')
        body.append('sudo test -s "$D/Caddyfile" || { echo "STOP: the Caddyfile did not install"; exit 1; }')
        staged.append('"$S/Caddyfile"')
    body.append('sudo test -s "$D/.env" || { echo "STOP: .env did not install"; exit 1; }')
    body.append('echo "installed and checked; removing the staged copies"')
    body.append(f"sudo rm -rf -- {' '.join(staged)}")
    body.append('echo "STEP DONE"')
    return ("Install what the wizard staged into the install folder, outside the Hermes folder, with the right "
            "owners and modes. It stops at the first failure, and removes the staged copies only after every file "
            "is checked (Hermes can read and edit its own folder, so it must not hold the relay's files):\n"
            + _cmds(_subshell(body)))


ID_CMD = ("import json,urllib.request as u; h=json.load(u.urlopen(\"http://127.0.0.1:8088/health\",timeout=5)); "
          "print(h.get(\"relay_id\"), h.get(\"relay_public_key\"))")


def move_steps(front_door: str, host_dir: str, domain: str, staging: str, snippet_name: str,
               relay: str = "emporia-relay", door_container: str = "", pin: tuple[str, str] = ("", "")) -> list[str]:
    """Moving an existing relay (old layout: its folder inside the Hermes data folder, its database in
    the `emporia-data` volume) to the install folder, in stages the person pastes one at a time. Keeps
    its .env, secrets, tunnel token and database, so the relay keeps its identity and key. Nothing
    here prints a secret. `relay`/`door_container`: the container names. `pin`: (old, new) commit."""
    tunnel = front_door == "tunnel"
    door = "emporia-cloudflared" if tunnel else "emporia-caddy"
    door_container = door_container or door
    old_pin, new_pin = pin
    last = ('sudo test -f "${S:?}/cloudflared/token" && echo "staging ok" || echo "STOP: staged snippet, .env or '
            'token missing in the staging folder"' if tunnel else
            'sudo test -f "${S:?}/Caddyfile" && echo "staging ok" || echo "STOP: staged snippet, .env or '
            'Caddyfile missing in the staging folder"')
    pin_line = (f"The staged snippet builds the relay from the current release commit {new_pin[:12]}"
                + (f" (the old relay was pinned to {old_pin[:12]})" if old_pin and old_pin != new_pin else "")
                + ". Stage 4 builds that image once on the host, so the host must reach GitHub then. The identity "
                  "and database come from the old volume, not from the image, so the relay stays the same one."
                if new_pin else "")
    stage0 = ("Stage 0: folders and checks. Run it again in any new terminal. Before this, in the Hermes container, "
              "emporia relay host --move-out --dir <the relay's folder> staged the new snippet there.\n"
              + _cmds(["# ---- Stage 0: folders ----",
                       *_vars_lines(host_dir, staging),
                       'sudo test -s "${S:?}/' + snippet_name + '" && sudo test -f "${S:?}/.env" && ' + last])
              + f"\n{_PROSE}{SESSION_NOTE}" + (f"\n{_PROSE}{pin_line}" if pin_line else ""))
    stage1 = ("Stage 1: who the relay is, and how it is wired (the relay is still running). Write down the before: "
              "line, and project=, dir= and files=; stage 3 needs them.\n"
              + _cmds(["# ---- Stage 1 ----",
                       "V=\"$(sudo docker inspect -f '{{range .Mounts}}{{if eq .Destination \"/data\"}}{{.Name}}{{end}}{{end}}' "
                       f"{relay})\"",
                       'echo "old volume: ${V:?STOP: ' + relay + ' has no named volume at /data}"',
                       "sudo docker inspect -f 'project={{index .Config.Labels \"com.docker.compose.project\"}}  "
                       "dir={{index .Config.Labels \"com.docker.compose.project.working_dir\"}}  "
                       "files={{index .Config.Labels \"com.docker.compose.project.config_files\"}}' " + relay,
                       f"ID_CMD='{ID_CMD}'",
                       f'BEFORE="$(sudo docker exec {relay} python -c "$ID_CMD")"',
                       'echo "before: ${BEFORE:?STOP: could not read the relay id}"']))
    keep = [
        'sudo install -d -o root -g root -m 755 "$D"',
        f'sudo install -d -o {RELAY_UID} -g {RELAY_UID} -m 700 "$D/secrets" "$D/data"',
        '[ -z "$(sudo ls -A "$D/data")" ] || { echo "STOP: $D/data is not empty"; exit 1; }',
        f'sudo sed "s#{DIR_PLACEHOLDER}#$D#g" "$S/{snippet_name}" | sudo tee "$D/{SNIPPET}" >/dev/null',
        f'sudo chmod 644 "$D/{SNIPPET}"',
        'sudo grep -qF -- "- $D/data:/data" "$D/' + SNIPPET + '" || { echo "STOP: the snippet did not install"; exit 1; }',
        "if sudo grep -qF -e '" + DIR_PLACEHOLDER + "' -e '${HOME}' \"$D/" + SNIPPET + "\"; then "
        "echo \"STOP: placeholder left in the snippet\"; exit 1; fi",
        f'sudo install -o root -g {RELAY_UID} -m 640 "$S/.env" "$D/.env"',
        'sudo cp -a "$S/secrets/." "$D/secrets/"',
        f'sudo chown -R {RELAY_UID}:{RELAY_UID} "$D/secrets"',
        'sudo chmod 700 "$D/secrets"',
        'sudo find "$D/secrets" -type f -exec chmod 600 {} +',
    ]
    if tunnel:
        keep += [f'sudo install -d -o {CLOUDFLARED_UID} -g {CLOUDFLARED_UID} -m 700 "$D/cloudflared"',
                 f'sudo install -o {CLOUDFLARED_UID} -g {CLOUDFLARED_UID} -m 400 "$S/cloudflared/token" "$D/cloudflared/token"']
    else:
        keep += ['sudo install -o root -g root -m 644 "$S/Caddyfile" "$D/Caddyfile"']
    keep += [
        "sudo docker run --rm -v \"$V\":/from:ro -v \"$D\":/to alpine sh -c "
        "'umask 077; tar czf /to/emporia-data-before-move.tgz -C /from .'",
        "sudo docker run --rm -v \"$V\":/from:ro -v \"$D/data\":/to alpine sh -c "
        "'cp -a /from/. /to/ && diff -r /from /to && echo COPY-VERIFIED'",
        f'sudo chown -R {RELAY_UID}:{RELAY_UID} "$D/data"',
        'sudo chmod 700 "$D/data"',
        'sudo test -s "$D/data/emporia.sqlite3" || echo "WARNING: no $D/data/emporia.sqlite3 (fine only if the .env '
        'names another database path)"',
        'echo "STAGE 2 DONE: files and database are in $D; the old volume $V is untouched"',
    ]
    stop = [f"sudo docker stop {door_container} {relay}",
            f"[ \"$(sudo docker inspect -f '{{{{.State.Running}}}}' {relay})\" = false ] || "
            "{ echo \"STOP: the relay is still running\"; exit 1; }"]
    stage2 = ("Stage 2: stop, back up, copy, verify. It stops at the first failure.\n"
              + _cmds(["# ---- Stage 2 ----", *_subshell([*stop, *keep], ' "${V:?}"')])
              + f"\n{_PROSE}Go on only after COPY-VERIFIED and STAGE 2 DONE. If it stopped part-way, the relay is "
                f"stopped and nothing is lost: sudo docker start {relay} {door_container} brings the old one back. "
                f"The backup $D/emporia-data-before-move.tgz (root, 600) holds the identity key: keep it.")
    stage3 = ("Stage 3: point the Hermes compose file at the new snippet, by hand. Edit it (in the dir= folder from "
              "stage 1) according to project=, dir= and files= from stage 1:\n"
              + _cmds(["# ---- Stage 3 ----", 'echo "include: [ ${D:?}/' + SNIPPET + ' ]"',
                       "sudo grep -c '^EMPORIA_ESCROW_NETWORK=' \"${D:?}/.env\" || true"])
              + f"\n{_PROSE}- It has an include: naming the old snippet: replace that entry with the line printed above.\n"
                f"{_PROSE}- The relay services were pasted into it: delete the emporia-relay and {door} services, "
                f"the emporia-data volume and the emporia-edge network from it, then add the include line.\n"
                f"{_PROSE}- files= listed the old snippet (started with two -f): add the include line, or keep -f "
                f"and name \"$D/{SNIPPET}\" as the second file in stage 4.\n"
                f"{_PROSE}- project= is not the Hermes project: first run sudo docker rm {door_container} {relay} and "
                f"sudo docker network rm PROJECT_emporia-edge (the project name from project=). The volume stays.\n"
                f"{_PROSE}The grep -c prints a count. It should be 0 (escrow stays off for this release); if it is 1, "
                f"comment that line out in \"$D/.env\" with sudo nano before stage 4.")
    stage4 = ("Stage 4: start and compare. Run it from the Hermes compose folder (dir= from stage 1), in the same "
              "terminal as stage 1 (BEFORE and ID_CMD must still be set).\n"
              + _cmds(["# ---- Stage 4 ----",
                       f"sudo HOME=\"$HOME\" docker compose up -d emporia-relay {door}",
                       f'AFTER="$(sudo docker exec {relay} python -c "$ID_CMD")"',
                       'echo "before: $BEFORE"; echo "after:  $AFTER"',
                       '[ -n "$BEFORE" ] && [ "$BEFORE" = "$AFTER" ] && echo "SAME RELAY" || echo "NOT THE SAME '
                       f'(or unreadable): sudo docker stop {door_container} {relay}, and remove nothing"',
                       f"sudo docker inspect -f '{{{{range .Mounts}}}}{{{{.Source}}}} -> {{{{.Destination}}}}  {{{{end}}}}' {relay}",
                       f"curl -s https://{domain}/health"])
              + f"\n{_PROSE}Expect SAME RELAY, mounts that all start with the install folder, and \"status\":\"ok\" "
                f"from the public address. Both the relay_id and the public key are compared; a matching "
                f"relay_url alone proves nothing, so it is not used. To go back before stage 5: undo the compose "
                f"file edit and run sudo docker compose up -d again; the old volume and the old files are still there.")
    gone = (['"${S:?}/.env"', '"${S:?}/secrets"', '"${S:?}/cloudflared"'] if tunnel
            else ['"${S:?}/.env"', '"${S:?}/secrets"', '"${S:?}/Caddyfile"'])
    gone.append('"${S:?}/' + SNIPPET + '"')
    if snippet_name != SNIPPET:
        gone.append('"${S:?}/' + snippet_name + '"')
    stage5 = ("Stage 5: only after SAME RELAY (a day later is fine). Removes the old volume and the old files.\n"
              + _cmds(["# ---- Stage 5 ----", 'sudo docker volume rm "${V:?}"', "sudo rm -rf -- " + " ".join(gone)])
              + f"\n{_PROSE}In a new terminal V is gone, and stage 1 would now find no named volume: set V by hand to "
                f"the old volume name that stage 1 printed. After the move, change settings only by editing "
                f"\"$D/.env\" on the host with sudo, then restart the relay.")
    return [stage0, stage1, stage2, stage3, stage4, stage5]


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
