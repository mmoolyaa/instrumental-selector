# Server only — the worker (torch + audio-separator, ~1.4 GB) stays on your own machine.
FROM python:3.12-slim

# Stdlib only: no pip install, no build tools, nothing to compile.
WORKDIR /app
COPY entrypoint.sh server.py index.html settings.html ./
RUN chmod +x entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PORT=8899 \
    INSTR_LIB=/navidrome \
    INSTR_MUSIC=/music \
    INSTR_CONFIG=/data/config.json \
    INSTR_CACHE=/data/cache \
    INSTR_JOBS=/data/jobs.json

# No USER here on purpose: entrypoint.sh runs as root only long enough to fix
# ownership of the /data mount, then drops to this user with setpriv.
RUN useradd -u 1000 -U -m app

EXPOSE 8899
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8899/api/health', timeout=4).read()" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
