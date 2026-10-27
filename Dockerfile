# The Playwright image ships a Chrome matching its own version; mismatching the two
# is the usual cause of "browser not found" at runtime. pyproject.toml pins the
# playwright *library* to this exact version for the same reason — see the comment
# there.
FROM mcr.microsoft.com/playwright:v1.49.0-noble

# python3-pip: the base image ships python3 for Playwright's own browser-install
# tooling, but no pip at all — not even as a python3 module.
# tini is PID 1: it forwards Docker's SIGTERM to the Python process and reaps
# the zombies Chrome, Xvfb and Caddy leave behind.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify python3-pip tini \
    && rm -rf /var/lib/apt/lists/*

# Static file server for the feeds. The official binary, copied rather than
# installed from a third-party apt repository.
COPY --from=caddy:2 /usr/bin/caddy /usr/local/bin/caddy
COPY docker/Caddyfile /etc/caddy/Caddyfile
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Xvfb creates its socket here; as a non-root user it cannot create the
# directory itself, and it must be world-writable with the sticky bit.
RUN mkdir -m 1777 -p /tmp/.X11-unix

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN python3 -m pip install --no-cache-dir --break-system-packages .

# browser.py launches channel="chrome" — real Google Chrome, not the Chromium the
# base image bundles — deliberately, for the TLS/HTTP2 fingerprint fidelity a bundled
# Chromium doesn't give it. The base image doesn't ship it; this pulls the current
# Google Chrome .deb and its apt dependencies via Playwright's own installer.
RUN python3 -m playwright install chrome

# HOME on the volume: Chrome writes crash reports and caches under it, and the
# process runs as PUID, which has no home of its own. Caddy needs writable XDG
# directories even with its admin API off.
ENV PYTHONUNBUFFERED=1 HOME=/data/home XDG_CONFIG_HOME=/tmp/xdg XDG_DATA_HOME=/tmp/xdg

EXPOSE 8080 6080
VOLUME /data
ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["--serve"]
