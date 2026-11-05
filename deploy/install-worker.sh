#!/usr/bin/env bash
# Worker setup for Linux / macOS. Creates ./.venv with CPU-only torch + audio-separator.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
command -v ffmpeg >/dev/null || { echo "ffmpeg not found on PATH — install it first"; exit 1; }
command -v uv >/dev/null && HAVE_UV=1 || HAVE_UV=0

if [ ! -d .venv ]; then
  if [ "$HAVE_UV" = 1 ]; then uv venv --python 3.12 .venv; else "$PY" -m venv .venv; fi
fi
PIP=".venv/bin/pip"; [ "$HAVE_UV" = 1 ] && PIP="uv pip install --python .venv/bin/python"

echo "==> torch (CPU wheels)"
$PIP torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
echo "==> audio-separator"
$PIP "audio-separator[cpu]" audioread

echo
echo "Done. Start the worker with:"
echo "  INSTR_SERVER=http://your-server:8899 .venv/bin/python worker.py"
