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


def settings_test(tmp, lib):
    """Settings page contract: read, probe, reject bad paths, persist good ones."""
    s = json.loads(req("/api/settings")[1])
    assert s["lib"] == lib and s["has_token"] is False, s

    probe = json.loads(req("/api/settings/test", data={"lib": lib,
                                                       "music": os.path.join(lib, "music")})[1])
    assert probe["ok"] and probe["tracks"] == 1 and probe["sample"], probe

    bad = req("/api/settings", data={"lib": "/nope/does/not/exist"})
    assert bad[0] == 400 and "lib" in json.loads(bad[1])["fields"], bad

    good = req("/api/settings", data={"ttl_hours": 48, "model": "custom.onnx"})
    assert good[0] == 200, good
    s2 = json.loads(req("/api/settings")[1])
    assert s2["ttl_hours"] == 48 and s2["model"] == "custom.onnx", s2
    assert os.path.isfile(os.path.join(tmp, "config.json")), "settings must persist"

    # regression: the server injects `const TOKEN = "...";` — a naive replace once
    # produced `""""`, which killed the whole inline script (no stats, no search).
    for page in ("/", "/settings"):
        html = req(page)[1].decode()
        assert "__INSTR_TOKEN__" not in html, f"placeholder left in {page}"
        assert '""""' not in html, f"broken token literal in {page}"
        assert 'const TOKEN = "";' in html, f"token literal malformed in {page}"
    print("settings assertions passed")


def token_test(tmp):
    """With INSTR_TOKEN set, writes and worker endpoints require the header."""
    port = PORT + 7        # keep clear of 8898 (this test) and 8899 (a running instance)
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PORT=str(port), INSTR_LIB=os.path.join(tmp, "lib"),
               INSTR_CACHE=os.path.join(tmp, "cache2"), INSTR_JOBS=os.path.join(tmp, "jobs2.json"),
               INSTR_CONFIG=os.path.join(tmp, "config2.json"), INSTR_TOKEN="s3cret")
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


def durability_test(tmp):
    """The sweep must never eat a job that has no output file yet, and a dead
    worker's claim must expire instead of jamming the queue forever."""
    os.environ.update(INSTR_CACHE=os.path.join(tmp, "cache3"),
                      INSTR_JOBS=os.path.join(tmp, "jobs3.json"),
                      INSTR_CONFIG=os.path.join(tmp, "config3.json"))
    sys.path.insert(0, ROOT)
    import importlib
    srv = importlib.import_module("server")
    importlib.reload(srv)
    srv.apply_config({})
    os.makedirs(srv.CACHE, exist_ok=True)

    def job(state, name, claimed=None):
        return {"state": state, "media_id": "mf1", "name": name,
                "out": os.path.join(srv.CACHE, name), **({"claimed_at": claimed} if claimed else {})}

    srv.JOBS.clear()
    srv.JOBS.update(q1=job("queued", "a.mp3"),
                    r1=job("running", "b.mp3", time.time()),
                    d1=job("done", "c.mp3"),
                    f1=job("failed", "d.mp3"))
    srv.sweep_cache()
    assert "q1" in srv.JOBS and "r1" in srv.JOBS, "queued/running jobs must survive the sweep"
    assert "d1" not in srv.JOBS and "f1" not in srv.JOBS, "finished jobs with no file must be pruned"

    # an expired claim goes back to the queue; a fresh one is untouched
    srv.JOBS["r1"]["claimed_at"] = time.time() - srv.LEASE_S - 60
    srv.JOBS["r2"] = job("running", "e.mp3", time.time())
    assert srv.requeue_orphans() == 1
    assert srv.JOBS["r1"]["state"] == "queued", srv.JOBS["r1"]
    assert srv.JOBS["r2"]["state"] == "running", "fresh claims must not be requeued"
    print("queue durability assertions passed")


def onboarding_test(tmp):
    """A fresh install with no library must explain itself, not 500."""
    port = PORT + 9
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, PORT=str(port), INSTR_LIB=os.path.join(tmp, "no-such-lib"),
               INSTR_CACHE=os.path.join(tmp, "cache4"), INSTR_JOBS=os.path.join(tmp, "jobs4.json"),
               INSTR_CONFIG=os.path.join(tmp, "config4.json"))
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=2)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise SystemExit("onboarding server did not start")

        with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
            assert r.status == 200 and json.loads(r.read())["ok"] is True
        with urllib.request.urlopen(base + "/api/stats", timeout=5) as r:
            st = json.loads(r.read())
        assert st["configured"] is False and st["tracks"] == 0, st
        assert st["hint"], "the banner needs a reason to show"
        with urllib.request.urlopen(base + "/api/tracks?q=x", timeout=5) as r:
            assert json.loads(r.read()) == [], "unconfigured search must be empty, not an error"
        with urllib.request.urlopen(base + "/", timeout=5) as r:
            assert 'id="setup"' in r.read().decode(), "setup banner markup missing"
        print("onboarding assertions passed")
    finally:
        srv.terminate()
        srv.wait(timeout=10)


def main():
    tmp = tempfile.mkdtemp(prefix="instr-test-")
    lib, src = make_lib(tmp)
    env = dict(os.environ, PORT=str(PORT), INSTR_LIB=lib,
               INSTR_CACHE=os.path.join(tmp, "cache"), INSTR_JOBS=os.path.join(tmp, "jobs.json"),
               INSTR_CONFIG=os.path.join(tmp, "config.json"))
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
        settings_test(tmp, lib)
        token_test(tmp)
        durability_test(tmp)
        onboarding_test(tmp)
        return 0
    finally:
        srv.terminate()
        srv.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
