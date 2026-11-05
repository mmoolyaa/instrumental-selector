# Architecture

## Roles

| Part | Runs on | Does |
|---|---|---|
| server | any small box with the Navidrome data dir | search, queue, cache, serve |
| worker | a machine with CPU to spare | download source, separate, upload MP3 |
| browser | anywhere | UI |

The server never imports torch and never loads a model. That split exists because separation is
CPU-bound and the box that hosts Navidrome is usually not the fastest machine you own.

Measured reference points, same 6:02 track, MDX-NET Inst HQ 3:

```
2012 4-thread i3 (no AVX2)       ~28 min
Ryzen 7 6800HS (8C/16T, AVX2)      4:58
Radeon RX 6700S via DirectML      ~6:30   (slower — small model, per-chunk overhead)
```

## Data flow

1. `GET /api/tracks?q=` → SQLite query against `media_file` (`missing=0`, `LIMIT 50`).
2. `POST /api/jobs {id}` → resolves the id to a path server-side, returns a job id.
   A cached result or an existing job for the same track short-circuits to `done`.
3. Worker polls `GET /api/worker/next`; the server flips the job to `running` and hands back
   a `source_url` for that one job only.
4. Worker decodes to 48 kHz WAV, runs the separator, measures integrated loudness of both the
   source and the stem, applies the difference as gain, encodes once to MP3 320k, uploads.
5. Server stores the MP3 under `cache/<sha1(media_id)>.mp3` and serves it with Range support.

## Library access

Navidrome's own SQLite DB is the index — no Subsonic credentials, no extra service:

```
media_file(id, path, title, artist, album, duration, missing)
```

Two traps worth knowing:

- `path` is relative to the **music folder** (`<data>/music`), not to the data root. Joining to
  the wrong base makes every row unresolvable while looking correct.
- Rows can be `missing=1` after a library move. Filter `missing=0` or the UI lists dead tracks.

The DB is opened `mode=ro`. Paths are never accepted from clients — only ids.

## Queue semantics

- Single-consumer: `/api/worker/next` hands out one job at a time, so two workers cannot run
  the same track.
- State lives in `jobs.json`; `running` is downgraded to `queued` on restart so a crashed server
  does not strand work.
- Cache is keyed by media id, so a repeat request costs nothing.

## Output quality

- Decode once (AAC/m4a → WAV), separate, encode once (WAV → MP3 320k). No double lossy pass.
- Gain-matched to the source's integrated loudness (EBU R128), clamped to ±12 dB.
- Ceiling is the model: MDX removes centred content, so kick/snare/bass/lead lose some body and
  harmonies can ghost. That is a model limit, not a pipeline bug.

## Failure modes

| Symptom | Cause |
|---|---|
| Job sits `queued` | worker not running or cannot reach the server |
| `Format not recognised` | feeding m4a straight to the separator; decode first |
| `torchvision::nms does not exist` | torchvision from PyPI against a `+cpu` torch |
| `FileNotFoundError: 'ffmpeg'` | ffmpeg not on the worker's PATH |
| Everything 401 | `INSTR_TOKEN` set on one side only |
