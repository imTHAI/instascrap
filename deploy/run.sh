#!/bin/bash
# Unraid User Script. Scheduled three times a day (cron: 0 8,14,21 * * *); the
# random delay below is what turns those three fixed cron entries into
# irregular, human-looking session times instead of hitting Instagram at the
# same second every day.
#
# This script, not the container, is what raises Unraid notifications. The
# notify script is PHP, needs the whole /usr/local/emhttp/webGui/include tree and
# a PHP interpreter, and none of that exists inside the Playwright image —
# mounting the script alone would not help. So the container reports its outcome
# through its exit code (0 ok, 1 failed, 2 blocked) plus $DATA/last_run.json, and
# the branching below turns that into a notification the user actually receives.
set -euo pipefail

DATA=/mnt/user/appdata/instascrap
PUBLISH=/mnt/user/appdata/caddy/site/instacrap
REPORT="$DATA/last_run.json"
LOCK="$DATA/session.lock"
NOTIFY=/usr/local/emhttp/webGui/scripts/notify

notify_unraid() {  # summary, description, importance (normal|warning|alert)
  echo "instascrap: $1 — $2"
  if [ -x "$NOTIFY" ]; then
    "$NOTIFY" -e instascrap -s "$1" -d "$2" -i "$3" || true
  fi
}

# docker's bind mount silently creates an empty directory at the source path
# when it doesn't exist yet -- for config.toml that would mount an empty
# directory instead of the file and fail deep inside tomllib with a confusing
# traceback. Checked before the jitter sleep so a misconfiguration fails
# immediately instead of after a wasted wait of up to 30 minutes.
if [ ! -f "$DATA/config.toml" ]; then
  notify_unraid "instascrap mal configuré" \
    "$DATA/config.toml manquant -- voir config.example.toml dans le dépôt" alert
  exit 1
fi

sleep $((RANDOM % 1800))

# Two Chromes on one persistent profile directory corrupt or lock it, which costs
# the Instagram session and forces a re-login -- and repeated logins are what get
# an account flagged. Reachable in practice: the jitter above spans 30 minutes, a
# hung session can outlive its slot, and login.sh gets run by hand. The lock is
# taken after the sleep on purpose, so a jittered run never holds it during a
# half-hour wait and blocks an interactive login. login.sh takes the same lock.
exec 9>"$LOCK"
if ! flock -n 9; then
  # Not benign: it means a previous session never finished. Left silent, a single
  # hung run would make every future run skip, forever, with nothing to show.
  notify_unraid "instascrap déjà en cours" \
    "Une autre session tient $LOCK -- session ignorée. Vérifie qu'un conteneur n'est pas bloqué." warning
  exit 0
fi

# Removed before the run, never after: a stale report from a previous session
# read back as this one's outcome would be worse than no report at all.
rm -f "$REPORT"

# --shm-size=1g is not decorative: Chrome dies with SIGBUS on Docker's default
# 64 MB /dev/shm.
set +e
docker run --rm \
  -v "$DATA:/data" \
  -v "$PUBLISH:/publish" \
  -v "$DATA/config.toml:/app/config.toml:ro" \
  --shm-size=1g \
  instascrap:latest --run
STATUS=$?
set -e

# last_run.json is written with one field per line (json.dumps indent=2,
# sort_keys=True) and every string field is flattened of quotes and newlines on
# the Python side, which is what makes this sed extraction safe. See _flat() and
# _write_last_run() in src/instascrap/main.py -- the two must stay in step.
field() { [ -f "$REPORT" ] && sed -n "s/^[[:space:]]*\"$1\": *\"\([^\"]*\)\".*/\1/p" "$REPORT" | head -n 1; }
number() { [ -f "$REPORT" ] && sed -n "s/^[[:space:]]*\"$1\": *\([0-9][0-9]*\).*/\1/p" "$REPORT" | head -n 1; }
flag() { [ -f "$REPORT" ] && grep -q "\"$1\": true" "$REPORT"; }

if [ ! -f "$REPORT" ]; then
  # The container writes this on every path, exception included. Its absence
  # means the process died before Python got that far -- OOM kill, image
  # missing, Chrome unable to launch -- which is exactly the class of failure
  # that used to vanish into a cron redirect.
  notify_unraid "instascrap muet" \
    "Le conteneur s'est terminé (code $STATUS) sans écrire de rapport. Voir les logs docker." alert
  exit "$STATUS"
fi

REASON=$(field reason)
ERROR=$(field error)
VISITED=$(number visited)
NEW_POSTS=$(number new_posts)
FAILURES=$(number failures)
EMPTY=$(number empty_visits)
SILENT_DAYS=$(number silent_days)

case "$STATUS" in
  2)
    notify_unraid "instascrap bloqué" \
      "${REASON:-blocage détecté} -- pause de sécurité, aucune nouvelle session avant la fin du backoff." alert
    ;;
  0)
    # A session with no new posts is the common case and notifies nothing. Only
    # the signals that mean something is broken get through.
    if flag stale; then
      notify_unraid "instascrap silencieux" \
        "Aucun post nouveau depuis ${SILENT_DAYS:-?} jours, tous comptes confondus -- parser cassé ?" alert
    fi
    if [ "${EMPTY:-0}" != "0" ]; then
      notify_unraid "instascrap sans réponse" \
        "${EMPTY} profil(s) visité(s) sans aucun JSON capturé -- interception cassée ?" warning
    fi
    if [ "${FAILURES:-0}" != "0" ]; then
      notify_unraid "instascrap partiellement en échec" \
        "${FAILURES} échec(s) isolé(s) sur ${VISITED:-0} compte(s), ${NEW_POSTS:-0} post(s) nouveaux. Premier : $(field first_failure)" warning
    fi
    ;;
  *)
    notify_unraid "instascrap en échec" \
      "${ERROR:-échec sans détail} (code $STATUS)" alert
    ;;
esac

exit "$STATUS"
