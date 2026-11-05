#!/usr/bin/env python3
"""Laptop-side worker: poll the selector for a job, separate vocals with MDX,
upload the instrumental MP3. Runs on Windows or Linux."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

SERVER = os.environ.get("INSTR_SERVER", "http://127.0.0.1:8899").rstrip("/")
TOKEN = os.environ.get("INSTR_TOKEN", "").strip()
ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS = os.environ.get("INSTR_MODELS", os.path.join(ROOT, "models"))
MODEL = os.environ.get("INSTR_MODEL", "UVR-MDX-NET-Inst_HQ_3.onnx")
POLL = 5


def _default_separator() -> str:
    win = os.path.join(ROOT, ".venv", "Scripts", "audio-separator.exe")
    nix = os.path.join(ROOT, ".venv", "bin", "audio-separator")
    return win if os.path.exists(win) else nix


SEP = os.environ.get("INSTR_SEPARATOR", _default_separator())


def api(path, data=None, raw=None):
    url = SERVER + path
    req = urllib.request.Request(url, method="POST" if (data is not None or raw is not None) else "GET")
    if TOKEN:
        req.add_header("X-Instr-Token", TOKEN)
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    elif raw is not None:
        body = raw
        req.add_header("Content-Type", "audio/mpeg")
    with urllib.request.urlopen(req, data=body, timeout=120) as r:
        payload = r.read()
        return r.status, (json.loads(payload) if payload else None)


def download(url, dest):
    req = urllib.request.Request(SERVER + url)
    if TOKEN:
        req.add_header("X-Instr-Token", TOKEN)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout)[-800:])
    return p.stdout


def lufs(path):
    """Integrated loudness, so the instrumental can be levelled against the original."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path,
                        "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    found = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", p.stderr)
    return float(found[-1]) if found else None


def separate(src, workdir, model=None):
    model = model or MODEL
    wav = os.path.join(workdir, "in.wav")
    run(["ffmpeg", "-v", "error", "-y", "-i", src, "-ar", "48000", "-ac", "2",
         "-c:a", "pcm_s16le", wav])
    outdir = os.path.join(workdir, "out")
    os.makedirs(outdir, exist_ok=True)
    run([SEP, wav, "--model_filename", model, "--output_format", "WAV",
         "--sample_rate", "48000", "--single_stem", "Instrumental",
         "--model_file_dir", MODELS, "--output_dir", outdir])
    stems = [os.path.join(outdir, f) for f in os.listdir(outdir) if f.lower().endswith(".wav")]
    if not stems:
        raise RuntimeError("separator produced no stem")

    # one lossy encode only: WAV stem -> MP3, gain-matched to the original
    src_l, inst_l = lufs(src), lufs(stems[0])
    gain = 0.0
    if src_l is not None and inst_l is not None:
        gain = max(-12.0, min(12.0, src_l - inst_l))
    mp3 = os.path.join(workdir, "instrumental.mp3")
    run(["ffmpeg", "-v", "error", "-y", "-i", stems[0],
         "-af", f"volume={gain:.2f}dB", "-ar", "48000", "-ac", "2",
         "-c:a", "libmp3lame", "-b:a", "320k", mp3])
    print(f"    level: source {src_l} LUFS, stem {inst_l} LUFS, gain {gain:+.2f} dB", flush=True)
    return mp3


def main():
    print(f"worker polling {SERVER}", flush=True)
    while True:
        try:
            status, job = api("/api/worker/next")
            if status == 204 or not job:
                time.sleep(POLL)
                continue
            jid = job["jid"]
            print(f"[{time.strftime('%H:%M:%S')}] job {jid}: {job['filename']}", flush=True)
            workdir = tempfile.mkdtemp(prefix="instr-")
            try:
                src = os.path.join(workdir, "source")
                download(job["source_url"], src)
                mp3 = separate(src, workdir, job.get("model"))
                with open(mp3, "rb") as f:
                    api(f"/api/worker/result/{jid}", raw=f.read())
                print(f"[{time.strftime('%H:%M:%S')}] job {jid}: done", flush=True)
            except Exception as exc:                      # noqa: BLE001 - report, keep polling
                print(f"[{time.strftime('%H:%M:%S')}] job {jid}: failed: {exc}", flush=True)
                api(f"/api/worker/fail/{jid}", data={"error": str(exc)[:400]})
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:                          # noqa: BLE001 - server offline, retry
            print(f"poll error: {exc}", flush=True)
            time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
