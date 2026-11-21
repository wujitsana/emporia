#!/bin/sh
# Update an Emporia relay that runs in Docker to a newer release commit, keeping its identity and history.
# POSIX sh; needs only Docker, curl (or wget) and the relay's compose files. Run it from the TARGET release, fetched at
# the commit you are moving to (a full 40-character sha, written out twice):
#
#   SHA=<full 40-character commit>
#   curl -fsSLO https://raw.githubusercontent.com/wujitsana/emporia/$SHA/installer/relay-update.sh
#   sh relay-update.sh --check $SHA        # prints every step and command; changes nothing
#   sh relay-update.sh $SHA                # does it (add --sudo if your Docker needs sudo, --yes to skip the schema question)
#
# Run it in the folder of the compose file the relay runs from (add -f FILE once per extra compose file).
# Steps: the commit is checked -> same-data check -> pin + build (the old relay keeps serving) -> database upgrade dry run
# with the new code -> stop -> full backup -> upgrade (y/N, only when the schema changes) -> start -> same-relay check.
# Stops at the first problem and deletes nothing. Settings are not rewritten, except one existing EMPORIA_BUILD_COMMIT line.
set -eu

REPO="${EMPORIA_UPDATE_REPO:-wujitsana/emporia}"
RAW="${EMPORIA_UPDATE_RAW:-https://raw.githubusercontent.com}"
API="${EMPORIA_UPDATE_API:-https://api.github.com}"

DIR="$HOME/.emporia"
SERVICE="emporia-relay"
CONTAINER="emporia-relay"
BACKUPS="$HOME/emporia-backups"
PORT=8088
CHECK=0
YES=0
SUDO=""
FILES=""
NEW=""

usage() {
  cat <<USAGE
usage: sh relay-update.sh [options] <full 40-character commit sha>
  --check            print every step and command, change nothing
  --yes              answer the database-upgrade question with yes (for automation)
  --sudo             run docker and file changes with sudo
  --dir FOLDER       the relay's install folder (default \$HOME/.emporia): compose.snippet.yaml and .env
  --service NAME     the compose service (default emporia-relay)
  --container NAME   the running container (default emporia-relay)
  --backup-dir DIR   where the data backup goes (default \$HOME/emporia-backups)
  -f FILE            a compose file to pass to docker compose (repeat for several)
USAGE
}

die() { echo "STOP: $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --yes) YES=1 ;;
    --sudo) SUDO="sudo" ;;
    --dir) [ $# -ge 2 ] || die "--dir needs a folder"; DIR="$2"; shift ;;
    --service) [ $# -ge 2 ] || die "--service needs a name"; SERVICE="$2"; shift ;;
    --container) [ $# -ge 2 ] || die "--container needs a name"; CONTAINER="$2"; shift ;;
    --backup-dir) [ $# -ge 2 ] || die "--backup-dir needs a folder"; BACKUPS="$2"; shift ;;
    -f) [ $# -ge 2 ] || die "-f needs a file"; FILES="$FILES -f $2"; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) usage >&2; die "unknown option $1" ;;
    *) [ -z "$NEW" ] || die "one commit only"; NEW="$1" ;;
  esac
  shift
done

# 1. the commit: full sha only
case "$NEW" in
  "") usage >&2; exit 1 ;;
esac
if ! printf '%s' "$NEW" | grep -Eq '^[0-9a-f]{40}$'; then
  usage >&2
  die "a full 40-character commit sha is needed (Docker cannot build from a short one); got '$NEW'"
fi
[ "$(id -u)" != 0 ] || [ -n "$SUDO" ] || echo "note: running as root"

SNIPPET="$DIR/compose.snippet.yaml"
ENVF="$DIR/.env"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
COMPOSE="$SUDO env HOME=$HOME docker compose$FILES"
ID_CMD='import json,urllib.request as u; h=json.load(u.urlopen("http://127.0.0.1:'"$PORT"'/health",timeout=5)); print(h.get("relay_id"), h.get("relay_public_key"))'
BUILD_CMD='import json,urllib.request as u; print(json.load(u.urlopen("http://127.0.0.1:'"$PORT"'/health",timeout=5)).get("build"))'
DATA_CMD='import os; s=os.stat("/data"); print(s.st_ino, sorted(os.listdir("/data")))'

