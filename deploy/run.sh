#!/bin/bash
# Unraid User Script. Scheduled three times a day (cron: 0 8,14,21 * * *); the
# random delay below is what turns those three fixed cron entries into
# irregular, human-looking session times instead of hitting Instagram at the
# same second every day.
set -euo pipefail

DATA=/mnt/user/appdata/instascrap
PUBLISH=/mnt/user/appdata/caddy/site/instacrap

# docker's bind mount silently creates an empty directory at the source path
# when it doesn't exist yet -- for config.toml that would mount an empty
# directory instead of the file and fail deep inside tomllib with a confusing
# traceback. Checked before the jitter sleep so a misconfiguration fails
# immediately instead of after a wasted wait of up to 30 minutes.
if [ ! -f "$DATA/config.toml" ]; then
  echo "missing $DATA/config.toml -- see config.example.toml in the repo" >&2
  exit 1
fi

sleep $((RANDOM % 1800))

# --shm-size=1g is not decorative: Chrome dies with SIGBUS on Docker's default
# 64 MB /dev/shm.
docker run --rm \
  -v "$DATA:/data" \
  -v "$PUBLISH:/publish" \
  -v "$DATA/config.toml:/app/config.toml:ro" \
  --shm-size=1g \
  instascrap:latest --run
