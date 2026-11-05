#!/usr/bin/env python3
"""Instrumental Selector — search Navidrome's library, queue a vocal-removal job,
serve the resulting MP3. Compute happens on the laptop worker, never here."""
import hashlib
import html
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("INSTR_CONFIG", os.path.join(ROOT, "config.json"))

# Effective values — env first, then config.json (written by the settings page).
LIB = os.environ.get("INSTR_LIB", "/srv/data/navidrome")
MUSIC = ""            # media_file.path is relative to Navidrome's music folder
DB_URI = ""
CACHE = os.environ.get("INSTR_CACHE", os.path.join(ROOT, "cache"))
JOBS_FILE = os.environ.get("INSTR_JOBS", os.path.join(ROOT, "jobs.json"))
TTL_HOURS = float(os.environ.get("INSTR_TTL_HOURS", "168"))   # 7 days, then the mp3 is deleted
TOKEN = os.environ.get("INSTR_TOKEN", "").strip()             # empty = no auth (LAN only)
MODEL = os.environ.get("INSTR_MODEL", "UVR-MDX-NET-Inst_HQ_3.onnx")   # advertised to workers
PORT = int(os.environ.get("PORT", "8899"))


def load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def apply_config(cfg: dict) -> None:
    """Config file wins over env; empty values fall through to env/defaults."""
    global LIB, MUSIC, DB_URI, CACHE, TTL_HOURS, TOKEN, MODEL
    LIB = cfg.get("lib") or os.environ.get("INSTR_LIB", "/srv/data/navidrome")
    MUSIC = cfg.get("music") or os.environ.get("INSTR_MUSIC", os.path.join(LIB, "music"))
    CACHE = cfg.get("cache") or os.environ.get("INSTR_CACHE", os.path.join(ROOT, "cache"))
    TTL_HOURS = float(cfg.get("ttl_hours") or os.environ.get("INSTR_TTL_HOURS", "168"))
    TOKEN = str(cfg.get("token") if cfg.get("token") is not None
                else os.environ.get("INSTR_TOKEN", "")).strip()
    MODEL = cfg.get("model") or os.environ.get("INSTR_MODEL", "UVR-MDX-NET-Inst_HQ_3.onnx")
    DB_URI = f"file:{LIB}/data/navidrome.db?mode=ro"


def save_config(cfg: dict) -> None:
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
    os.replace(tmp, CONFIG_FILE)


def probe_library(lib: str, music: str) -> dict:
    """Checks the settings page can run before saving anything."""
    out = {"lib_ok": False, "db_ok": False, "music_ok": False, "tracks": 0, "sample": None,
           "errors": []}
    if not os.path.isdir(lib):
        out["errors"].append(f"library dir not found: {lib}")
    else:
        out["lib_ok"] = True
    db_file = os.path.join(lib, "data", "navidrome.db")
    if not os.path.isfile(db_file):
        out["errors"].append(f"no navidrome.db under {os.path.join(lib, 'data')}")
    else:
        try:
            with sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=5) as c:
                out["tracks"] = c.execute(
                    "SELECT count(*) FROM media_file WHERE missing=0").fetchone()[0]
                row = c.execute("SELECT path FROM media_file WHERE missing=0 LIMIT 1").fetchone()
                out["db_ok"] = True
                out["sample"] = row[0] if row else None
        except sqlite3.Error as exc:
            out["errors"].append(f"cannot read navidrome.db: {exc}")
    if not os.path.isdir(music):
        out["errors"].append(f"music dir not found: {music}")
    else:
        out["music_ok"] = True
        if out["sample"]:
            probe = _resolve_ci(music, out["sample"])
            if probe and os.path.isfile(probe):
                out["sample"] = probe
            else:
                out["errors"].append("DB paths do not resolve under the music dir — "
                                     "check that it is the folder Navidrome scans")
    out["ok"] = not out["errors"]
    return out

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
LAST_WORKER = 0.0


def stats() -> dict:
    with db() as c:
        tracks = c.execute("SELECT count(*) FROM media_file WHERE missing=0").fetchone()[0]
    with LOCK:
        queued = sum(1 for j in JOBS.values() if j["state"] == "queued")
        running = sum(1 for j in JOBS.values() if j["state"] == "running")
    cached_n = len([f for f in os.listdir(CACHE) if f.endswith(".mp3")]) if os.path.isdir(CACHE) else 0
    return {"tracks": tracks, "cached": cached_n, "queued": queued, "running": running,
            "worker_online": (time.time() - LAST_WORKER) < 30}


