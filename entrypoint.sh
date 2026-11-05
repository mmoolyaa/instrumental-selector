#!/bin/sh
set -eu
# Docker creates a fresh named volume owned by root, so a non-root app cannot
# write /data (config.json, cache, jobs.json) on first boot. Fix ownership of
# the mount point itself only — never -R, the cache can hold gigabytes — then
# drop privileges before starting the server.
if [ "$(id -u)" = 0 ]; then
  chown app:app /data
  exec setpriv --reuid=app --regid=app --init-groups -- python server.py
fi
exec python server.py
