#!/usr/bin/env python3
"""End-to-end assertions for instrumental-selector. No pytest, no mocks:
real sqlite DB, real files, real HTTP, real ffmpeg-made audio."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
FFMPEG = os.environ.get("FFMPEG", os.path.expanduser("~/.hermes/tools/ffmpeg-static/ffmpeg"))
PORT = 8898
BASE = f"http://127.0.0.1:{PORT}"


def make_lib(tmp):
    lib = os.path.join(tmp, "lib")
    os.makedirs(os.path.join(lib, "data"))
    os.makedirs(os.path.join(lib, "music", "Artist", "Album"))
    src = os.path.join(lib, "music", "Artist", "Album", "1 - Song.m4a")
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:a", "aac", src], check=True)
    db = os.path.join(lib, "data", "navidrome.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE media_file (id TEXT PRIMARY KEY, path TEXT, title TEXT, artist TEXT,"
              " album TEXT, duration REAL, missing INTEGER DEFAULT 0)")
    c.execute("INSERT INTO media_file VALUES ('mf1','Artist/Album/1 - Song.m4a',"
              "'Song','Artist','Album',3.0,0)")
    c.execute("INSERT INTO media_file VALUES ('mf2','Gone/x.m4a','Gone','Ghost','Lost',9.0,1)")
    c.commit()
    c.close()
    return lib, src


def req(path, data=None, raw=None, headers=None):
    r = urllib.request.Request(BASE + path, method="POST" if (data or raw) else "GET")
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        r.add_header("Content-Type", "application/json")
    elif raw is not None:
        body = raw
        r.add_header("Content-Type", "audio/mpeg")
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    try:
        with urllib.request.urlopen(r, data=body, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def token_test(tmp):
    """With INSTR_TOKEN set, writes and worker endpoints require the header."""
    port = PORT + 7        # keep clear of 8898 (this test) and 8899 (a running instance)
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PORT=str(port), INSTR_LIB=os.path.join(tmp, "lib"),
               INSTR_CACHE=os.path.join(tmp, "cache2"), INSTR_JOBS=os.path.join(tmp, "jobs2.json"),
               INSTR_TOKEN="s3cret")
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/tracks?q=x", timeout=2)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("token server did not start")

        def post(payload, headers=None):
            r = urllib.request.Request(base + "/api/jobs", method="POST",
                                       data=json.dumps(payload).encode())
            r.add_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                r.add_header(k, v)
            try:
                with urllib.request.urlopen(r, timeout=5) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code

        assert post({"id": "mf1"}) == 401, "write without token must be refused"
        assert post({"id": "mf1"}, {"X-Instr-Token": "wrong"}) == 401
        assert post({"id": "mf1"}, {"X-Instr-Token": "s3cret"}) == 200
        try:
            urllib.request.urlopen(base + "/api/worker/next", timeout=5)
            raise AssertionError("worker endpoint must require the token")
        except urllib.error.HTTPError as e:
            assert e.code == 401, e.code
        print("token assertions passed")
    finally:
        srv.terminate()
        srv.wait(timeout=10)


def main():
    tmp = tempfile.mkdtemp(prefix="instr-test-")
    lib, src = make_lib(tmp)
    env = dict(os.environ, PORT=str(PORT), INSTR_LIB=lib,
               INSTR_CACHE=os.path.join(tmp, "cache"), INSTR_JOBS=os.path.join(tmp, "jobs.json"))
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                req("/api/tracks?q=x")
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("server did not start")

        # 1. search: only present rows, metadata intact
        code, body, _ = req("/api/tracks?q=artist")
        rows = json.loads(body)
        assert code == 200 and len(rows) == 1, rows
        assert rows[0]["title"] == "Song" and rows[0]["artist"] == "Artist", rows
        assert "path" not in rows[0], "must not leak paths to the client"
        code, body, _ = req("/api/tracks?q=ghost")
        assert json.loads(body) == [], "missing=1 rows must never surface"

        # 2. submit rejects unknown ids
        code, body, _ = req("/api/jobs", data={"id": "nope"})
        assert code == 400 and json.loads(body)["error"], (code, body)

        # 3. job lifecycle: queued -> running -> done
        code, body, _ = req("/api/jobs", data={"id": "mf1"})
        jid = json.loads(body)["jid"]
        assert code == 200 and jid, body
        assert json.loads(req(f"/api/job/{jid}")[1])["state"] == "queued"
        code, body, _ = req("/api/worker/next")
        job = json.loads(body)
        assert code == 200 and job["jid"] == jid, job
        assert json.loads(req(f"/api/job/{jid}")[1])["state"] == "running"
        assert req("/api/worker/next")[0] == 204, "only one job may be handed out"
        code, srcbytes, _ = req(job["source_url"])
        assert code == 200 and srcbytes == open(src, "rb").read()

        # fake instrumental: 3s mp3, uploaded as the worker would
        fake = os.path.join(tmp, "fake.mp3")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=220:duration=3", "-b:a", "192k", fake], check=True)
        assert req(f"/api/worker/result/{jid}", raw=open(fake, "rb").read())[0] == 200
        assert json.loads(req(f"/api/job/{jid}")[1])["state"] == "done"

        # 4. streaming: full, ranged, attachment
        code, audio, hdrs = req(f"/api/audio/{jid}")
        assert code == 200 and hdrs["Content-Type"] == "audio/mpeg" and len(audio) > 1024
        code, part, hdrs = req(f"/api/audio/{jid}", headers={"Range": "bytes=0-1023"})
        assert code == 206 and len(part) == 1024, (code, len(part))
        assert hdrs["Content-Range"].startswith("bytes 0-1023/"), hdrs
        code, _, hdrs = req(f"/api/download/{jid}")
        assert "attachment" in hdrs.get("Content-Disposition", "")

        # 5. cache hit: same track is done immediately, no new job
        code, body, _ = req("/api/jobs", data={"id": "mf1"})
        again = json.loads(body)["jid"]
        assert again == jid, "same media must reuse the same job"
        assert json.loads(req(f"/api/job/{again}")[1])["state"] == "done"

        # 6. failure path
        code, body, _ = req("/api/jobs", data={"id": "mf1"})
        # make a second, independent job by clearing cache + jobs file is overkill;
        # instead assert the fail endpoint reports through /api/job
        req(f"/api/worker/fail/{jid}", data={"error": "boom"})
        st = json.loads(req(f"/api/job/{jid}")[1])
        assert st["state"] == "failed" and st["error"] == "boom", st

        print("all assertions passed")
        token_test(tmp)
        return 0
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