def db() -> sqlite3.Connection:
    return sqlite3.connect(DB_URI, uri=True, timeout=5)


def search(q: str, limit: int = 50) -> list[dict]:
    q = (q or "").strip()
    cols = "id, title, artist, album, duration"
    if q:
        like = f"%{q}%"
        sql = (f"SELECT {cols} FROM media_file WHERE missing=0 AND "
               "(title LIKE ? OR artist LIKE ? OR album LIKE ?) "
               "ORDER BY artist, album, title LIMIT ?")
        args = (like, like, like, limit)
    else:
        sql = f"SELECT {cols} FROM media_file WHERE missing=0 ORDER BY id LIMIT ?"
        args = (limit,)
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return [{"id": r[0], "title": r[1], "artist": r[2], "album": r[3],
             "duration": round(r[4] or 0, 1)} for r in rows]


def _resolve_ci(base: str, rel: str) -> str | None:
    """Join a DB-relative path under base, matching each segment case-insensitively.
    The library was moved once: rows contain both 'Music/...' and 'music/...'."""
    cur = base
    for part in rel.split("/"):
        if not part:
            continue
        exact = os.path.join(cur, part)
        if os.path.exists(exact):
            cur = exact
            continue
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        low = part.lower()
        match = next((n for n in names if n.lower() == low), None)
        if match is None:
            return None
        cur = os.path.join(cur, match)
    return cur


def media_info(media_id: str) -> dict | None:
    with db() as c:
        row = c.execute("SELECT title, artist, missing FROM media_file WHERE id=?",
                        (media_id,)).fetchone()
    if not row or row[2]:
        return None
    return {"title": row[0] or "track", "artist": row[1] or "unknown"}


def safe_name(media_id: str) -> str:
    info = media_info(media_id) or {"artist": "unknown", "title": "track"}
    raw = f"{info['artist']} - {info['title']} (Instrumental).mp3"
    return re.sub(r'[\\/:*?"<>|]+', "_", raw)


def media_path(media_id: str) -> str | None:
    """Resolve a media_file id to an absolute path inside the library. Client never sends paths."""
    with db() as c:
        row = c.execute("SELECT path, missing FROM media_file WHERE id=?", (media_id,)).fetchone()
    if not row or row[1]:
        return None
    path = _resolve_ci(MUSIC, row[0])
    if not path:
        return None
    path = os.path.normpath(path)
    if not path.startswith(MUSIC + os.sep) or not os.path.isfile(path):
        return None
    return path


def cache_key(media_id: str) -> str:
    return hashlib.sha1(media_id.encode()).hexdigest()


def cached(media_id: str) -> str | None:
    out = os.path.join(CACHE, cache_key(media_id) + ".mp3")
    return out if os.path.isfile(out) and os.path.getsize(out) > 1024 else None


def save_jobs() -> None:
    tmp = JOBS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: {kk: vv for kk, vv in v.items() if kk != "out"} for k, v in JOBS.items()}, f)
    os.replace(tmp, JOBS_FILE)


def load_jobs() -> None:
    if not os.path.isfile(JOBS_FILE):
        return
    try:
        with open(JOBS_FILE) as f:
            stored = json.load(f)
    except (OSError, ValueError):
        return
    for jid, job in stored.items():
        job["out"] = os.path.join(CACHE, cache_key(job["media_id"]) + ".mp3")
        if job["state"] == "running":       # server restarted mid-job
            job["state"] = "queued"
        JOBS[jid] = job


def submit(media_id: str) -> tuple[str | None, str | None]:
    if media_path(media_id) is None:
        return None, "unknown or missing track"
    hit = cached(media_id)
    with LOCK:
        for jid, job in JOBS.items():
            if job["media_id"] == media_id and job["state"] in ("queued", "running", "done"):
                if job["state"] == "done" and not hit:
                    job["state"] = "queued"
                job.setdefault("name", safe_name(media_id))
                return jid, None
        jid = uuid.uuid4().hex[:12]
        JOBS[jid] = {"state": "done" if hit else "queued", "media_id": media_id,
                     "name": safe_name(media_id),
                     "out": hit or os.path.join(CACHE, cache_key(media_id) + ".mp3")}
        save_jobs()
    return jid, None


def next_job() -> dict | None:
    with LOCK:
        for jid, job in JOBS.items():
            if job["state"] == "queued":
                job["state"] = "running"
                save_jobs()
                path = media_path(job["media_id"])
                if path is None:
                    job["state"] = "failed"
                    job["error"] = "source file vanished"
                    save_jobs()
                    continue
                return {"jid": jid, "source_url": f"/api/worker/source/{jid}",
                        "filename": os.path.basename(path), "model": MODEL}
    return None