step() { echo; echo "== $*"; }
# Print what a changing step would run; run it unless --check.
do_run() {
  if [ "$CHECK" = 1 ]; then echo "   would run: $*"; return 0; fi
  "$@"
}

fetch() {  # fetch URL [Accept header]
  if command -v curl >/dev/null 2>&1; then
    if [ -n "${2:-}" ]; then curl -fsSL -H "Accept: $2" "$1"; else curl -fsSL "$1"; fi
  elif command -v wget >/dev/null 2>&1; then
    if [ -n "${2:-}" ]; then wget -qO- --header="Accept: $2" "$1"; else wget -qO- "$1"; fi
  else
    return 127
  fi
}

rollback() {
  echo
  echo "Rollback, if you need it (nothing was deleted):"
  echo "  1. $COMPOSE stop $SERVICE"
  if [ -n "${OLD:-}" ] && [ -f "$SNIPPET.bak-$STAMP" ]; then
    echo "  2. $SUDO cp -a $SNIPPET.bak-$STAMP $SNIPPET     (the old pin, $OLD)"
  else
    echo "  2. (the snippet backup would be $SNIPPET.bak-$STAMP; none was made yet)"
  fi
  [ ! -f "$ENVF.bak-$STAMP" ] || echo "     $SUDO cp -a $ENVF.bak-$STAMP $ENVF"
  echo "  3. only if the database upgrade ran: move the data aside and untar the backup into it:"
  echo "       $SUDO docker run --rm -v ${VOL:-<data volume or folder>}:/data -v $BACKUPS:/b alpine sh -c 'mkdir /data/_failed-$STAMP && find /data -mindepth 1 -maxdepth 1 ! -name _failed-$STAMP -exec mv {} /data/_failed-$STAMP/ \; && tar xzf /b/emporia-data-$STAMP.tgz -C /data'"
  echo "  4. $COMPOSE build --build-arg EMPORIA_BUILD_COMMIT=${OLD:-<old sha>} $SERVICE && $COMPOSE up -d --force-recreate $SERVICE"
}

# 2. this script is the commit it was fetched at; the commit is on GitHub
step "1. the commit $NEW"
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
  die "curl or wget is needed to check the commit on GitHub"
fi
GOT="$(fetch "$API/repos/$REPO/commits/$NEW" "application/vnd.github.sha" 2>/dev/null || true)"
[ "$GOT" = "$NEW" ] || die "GitHub does not know commit $NEW in $REPO (is it pushed? is it the full sha?)"
echo "   on GitHub: yes"
if [ -r "$0" ] && REMOTE="$(fetch "$RAW/$REPO/$NEW/installer/relay-update.sh" 2>/dev/null)"; then
  if [ "$REMOTE" = "$(cat "$0")" ]; then
    echo "   this script is installer/relay-update.sh at that commit: yes"
  else
    die "this script is not the installer/relay-update.sh at commit $NEW; fetch it again from the URL in its header"
  fi
else
  echo "   this script could not be compared with the one at that commit (run from a pipe, or no network to raw): continuing"
fi

step "2. the relay as it is"
if [ "$CHECK" = 1 ]; then
  echo "   would run: $SUDO docker exec $CONTAINER python -c <identity from /health>   (relay_id, relay_public_key)"
  echo "   would run: $SUDO docker inspect $CONTAINER   (where /data lives)"
  echo "   would run: $COMPOSE run --rm --no-deps $SERVICE python -c <stat /data>   (must match the live container's)"
else
  BEFORE="$($SUDO docker exec "$CONTAINER" python -c "$ID_CMD")" || die "the relay container $CONTAINER is not answering on /health"
  echo "   identity: $BEFORE"
  VOL="$($SUDO docker inspect "$CONTAINER" --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}}{{end}}{{end}}')"
  [ -n "$VOL" ] || die "could not find the relay's /data"
  echo "   data at: $VOL"
  LIVE_DATA="$($SUDO docker exec "$CONTAINER" python -c "$DATA_CMD")"
  COMPOSE_DATA="$($COMPOSE run --rm --no-deps "$SERVICE" python -c "$DATA_CMD")"
  [ "$LIVE_DATA" = "$COMPOSE_DATA" ] || die "compose's $SERVICE does not see the live relay's /data (nothing changed)"
  echo "   same /data as compose's $SERVICE: yes"
