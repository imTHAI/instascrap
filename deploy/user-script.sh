#!/bin/bash
# Entry point installed as an Unraid User Script
# (/boot/config/plugins/user.scripts/scripts/instascrap/script), scheduled
# 0 8,14,21 * * *. run.sh adds its own random delay of up to 30 minutes, which is
# what keeps those three fixed slots from hitting Instagram at the same second
# every day.
#
# Deliberately a wrapper rather than a copy of run.sh: run.sh is rsync'd from the
# repo to appdata (see docs/DEPLOYMENT.md) and has to stay in step with the image
# it launches. A second copy living under /boot would drift out of that
# pairing without anything noticing.
set -euo pipefail

RUN=/mnt/user/appdata/instascrap-src/deploy/run.sh
NOTIFY=/usr/local/emhttp/webGui/scripts/notify

# Reachable in practice: the array not being started yet at 08:00 leaves
# /mnt/user unmounted. Silent failure here would mean no session and no trace of
# why, so it gets the same notification treatment as any other run.sh failure.
if [ ! -x "$RUN" ]; then
  echo "instascrap: $RUN introuvable ou non exécutable"
  if [ -x "$NOTIFY" ]; then
    "$NOTIFY" -e instascrap -s "instascrap introuvable" \
      -d "$RUN absent ou non exécutable -- source non copiée, ou tableau non démarré." \
      -i alert || true
  fi
  exit 1
fi

exec bash "$RUN"