def send_file(handler: BaseHTTPRequestHandler, path: str, ctype: str, download: str | None = None) -> None:
    size = os.path.getsize(path)
    start, end = 0, size - 1
    rng = handler.headers.get("Range")
    partial = False
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m:
            if m.group(1):
                start = int(m.group(1))
            if m.group(2):
                end = min(int(m.group(2)), size - 1)
            if start > end or start >= size:
                handler.send_error(416)
                return
            partial = True
    length = end - start + 1
    handler.send_response(206 if partial else 200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    if partial:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    if download:
        handler.send_header("Content-Disposition", f'attachment; filename="{download}"')
    handler.end_headers()
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            chunk = f.read(min(65536, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _authed(self) -> bool:
        """Writes and worker endpoints require X-Instr-Token when INSTR_TOKEN is set."""
        return not TOKEN or self.headers.get("X-Instr-Token") == TOKEN

    def _deny(self):
        self._json({"error": "unauthorized"}, 401)

    def _json(self, obj, code=200):
        body = b"" if code == 204 else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self, limit=60 * 1024 * 1024) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            self.send_error(413)
            return b""
        return self.rfile.read(n)

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            with open(os.path.join(ROOT, "index.html"), "rb") as f:
                body = f.read()
            body = body.replace(b"__INSTR_TOKEN__", json.dumps(TOKEN).encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/tracks":
            q = (parse_qs(u.query).get("q") or [""])[0]
            return self._json(search(q))
        if p == "/api/stats":
            return self._json(stats())
        if p == "/settings":
            with open(os.path.join(ROOT, "settings.html"), "rb") as f:
                body = f.read()

            def esc(v) -> bytes:
                return html.escape(str(v), quote=True).encode()

            note = ("A token is set. Anyone with it can queue work and use the worker endpoints."
                    if TOKEN else
                    "No token set — fine on a trusted LAN, risky on a public address.")
            for key, val in ((b"__LIB__", LIB), (b"__MUSIC__", MUSIC), (b"__CACHE__", CACHE),
                             (b"__TTL__", f"{TTL_HOURS:g}"), (b"__MODEL__", MODEL),
                             (b"__TOKENPH__", "•••••••• token set — type to replace" if TOKEN
                              else "no token — anyone can write"),
                             (b"__TOKENNOTE__", note)):
                body = body.replace(key, esc(val))
            body = body.replace(b"__INSTR_TOKEN__", json.dumps(TOKEN).encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/settings":
            if not self._authed():
                return self._deny()
            return self._json({"lib": LIB, "music": MUSIC, "cache": CACHE,
                               "ttl_hours": TTL_HOURS, "model": MODEL,
                               "has_token": bool(TOKEN), "config_file": CONFIG_FILE})
        if p == "/api/cached":
            with LOCK:
                done = [(jid, j) for jid, j in JOBS.items()
                        if j["state"] == "done" and os.path.isfile(j["out"])]
            done.sort(key=lambda kv: os.path.getmtime(kv[1]["out"]), reverse=True)
            return self._json([{"jid": jid, "name": j.get("name") or os.path.basename(j["out"]),
                                "media_id": j["media_id"]} for jid, j in done[:12]])
        if p == "/api/worker/next":
            if not self._authed():
                return self._deny()
            global LAST_WORKER
            LAST_WORKER = time.time()
            job = next_job()
            return self._json(job, 200) if job else self._json(None, 204)
        m = re.match(r"^/api/(job|audio|download)/([0-9a-f]{6,32})$", p)
        if m:
            kind, jid = m.groups()
            job = JOBS.get(jid)
            if not job:
                return self.send_error(404)
            if kind == "job":
                return self._json({"state": job["state"], "error": job.get("error")})
            if job["state"] != "done" or not os.path.isfile(job["out"]):
                return self.send_error(409, "not ready")
            name = job.get("name") or os.path.basename(job["out"])
            return send_file(self, job["out"], "audio/mpeg", name if kind == "download" else None)
        m = re.match(r"^/api/worker/source/([0-9a-f]{6,32})$", p)
        if m:
            if not self._authed():
                return self._deny()
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_error(404)
            src = media_path(job["media_id"])
            if not src:
                return self.send_error(410)
            return send_file(self, src, "application/octet-stream")
        self.send_error(404)

    def do_POST(self):
        p = urlparse(self.path).path
        if p in ("/api/settings", "/api/settings/test"):
            if not self._authed():
                return self._deny()
            try:
                data = json.loads(self._body(65536) or b"{}")
            except ValueError:
                return self.send_error(400)
            if p.endswith("/test"):
                lib = (data.get("lib") or LIB).strip()
                music = (data.get("music") or MUSIC or os.path.join(lib, "music")).strip()
                return self._json(probe_library(lib, music))

            errors = {}
            lib = (data.get("lib") or "").strip()
            music = (data.get("music") or "").strip()
            cache = (data.get("cache") or "").strip()
            model = (data.get("model") or "").strip()
            if lib and not os.path.isdir(lib):
                errors["lib"] = "directory not found"
            if music and not os.path.isdir(music):
                errors["music"] = "directory not found"
            if cache:
                try:
                    os.makedirs(cache, exist_ok=True)
                except OSError as exc:
                    errors["cache"] = str(exc)
            ttl = data.get("ttl_hours")
            if ttl not in (None, ""):
                try:
                    if float(ttl) <= 0:
                        errors["ttl_hours"] = "must be greater than 0"
                except (TypeError, ValueError):
                    errors["ttl_hours"] = "not a number"
            if errors:
                return self._json({"error": "validation failed", "fields": errors}, 400)

            cfg = load_config()
            if lib:
                cfg["lib"] = lib
            if music:
                cfg["music"] = music
            if cache:
                cfg["cache"] = cache
            if model:
                cfg["model"] = model
            if ttl not in (None, ""):
                cfg["ttl_hours"] = float(ttl)
            if "token" in data and data["token"] is not None:
                cfg["token"] = str(data["token"])
            save_config(cfg)
            apply_config(cfg)
            os.makedirs(CACHE, exist_ok=True)
            return self._json({"ok": True, "lib": LIB, "music": MUSIC, "cache": CACHE,
                               "ttl_hours": TTL_HOURS, "model": MODEL, "has_token": bool(TOKEN)})

        if p == "/api/jobs":
            if not self._authed():
                return self._deny()
            try:
                data = json.loads(self._body(65536) or b"{}")
            except ValueError:
                return self.send_error(400)
            jid, err = submit(str(data.get("id", "")))
            return self._json({"jid": jid, "error": err}, 200 if jid else 400)
        m = re.match(r"^/api/worker/result/([0-9a-f]{6,32})$", p)
        if m:
            if not self._authed():
                return self._deny()
            jid = m.group(1)
            job = JOBS.get(jid)
            if not job:
                return self.send_error(404)
            body = self._body()
            if len(body) < 1024:
                return self.send_error(400, "empty result")
            os.makedirs(CACHE, exist_ok=True)
            tmp = job["out"] + ".part"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, job["out"])
            with LOCK:
                job["state"] = "done"
                save_jobs()
            return self._json({"ok": True, "bytes": len(body)})
        m = re.match(r"^/api/worker/fail/([0-9a-f]{6,32})$", p)
        if m:
            if not self._authed():
                return self._deny()
            job = JOBS.get(m.group(1))
            if not job:
                return self.send_error(404)
            try:
                data = json.loads(self._body(65536) or b"{}")
            except ValueError:
                data = {}
            with LOCK:
                job["state"] = "failed"
                job["error"] = str(data.get("error", "worker failed"))[:500]
                save_jobs()
            return self._json({"ok": True})
        self.send_error(404)


def sweep_cache() -> int:
    """Delete cached instrumentals older than TTL_HOURS and forget their jobs."""
    if not os.path.isdir(CACHE):
        return 0
    cutoff = time.time() - TTL_HOURS * 3600
    removed = 0
    with LOCK:
        for name in os.listdir(CACHE):
            if not name.endswith(".mp3"):
                continue
            path = os.path.join(CACHE, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
        for jid in [j for j, job in JOBS.items() if not os.path.isfile(job["out"])]:
            del JOBS[jid]
        if removed:
            save_jobs()
    return removed


def sweep_loop() -> None:
    while True:
        try:
            n = sweep_cache()
            if n:
                print(f"cache sweep: removed {n} file(s) older than {TTL_HOURS:g}h", flush=True)
        except Exception as exc:                       # noqa: BLE001 - never kill the thread
            print(f"cache sweep failed: {exc}", flush=True)
        time.sleep(3600)


def main():
    apply_config(load_config())
    os.makedirs(CACHE, exist_ok=True)
    load_jobs()
    sweep_cache()
    threading.Thread(target=sweep_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"instrumental-selector on :{PORT}  lib={LIB}  cache={CACHE}  ttl={TTL_HOURS:g}h", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
