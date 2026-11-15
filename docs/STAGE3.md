# Stage 3: Cloudflare R2 storage and several models

## Models

| id | data | charts | steps |
|---|---|---|---|
| `ifs` | IFS HRES 0.25 deg (`ifs/0p25/oper`) | all 13 | 00/12Z: 0-240 h, 06/18Z: 0-144 h; every 3 h to 144 h, then 6 h |
| `ifs01` | IFS HRES 0.1 deg (`ifs/0p1/oper`, split `-sfc` / `-pl` files) | all except 300, 200, 500 wide | same |
| `aifs` | AIFS Single 0.25 deg (`aifs-single/0p25/oper`) | all 13 | same lengths, every 6 h |

Each model is the base config deep-merged with its `[[models]] override` (`render.config.for_model`).
Measured on the Google mirror (runs 2026-11-14/15):

- `ifs/0p1/oper`: 85 steps 0-360 h (00Z), 49 steps 0-144 h (06Z). The fields we need are about 42 MB
  (pressure levels) per step plus the surface fields; one 2-step smoke render downloaded in 15.5 s.
- `aifs-single/0p25/oper`: 61 steps 0-360 h every 6 h for 00Z and 06Z. No `r`: 700 hPa RH is derived
  from `q` and `t` (IFS mixed-phase saturation). `tp` is in kg m-2 (IFS: m) and is converted on read.
- Average image size in the smoke render: IFS 0.1 deg about 118 KB, AIFS about 92 KB (PNG8).

## Storage layout (R2 bucket, public via r2.dev)

```
index.json                         manifest (schema_version 2), Cache-Control: no-cache
NOTICE.txt
<model>/<run>/<chart>_<step>.png   Cache-Control: public, max-age=86400
<model>/<run>/meta.json            uploaded after the run's images
_ops/<YYYY-MM>.json                request counter (monthly budget)
```

## Poll (`python -m render poll --store r2`)

1. Read `index.json` (one Class B request). For every model find the newest 12 complete runs
   (HEAD of the final step's index on the mirrors).
2. Nothing to do -> stop (no writes).
3. Otherwise list the bucket (actual sizes), drop runs that left the window from the manifest first, then
   delete their objects; delete orphaned prefixes from interrupted uploads.
4. Render missing runs newest cycle first; within a cycle `site.process_order` (IFS, AIFS, IFS 0.1: lightest
   first). After every uploaded run the newest two cycles are re-checked, and a run published meanwhile
   goes to the front of the queue. All within the time budget. Before each upload
   the guard checks `bucket size - replaced + new <= storage.max_bytes` (9 GB) and the monthly Class A
   count; if either fails nothing of that run is uploaded and the job fails (red run = alert).
5. Upload images, then `meta.json`, then rewrite `index.json` (so the app never sees a half-uploaded run).
6. Job summary: storage used / cap, per-model MB, requests this month.

Request counts are an upper bound: every write, list and delete counts as Class A, every read as Class B.
Estimated per month: about 1300 uploads per 6-hourly cycle x 120 cycles = 160k Class A, plus about 4k
polls with work. Storage estimate: 12 runs x 3 models, about 1.3-1.6 GB.

## Owner setup (done once)

- R2 bucket `ecmwf-viewer`, public development URL (r2.dev) enabled, CORS policy:
  `[{"AllowedOrigins":["*"],"AllowedMethods":["GET","HEAD"],"AllowedHeaders":["*"],"MaxAgeSeconds":3600}]`
- R2 API token: Object Read & Write, this bucket only.
- GitHub Actions secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
  (optional `R2_BUCKET` if the bucket name differs from the config).

UNVERIFIED (Cloudflare docs were not reachable from the build sandbox): R2 free-tier numbers
(10 GB-month, 1M Class A, 10M Class B, free egress) and r2.dev rate limits. The first publish run reports
the public URL's headers (status, CORS, Cache-Control) in its job summary.

## Web app / APK

- GitHub Pages now serves only the web app (`pages.yml`), at the same `<pages>/app/` URL.
- The app reads `index.json` from R2 (`DATA_BASE` in `app/www/app.js`). APKs before 0.3.0 read the old
  Pages manifest and stop working once Pages carries only the app: install the 0.3.0 release.

## M8: KMA API Hub (KIM regional), measurement only

Run `measure` with `only = m8`. `m8_urls` is optional: empty uses a built-in KIM regional set (yesterday 00 UTC, +24 h,
GRIB2, `nwp_nc_vars_down.php`); otherwise give API Hub request URL(s) without the key. Connections are retried
(5 tries, counted in the report) because they are often reset from abroad (seen from the build sandbox).
The key is read from the `KMA_APIHUB_KEY` secret, masked in the log and replaced by `***` in the report.
The job summary shows: hub reachability from the runner, HTTP status, size, speed, file kind
(GRIB / NetCDF / JSON ...), and for GRIB the grid definition and the field list.
