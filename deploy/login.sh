#!/bin/bash
# First login, done by hand: starts a virtual display with noVNC so the Instagram
# session is created by a real, human-driven browser. Every later run is headless
# on the profile this produces.
set -euo pipefail

DATA=/mnt/user/appdata/instascrap
LOCK="$DATA/session.lock"

# Same lock run.sh takes. Two Chromes on one persistent profile directory corrupt
# or lock it, which costs the Instagram session -- and a forced re-login is
# exactly the repeated-login pattern that gets an account flagged. A scheduled
# run can start at any point inside its 30-minute jitter window, so this
# collision is a matter of timing, not of carelessness.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "une session instascrap tourne déjà (verrou $LOCK) -- attends qu'elle finisse" >&2
  exit 1
fi

docker run --rm -it \
  -v "$DATA:/data" \
  -v "$DATA/config.toml:/app/config.toml:ro" \
  -p 6080:6080 \
  --shm-size=1g \
  --entrypoint bash \
  instascrap:latest -c '
    set -euo pipefail

    # Xvfb, x11vnc and websockify each depend on the one before it being ready
    # (X socket up, then VNC port accepting, then noVNC has something to proxy
    # to). A flat sleep races that chain silently; this polls each precondition
    # with a bounded timeout so a startup failure is a loud, specific error
    # instead of a blank VNC screen with no clue why. This is the only path
    # that creates the production Instagram session, so a mute failure here
    # is expensive: it invites a retry, and repeated login attempts are what
    # gets an account flagged.
    wait_for() {
      local desc="$1" check="$2" timeout_s="$3" tries=$(( $3 * 5 )) i=0
      until eval "$check"; do
        i=$((i + 1))
        if [ "$i" -ge "$tries" ]; then
          echo "timed out after ${timeout_s}s waiting for: $desc" >&2
          exit 1
        fi
        sleep 0.2
      done
    }

    Xvfb :99 -screen 0 1440x900x24 >/dev/null &
    wait_for "X display :99 socket" "[ -S /tmp/.X11-unix/X99 ]" 15

    x11vnc -display :99 -nopw -forever -shared >/dev/null &
    wait_for "x11vnc listening on 5900" "(exec 3<>/dev/tcp/127.0.0.1/5900) 2>/dev/null" 15

    websockify --web=/usr/share/novnc 6080 localhost:5900 >/dev/null &
    wait_for "websockify listening on 6080" "(exec 3<>/dev/tcp/127.0.0.1/6080) 2>/dev/null" 15

    echo "Ouvre http://tower:6080/vnc.html, connecte-toi, puis Entrée ici."
    DISPLAY=:99 python3 -m instascrap --login
  '
