#!/bin/bash
# First login, done by hand: starts a virtual display with noVNC so the Instagram
# session is created by a real, human-driven browser. Every later run is headless
# on the profile this produces.
set -euo pipefail

DATA=/mnt/user/appdata/instascrap

docker run --rm -it \
  -v "$DATA:/data" \
  -v "$DATA/config.toml:/app/config.toml:ro" \
  -p 6080:6080 \
  --shm-size=1g \
  --entrypoint bash \
  instascrap:latest -c '
    Xvfb :99 -screen 0 1440x900x24 &
    x11vnc -display :99 -nopw -forever -shared >/dev/null 2>&1 &
    websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
    sleep 2
    echo "Ouvre http://tower:6080/vnc.html, connecte-toi, puis Entrée ici."
    DISPLAY=:99 python3 -m instascrap --login
  '