fi

step "3. pin the commit and build (the relay keeps serving)"
[ -f "$SNIPPET" ] || die "no $SNIPPET: this script updates a relay whose image is built from a pinned git commit (emporia relay host --into-compose). Use --dir for another install folder"
OLD="$($SUDO grep -o 'emporia.git#[0-9a-f]\{7,40\}' "$SNIPPET" | head -1 | cut -d'#' -f2)"
[ -n "$OLD" ] || die "no pinned commit (emporia.git#<sha>) in $SNIPPET"
if [ "$OLD" = "$NEW" ]; then echo "   already pinned to $NEW. Nothing to do."; exit 0; fi
echo "   pinned now: $OLD"
if [ "$CHECK" = 1 ]; then
  echo "   would run: $SUDO cp -a $SNIPPET $SNIPPET.bak-$STAMP"
  echo "   would run: $SUDO sed -i s/emporia.git#$OLD/emporia.git#$NEW/ and emporia-relay:<12> in $SNIPPET"
  echo "   would run: $COMPOSE build --build-arg EMPORIA_BUILD_COMMIT=$NEW $SERVICE"
else
  $SUDO cp -a "$SNIPPET" "$SNIPPET.bak-$STAMP"
  restore_pin() { $SUDO cp -a "$SNIPPET.bak-$STAMP" "$SNIPPET"; echo "   (pin restored to $OLD; the relay was not stopped)"; }
  OLD12="$(printf '%s' "$OLD" | cut -c1-12)"; NEW12="$(printf '%s' "$NEW" | cut -c1-12)"
  $SUDO sed -i "s/emporia.git#$OLD/emporia.git#$NEW/g; s/emporia-relay:$OLD12/emporia-relay:$NEW12/g" "$SNIPPET"
  $SUDO grep -q "emporia.git#$NEW" "$SNIPPET" || { restore_pin; die "the pin did not change"; }
  $COMPOSE build --build-arg "EMPORIA_BUILD_COMMIT=$NEW" "$SERVICE" || { restore_pin; die "the build failed"; }
  echo "   built; pinned $NEW"
fi

step "4. does the database need an upgrade? (dry run with the new code; changes nothing)"
UPGRADE_DRY="$COMPOSE run --rm --no-deps $SERVICE emporia relay upgrade --db /data/emporia.sqlite3 --dry-run"
NEED=0
if [ "$CHECK" = 1 ]; then
  echo "   would run: $UPGRADE_DRY"
  echo "   (a 'nothing to do' answer means no schema change and no question is asked)"
else
  RC=0
  PLAN="$($COMPOSE run --rm --no-deps "$SERVICE" emporia relay upgrade --db /data/emporia.sqlite3 --dry-run 2>&1)" || RC=$?
  echo "$PLAN"
  [ "$RC" = 0 ] || { restore_pin; die "the new release refuses this database (see above); the relay was not stopped"; }
  if printf '%s\n' "$PLAN" | grep -q "nothing to do: the database is already at this schema"; then
    echo "   no upgrade needed"
  else
    NEED=1; echo "   this release upgrades the database"
  fi
fi

step "5. stop the relay and take a full backup of its data"
# shellcheck disable=SC2086  # $COMPOSE is a command line and must split into words
do_run $COMPOSE stop "$SERVICE"
if [ "$CHECK" = 1 ]; then
  echo "   would run: mkdir -p $BACKUPS"
  echo "   would run: $SUDO docker run --rm -v <data>:/data:ro -v $BACKUPS:/b alpine tar czf /b/emporia-data-$STAMP.tgz -C /data ."
