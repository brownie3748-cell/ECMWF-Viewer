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

Run `measure` with `only = m8`. `m8_urls` is optional: empty uses a built-in KIM global + regional set (yesterday 00 UTC,
GRIB2, `nwp_nc_vars_down.php`); otherwise give API Hub request URL(s) without the key. Connections are retried
(5 tries, counted in the report) because they are often reset from abroad (seen from the build sandbox).
The key is read from the `KMA_APIHUB_KEY` secret, masked in the log and replaced by `***` in the report.
The job summary shows: hub reachability from the runner, HTTP status, size, speed, file kind
(GRIB / NetCDF / JSON ...), and for GRIB the grid definition and the field list.

### M8 result (2026-11-15, run 2026111400, measured from a GitHub runner)

- Hub reachable 10/10 (0.7-3.3 s); no failed tries on any request (the sandbox resets were not seen on the runner).
- `nwp_nc_vars_down.php` returns **one field per request**: several `vars` give a text error ("vars ... one"),
  several `lvl_lst` levels return only the first level.
- KIM global (KIMG, cut to 100-180E 10-55N): `regular_ll`, 0.083333 deg (about 9 km), 961 x 540, 1.56 MB per
  field, 2.0-3.5 s per request (0.45-0.78 MB/s). +240 h available.
- KIM regional (KIMR, whole domain): **Lambert conformal, 3 km** (Dx = Dy = 3000 m), 1050 x 840, LoV 126E,
  Latin1/2 30/60N, first point 25.26N 110.62E; 2.65 MB per field, 2.7-20 s per request (0.13-0.97 MB/s).
  +87 h available. So the regional model is 3 km, not the 12 km of the page's overview text.
- The GRIB2 headers are **not usable for identification**: every message reads `shortName=t`, date 2007-03-23 12 UTC,
  step 0, whatever variable, level or lead time was requested. A KIM reader must label fields from the request
  (variable, level, run, lead time), not from the GRIB keys. Units of the values still need a check.
- Consequence for a KIM pipeline: about 30 fields per step means 30 requests per step (estimated: global 65 steps
  about 2000 requests and 3 GB per run; regional 30 steps about 900 requests and 2.4 GB per run). The API Hub daily
  request quota per key is UNVERIFIED.

## KIM models (render/kim.py)

Owner decision 2026-11-15: KIM global is disabled (`enabled = false`; a 0-240 h run is about 1230 requests,
about 100 min at the measured rate of 2 requests in parallel with a 15 s timeout). KIM regional runs 3-hourly to +84 h
(29 steps, about 640 requests per run, estimated 70-80 min). Whether KIM regional is output every 3 h is checked by the CI smoke (step 3).


- `data.source = "kma"` selects `kim.KimSource` (same interface as `sources.Sources`).
- One request per field and lead time; fields are labelled from the request (the GRIB headers are wrong, M8).
  4 requests in parallel, 4 tries each. The key comes from `KMA_APIHUB_KEY` and is never logged.
- KIM global: `box` = default map + margin; gh500 alone uses `wide_box_gh500` (100-180E) for the WPSH line.
  Fields are placed by index on a 1/12 deg lattice (no interpolation).
- KIM regional: whole Lambert domain, bilinear interpolation to a 0.05 deg lat/lon grid with pyproj
  (the projection's x/y lattice is checked against the GRIB point coordinates, max error 500 m).
  `QVAPOR` (mixing ratio) is converted to specific humidity.
- Precipitation (`prec_acc`, `RAIN`, kg m-2 accumulated) is converted to m like IFS `tp`; the previous step's
  value is cached, so a 6 h window needs no extra request.
- Completeness: a run counts as complete when its last step's MSLP downloads (KIM global: a 1 deg cut), at most
  once per 30 min per run and not before 4 h after the run time. Runs already in the store are not probed.
- Daily guard: calls and bytes per KST day are stored in the bucket (`_ops/kma-<date>.json`). A KIM run is not
  started when its estimated cost (`kma.est_bytes_per_field`) would pass `kma_quota`; it waits for the next day.
  The first backfill (9 runs per model in the 18-cycle window) therefore spreads over about 4 days (estimated).
- UNVERIFIED until the first CI smoke render: units and value ranges of the KIM fields (`field_ranges` in each
  run's meta.json lists min/max per field), whether `prec_acc` / `RAIN` accumulate from the run start.

## Map (render_version 7)

The default domain is `projection = "lcc"` with `extent_m` in the KIM regional grid's own Lambert x/y
(`plot.domain_geometry`): every model is drawn on that rectangle, so switching models never moves the map and
KIM regional fills it. Data are cropped to the rectangle's lat/lon bounding box (about 25.4-49.6N,
104.2-147.8E), hence the KIM global request box 101-151E, 22-53N.

## 2 m temperature chart and publication times (render_version 9)

- `t2m`: 2 m temperature with the fixed rainbow shading of the 925-700 hPa charts, white 3 deg C isotherms with
  labels (lines smoothed per model, `charts.t2m.smooth_sigma`), red 0 deg C line, 10 m wind barbs. Fields `2t`,
  `10u`, `10v` (single-level fields are keyed with level 0 in `grib.read`). KIM regional names `T2`, `U10`, `V10`
  are UNVERIFIED until the CI smoke render. Cost: 3 more fields per step (KIM regional 725 calls per run).
- The white isotherms of the 925-700 hPa charts now carry labels.
- Publication times in the app footer were measured from the Google mirror's object creation times
  (runs 2026-11-14/15, last `.index` of each run): IFS 0.25 deg +7.6 h (00/12Z) / +6.5 h (06/18Z),
  IFS 0.1 deg +9.7 h / +8.5 h, AIFS +5.4 h. KIM regional: not measured.

## KIM regional Korea zoom (`kimz`), translucent barbs

- `kimr` renders its charts a second time on `domains.korea` (same Lambert projection; 120E at the south-west
  corner to 15 km east of Dokdo, 31N to 10 km north of Liaodong Bay; about 1.2 km per pixel) for
  `site.zoom.charts` (500, MSLP + precipitation, 2 m temperature, 925 / 850 with MFC, 700 with RH).
  Images are stored with the kimr run as `z_<chart>_<step>.png`; the manifest lists a virtual model `kimz`
  (`data_model = "kimr"`, `chart_prefix = "z_"`) that the app shows as "KIM 지역 확대". No extra API Hub request.
  Estimated cost: 9 x 29 images per run, about +0.4 GB for 12 runs.
- Wind barbs are drawn with alpha 0.7.
