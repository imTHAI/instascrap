# The Playwright image ships a Chrome matching its own version; mismatching the two
# is the usual cause of "browser not found" at runtime. pyproject.toml pins the
# playwright *library* to this exact version for the same reason — see the comment
# there.
FROM mcr.microsoft.com/playwright:v1.49.0-noble

# python3-pip: the base image ships python3 for Playwright's own browser-install
# tooling, but no pip at all — not even as a python3 module.
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb x11vnc novnc websockify python3-pip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN python3 -m pip install --no-cache-dir --break-system-packages .

ENV PYTHONUNBUFFERED=1
# python3, not python: the base image has no `python` alias (only /usr/bin/python3).
ENTRYPOINT ["python3", "-m", "instascrap"]
CMD ["--run"]
