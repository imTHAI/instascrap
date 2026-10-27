#!/bin/bash
# Runs as root only long enough to hand /data to PUID:PGID, then drops to that
# user for good. Unraid's convention is nobody:users (99:100); files written as
# root would otherwise be unmanageable from the appdata share.
set -euo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"

# Only when ownership is off: on an archive of tens of thousands of media files a
# recursive chown on every start would be slow for nothing. Checking the top
# directory catches the one real case, data written by an older root-run setup.
if [ "$(stat -c '%u:%g' /data)" != "$PUID:$PGID" ]; then
  echo "entrypoint: giving /data to $PUID:$PGID (one-time, may take a while)"
  chown -R "$PUID:$PGID" /data
fi
# Created with its owner in one step, after the check above: a plain mkdir as
# root would leave it root-owned whenever /data already belongs to PUID, and
# Chrome then cannot write its crash reports or certificate store there.
install -d -o "$PUID" -g "$PGID" /data/home

if [ "$PUID" = "0" ]; then
  exec python3 -m instascrap "$@"
fi
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups python3 -m instascrap "$@"