else
  mkdir -p "$BACKUPS"
  $SUDO docker run --rm -v "$VOL":/data:ro -v "$BACKUPS":/b alpine tar czf "/b/emporia-data-$STAMP.tgz" -C /data . \
    || { echo "STOP: the backup failed; starting the old relay again"; restore_pin; $COMPOSE up -d "$SERVICE"; exit 1; }
  echo "   backup: $BACKUPS/emporia-data-$STAMP.tgz"
fi

step "6. upgrade the database, only if the schema changes"
if [ "$CHECK" = 1 ]; then
  echo "   would ask: Apply the database upgrade shown by the dry run? [y/N]   (only if step 4 shows one; --yes answers it)"
  echo "   would run: $COMPOSE run --rm --no-deps $SERVICE emporia relay upgrade --db /data/emporia.sqlite3 --yes"
elif [ "$NEED" = 1 ]; then
  ok="n"
  if [ "$YES" = 1 ]; then ok="y"; else printf 'Apply the upgrade shown above? [y/N] '; read -r ok || ok="n"; fi
  if [ "$ok" != "y" ]; then
    restore_pin; $COMPOSE up -d "$SERVICE"
    echo "Stopped before changing the database; the old relay ($OLD) is started again."
    rollback
    exit 1
  fi
  if ! $COMPOSE run --rm --no-deps "$SERVICE" emporia relay upgrade --db /data/emporia.sqlite3 --yes; then
    echo "!!! THE DATABASE UPGRADE FAILED. The relay is stopped."
    rollback
    exit 1
  fi
  echo "   upgraded"
else
  echo "   not needed"
fi

step "7. the build commit"
if [ "$CHECK" = 1 ]; then
  echo "   the image carries it (--build-arg EMPORIA_BUILD_COMMIT); a line EMPORIA_BUILD_COMMIT= in $ENVF, if there is one, would be set to $NEW (backup first)"
elif $SUDO test -f "$ENVF" && $SUDO grep -q '^EMPORIA_BUILD_COMMIT=' "$ENVF"; then
  $SUDO cp -a "$ENVF" "$ENVF.bak-$STAMP"
  $SUDO sed -i "s/^EMPORIA_BUILD_COMMIT=.*/EMPORIA_BUILD_COMMIT=$NEW/" "$ENVF"
  $SUDO grep -q "^EMPORIA_BUILD_COMMIT=$NEW\$" "$ENVF" || { echo "STOP: could not set EMPORIA_BUILD_COMMIT in $ENVF (relay still stopped)"; rollback; exit 1; }
  echo "   set EMPORIA_BUILD_COMMIT in $ENVF (backup $ENVF.bak-$STAMP)"
else
  echo "   the image carries it; $ENVF has no EMPORIA_BUILD_COMMIT line and is not changed"
fi

step "8. start the new relay"
# shellcheck disable=SC2086  # $COMPOSE is a command line and must split into words
do_run $COMPOSE up -d --force-recreate "$SERVICE"

step "9. same relay?"
if [ "$CHECK" = 1 ]; then
  echo "   would check: /health shows the same relay_id and relay_public_key as before, and build $(printf '%s' "$NEW" | cut -c1-8)"
  rollback
  echo
  echo "--check: nothing was changed."
  exit 0
fi
AFTER=""
i=0
while [ "$i" -lt 60 ]; do
  AFTER="$($SUDO docker exec "$CONTAINER" python -c "$ID_CMD" 2>/dev/null || true)"
  [ -z "$AFTER" ] || break
  i=$((i + 1)); sleep 3
done
BUILD="$($SUDO docker exec "$CONTAINER" python -c "$BUILD_CMD" 2>/dev/null || true)"
WANT="$(printf '%s' "$NEW" | cut -c1-8)"
echo "   before: $BEFORE"
echo "   after:  $AFTER"
echo "   build:  $BUILD (wanted $WANT)"
if [ "$BEFORE" = "$AFTER" ] && [ "$BUILD" = "$WANT" ]; then
  echo "SAME RELAY, NEW BUILD - DONE. Backup: $BACKUPS/emporia-data-$STAMP.tgz"
  rollback
  exit 0
fi
echo "!!! NOT AS EXPECTED: the relay's identity or its build differs from what was checked. Delete nothing."
rollback
exit 2
