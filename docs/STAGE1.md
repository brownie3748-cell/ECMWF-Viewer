# Stage 1: renderer and Pages deploy

> Superseded for storage and publishing by [STAGE3.md](STAGE3.md) (Cloudflare R2, several models).
> The rendering notes below still apply.

## Pipeline (`.github/workflows/publish.yml`, main only)

Every 30 minutes (and on *Run workflow*):

1. Restore the previous site from the Actions cache (`site-v1-*`, newest entry).
2. `python -m render poll`:
   - looks at the last `site.lookback_runs` (16) 6-hourly cycles and asks the sources, in
     `data.source_order` (google → ecmwf → azure), whether the run's final step (+240 h for
     00/12Z, +144 h for 06/18Z) is published;
   - target window = the newest `site.retention_runs` (12) complete runs;
   - **prunes first**: deletes run directories outside the window and partial leftovers;
   - renders missing runs, newest first, until the time budget (150 min) is used up — the
     rest is picked up by the next poll;
   - writes `index.json`, `index.html` and `NOTICE.txt`.
3. If anything changed: save the site to the cache, upload it as the Pages artifact, deploy.
4. If the cache was lost, every run in the window is simply re-rendered from the mirrors
   (they keep about 2 years of runs, measured in Stage 0).

A run directory gets its `meta.json` last, so a run interrupted mid-way is never published and is
re-rendered on the next poll. Each step is downloaded (30 fields + the tp field of the step 3 or 6 h
earlier), rendered by a pool of `site.workers` processes and its GRIB files are deleted.

## Site layout

```
index.json                       manifest (schema below)
index.html, NOTICE.txt           human-readable pointer and attribution
runs/<YYYYMMDDHH>/<chart>_<SSS>.png
runs/<YYYYMMDDHH>/meta.json      per-run steps, WPSH values, timings
```

Images are palette PNG (PNG8). 13 charts per step (precipitation is drawn in `msl` since render_version 2): 533 images per 00/12Z run, 325 per 06/18Z run. Runs whose `meta.json` has an older `render_version` are deleted and re-rendered by the next poll.

## Manifest `index.json` (schema_version 1)

```json
{
  "schema_version": 1,
  "generated_utc": "2026-11-14T13:05:00Z",
  "model": "ECMWF IFS 0.25deg",
  "attribution": "Adapted from ... CC BY 4.0 ...",
  "kst_offset_h": 9,
  "path": "runs/{run}/{chart}_{step:03d}.png",
  "charts": [
    {"id": "msl", "title": "MSLP + precipitation", "domain": "default"},
    {"id": "wpsh500", "title": "WPSH line", "level": 500, "domain": "default", "overlay_of": "gz500"}
  ],
  "runs": [
    {"run": "2026111312", "steps": [0, 6, 12, 240], "wpsh": {"0": [5944, 5894], "6": null}},
    {"run": "2026111318", "steps": [0, 6, 144], "wpsh": null}
  ],
  "latest": "2026111318"
}
```

- `runs` is sorted oldest → newest. Image URL = `path` with `run`, `chart`, `step`.
- Past-run playback for a chart at valid time V: every run R with `(V - R) in steps`
  (and `step >= min_step` when a chart defines it), oldest to newest.
- `wpsh`: per step `[H, H-50]` in gpm, `null` for "no centre"; the whole field is `null` for
  06/18Z runs (no red line is drawn for them).

## Caching (measured in M6)

GitHub Pages serves every file with `Access-Control-Allow-Origin: *` and `Cache-Control: max-age=600`.
Image URLs never change content (the run is in the path), so caching them is harmless. The app should
request `index.json` with a cache-busting query (e.g. `index.json?t=<now>`) so a new run shows up
without the 10-minute delay.

## Operations

- Manual run: Actions → *publish (Stage 1)* → *Run workflow* (branch `main`).
- Local test of a few steps:
  `python -m render render-run --run $(python -m render latest --main-only) --steps 0,6,150 --site site --work work`
- Branch checks: `.github/workflows/ci.yml` (unit tests + smoke render, artifact `smoke-site`).
- Keepalive: GitHub disables scheduled workflows after 60 days without repository activity. The
  `keepalive` job re-enables the workflow through the API once a day. Whether this resets the
  60-day timer is **UNVERIFIED**; if the schedule stops, re-enable it in the Actions tab.
