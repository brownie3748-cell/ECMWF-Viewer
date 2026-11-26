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
  IFS 0.1 deg +9.7 h / +8.5 h, AIFS +5.4 h.
- KIM regional, 2026-11-17 00Z: estimated available by about 14:25 KST. The poll ran from 05:20:32 to
  06:54:21 UTC. After the +84 h MSLP check passed, the download ran for about 90 minutes before hitting
  the quota. This places the successful check near the start of the poll.
  [Job timings](https://github.com/brownie3748-cell/ECMWF-Viewer/actions/runs/44023371479/job/133837815641)
  · [Download and quota report](https://github.com/brownie3748-cell/ECMWF-Viewer/pull/22)
- KIM regional, 2026-11-17 12Z: the +84 h MSLP request started at 17:10:49 UTC. Rendering started by
  17:11:19 UTC (17:54:42 UTC completion minus 2603.5 s), confirming availability around 02:11 KST on
  November 18. Each cycle has one observation. The API may have served the field earlier.
  [Probe timestamps](https://pub-07aea3ace26c452cbe6112df32e49088.r2.dev/_ops/kma-2026-11-18.json)
  · [Render metadata](https://pub-07aea3ace26c452cbe6112df32e49088.r2.dev/kimr/2026111712/meta.json)

## KIM regional Korea zoom (`kimz`), translucent barbs

- `kimr` renders its charts a second time on `domains.korea` (same Lambert projection; corners about 120.0E 31.1N
  to 132.6E 42.85N, almost all of North Korea, as the owner's KMA RDAPS sample; about 1.25 km per pixel) for
  `site.zoom.charts` (500, MSLP + precipitation, 2 m temperature, 925 / 850 with MFC, 700 with RH).
  Images are stored with the kimr run as `z_<chart>_<step>.png`; the manifest lists a virtual model `kimz`
  (`data_model = "kimr"`, `chart_prefix = "z_"`) that the app shows as "KIM 지역 확대". No extra API Hub request.
  Estimated cost: 9 x 29 images per run, about +0.4 GB for 12 runs.
- Wind barbs are drawn with alpha 0.7.
- 500 hPa wide map: 0-60N, 100-180E (was 10-50N). WPSH search box: 5-40N, 115-160E (was 10-40N, 120-180E),
  shown on the wide chart as a thin dotted frame (`wpsh.box_on_wide_map`). Owner decision 2026-11-16.

### M8 follow-up: KIM regional cut (measure run 43996972187, 2026-11-17)

`lon1=119&lat1=31&lon2=133&lat2=43` on a KIMR request returns the Lambert 3 km grid cut to 442 x 443 points
(first point 30.76N 119.04E; whole domain 1050 x 840), about 0.6 MB per field instead of 2.65 MB (estimated from
24-bit simple packing). Owner decision: keep downloading the whole domain, because the "KIM 지역" charts use the
wide map; the cut stays available if the daily quota becomes the limit again.

## KMA quota protection (2026-11-22)

What happened (measured from the publish logs of 2026-11-22, KST day): the KIMR 2026112200 run took about
2 h 50 min to download (slow hub, several fields retried), and the job hit its 180 min timeout while the run was
almost finished. Nothing was kept, so the next job downloaded the whole run again (about 1.9 GB each, estimated
from 725 fields x 2.7 MB) and ran into the hub's daily limit (HTTP 403). On the following poll the completeness
probe got that 403, the probe loop stopped with `break`, the stored KIM runs fell out of the window and the prune
step deleted all of them.

Changes:
- KIM runs are resumable. Each finished step's images are uploaded at once together with
  `<model>/<run>/partial.json` (rendered steps and their results); the usage count is saved with every step.
  A later poll treats a run with `partial.json` as complete (no probe), renders only the missing steps and
  publishes the run with `meta.json` (kept up to `max_fetch_age_h + resume_extra_h`). Orphan cleanup skips it.
- A KIM run starts no new step after `--hard-stop-min` (165 min, the job times out at 180 min); it stops with
  "deferred" and continues next poll. A hub 403 in the middle of a run also keeps the finished steps.
- A 403 during probing only ends the probing; stored runs stay in the window and are not deleted.
- Stored KIM runs are not re-rendered for a `render_version` bump (a re-render downloads the whole run again);
  new runs get the new version.

## Snow area and snow chart (render_version 11, AIFS 12)

Owner request 2026-11-22, after KMA's snow charts. ECMWF models only (IFS 0.25, IFS 0.1, AIFS Single): their open
data carry `2d` and `gh` at 1000 hPa (checked in the 2026112200 +24 h index files). KIM is unchanged (2 m
humidity would cost extra KMA API quota).
- Method: the KMA water-equivalent ratio (KMA Numerical Modeling Center, 2024 numerical weather chart manual;
  owner 2026-11-22). Snow where any of its three snow-occurrence conditions on 2 m temperature and 2 m RH (from
  `2d`) holds; ratio = a log(RAIN) + b, with a, b chosen by T850 > -8 C and Tsfc > 0 C, RAIN = 3 h precipitation,
  negative ratios cut to 0; snowfall cm = RAIN x ratio / 10. `log` is the natural log for now (owner: compare
  with the KMA charts, `charts.snow.ratio_log = "log10"` otherwise). 6 h windows (+144 h on, AIFS) use the 3 h
  mean rate for the ratio (our assumption).
- `msl`: magenta hatching = expected snowfall area (as named in the KMA manual): at least half of the window's
  precipitation (>= 0.1 mm) is model snowfall `sf` (owner 2026-11-22: not the ratio's snow conditions).
- `snow` (from the first window; no image at +0 h): snowfall (cm), 925 hPa height and temperature, 1000-850 hPa
  thickness 1270-1310 m (1290 m bold), surface wet-bulb 1.2 C line (psychrometric, from 2t, 2d, sp), 925 hPa wind.
- Tried and dropped for the snow chart: model snowfall `sf` (the KMA ratio is the method of the KMA charts) and the
  IFS snow depth `sd` (packed in 0.03125 m w.e. steps: 3 h changes are 0 or about 20 cm).
- Sample 2026112200 +27 h IFS over 37-43N 124-131E (measured): snowfall max 4.4 cm with ln, 5.9 cm with log10.
- KMA rain cut-off (same manual) for the snowfall: no snow below 0.25 mm per 3 h (Nov-Apr; 0.5 mm May-Oct; a
  6 h window compares its 3 h mean). Without it, the packing noise of the cumulative tp (about 0.12-0.24 mm at
  +123 h, measured 2026112206) times the large ratio of small amounts (about 26 at 0.1 mm) drew ~0.3 cm over wide
  dry areas (382 of 401 such cells had less than 0.25 mm). render_version 12 (AIFS 13). Chip renamed '강설' and
  placed after '지상·강수'.

## WPSH search to 170E and secondary centre (render_version 13, AIFS 14)

Owner 2026-11-23: the line was missing at many steps (published manifest, measured: IFS 17-28 of 65 steps per run,
AIFS 9-12 of 41). Search box east edge 160E -> 170E (also the dotted frame on the wide chart). When no centre
dominates +/- 5 deg, the strongest centre within +/- 2.5 deg is used ("2nd centre" in the label). IFS 2026112200,
12 steps that had no line (measured): 170E alone draws 8, 170E + secondary centre draws all 12.
- 500 hPa wide map: "H" and the height (gpm) at each local maximum within +/- 5 deg (`charts.gz.high_centres`,
  owner 2026-11-23, after the KMA charts). render_version 14 (AIFS 15).

## KIM regional: snow area and snow chart (owner 2026-11-23)

KIM regional now also fetches `RH2` (2 m RH, %) and `SNOW` (accumulated grid-scale snow and ice, mm): 27 fields per
step, 783 calls and about 2.1 GB per run (estimated from 2.7 MB per field), about 4.2 GB for two runs a day (guard lowered to 4.5 GB, owner 2026-11-23: another app shares the key). No
1000 hPa height (no thickness lines on the KIM snow chart; owner's choice to save quota). The dew point for the
wet-bulb line is derived from T2 and RH2. Names measured with measure run 45080956733 (2026112212 +24 h, small box:
T2, RH2 and SNOW all GRIB). Two earlier checks returned "# ERROR: CGI 호출 오류" even for T2, also in the production
URL format; both ran while a publish job was downloading KIM with the same key, so the hub seems to refuse
concurrent requests from a second client (inferred). Stored KIM runs are not re-rendered; the next runs get the
charts. Not yet run on real KIM data here: the [kim] CI smoke would cost about 0.35 GB of today's quota.

## KIM: Korea map only, lon/lat cut download (owner 2026-11-24)

2026-11-23 00Z KIMR was not fetched: the own guard refused it ("used 1838 calls / 2.55 GB, next needs about 783 calls
/ 2.11 GB (limits 16000 / 4.5 GB)", publish run 45165805681). The 22 12Z run is fetched after KST midnight, so two
whole-domain runs fall on one KST day: 2.51 GB were counted for the 22 12Z run alone (25 fields; retries included),
so two runs did not fit under 4.5 GB. Owner: drop the wide KIM charts and keep the Korea map. KIM regional
(button "KIM 한반도") is now drawn on the Korea map (domains.korea) and downloads only `kma.box` = 117-135E,
29.5-44.5N; the separate zoom model (`kimz`) is gone. Size per field is estimated at about 1 MB (the measured
119-133E / 31-43N cut was 0.6 MB), so about 0.8 GB per run. Stored runs keep their wide images until they age out.

## 500 hPa vorticity, theta-e charts, KIM hourly surface charts (owner 2026-11-24)

- 500 hPa (default map, every model): relative vorticity shaded in E-5/s (2-20, redder for larger values, as the KMA
  500 hPa vorticity chart; 0.25 deg gaussian smoothing on the 0.1 deg and KIM grids), temperature as red dashed
  isotherms (`charts.gz.temp_style_by_level` 500 = "V"). The 500 hPa wide map keeps the temperature fill.
- New charts `ept925`, `ept850`, `ept700` for every model: equivalent potential temperature (Bolton 1980, from t and q;
  owner colours (render_version 16, AIFS 17): white below 279 K (grey lines only), 3 K bands with the colour family
  changing every 18 K (pink-purple, purple-blue, blue-yellow-green), red line at 333 K, yellow to red above, black > 360 K;
  where only RH is given (IFS / KIM 700 hPa) q is derived with the IFS saturation formula), 3 K lines, height and wind.
  No extra download. render_version 15 (AIFS 16): stored ECMWF runs are re-rendered from the mirrors; stored KIM runs
  are not (new KIM runs get the charts).
- KIM regional: `msl` and `t2m` every hour to +84 h (`data.hourly`). Between the 3-hourly steps only MSLP, sp, RAIN,
  T2, U10, V10 and SNOW are fetched (7 calls per step, 56 steps: 392 calls). Hourly output measured on 2026112400
  (measure run 45358318237: +1, +2, +40, +83 h, MSLP/RAIN/T2/SNOW/U10 all GRIB). The precipitation on the hourly charts
  is the last 3 h (+1 h / +2 h: since the run start), like the 3-hourly ones.
- Manifest: a run lists the surface-only steps in `hourly_steps`; charts drawn there carry `"hourly": true`. Apps
  before 0.8.0 ignore both and show the 3-hourly steps only.
- Cost (estimated from 1.22 GB measured for 2026112400 = 1.56 MB per field): about 1.18k calls and 1.83 GB per KIM run,
  two runs about 3.7 GB per KST day (guard 4.5 GB). R2: three more charts per step, about 150 KB each (local smoke render).

## KMA daily count rollover fix, vorticity on the 500 hPa wide map (2026-11-25)

- 2026112412 KIM was refused by the guard ("used 2542 calls / 3.02 GB, next needs about 1175 calls / 1.88 GB",
  publish 2026-11-24 21:01 UTC). 3.02 GB was the 24th's (KST) count: a job that loaded it before KST midnight saved
  it under the 25th's key, so the new day started at 3.02 GB. The count now carries its KST day; it restarts at
  KST midnight (also inside a running job), and a file without a day is not trusted (count from zero, probe times kept).
- Correction: the `probes` values in `_ops/kma-*.json` are probe times (Unix seconds), not byte counts. The earlier
  "1.22 GB for 2026112400" was read from them and is wrong; that run's real cost is not separately known (the 24th's
  3.02 GB also holds the whole-domain 2026112312 run). `est_bytes_per_field` 1.6 MB stays as a conservative value.
- 500 hPa wide map: relative vorticity and red dashed isotherms like the default map (owner). render_version 17
  (AIFS 18).

## AIFS-ENS plume (owner 2026-11-25)

- Model `aifsens` ("AIFS-ENS"): one graph per point and run, `plume_<id>`: every member's 850 hPa and 2 m temperature
  at Seoul, Cheongju, Daejeon, Gangneung, Daegu, Gwangju, Busan and Jeju (`ens.points`, bilinear), control blue, mean
  black, 10-90 % band, 0 C line and a fainter -8 C (850 hPa) line. All points come from the same download (8 images,
  about 1 MB per run; 198 s for run 2026112418 from the build sandbox, measured).
- Data (measured 2026-11-24/25): `aifs-ens/0p25/enfo`, control `cf` + 50 members `pf`, steps 0-360 h every 6 h for
  00/06/12/18Z; final step on the Google mirror about run + 6.3 h; Azure answers 409. Only t850 and 2t are fetched
  with HTTP range requests from each step's index (about 0.3 MB per member and field, about 30 MB per step, about
  1.9 GB per run); a whole run took 150-183 s from the build sandbox. The grids start at 90N, 180E/W.
- The run's point values are kept as `series.json` next to the image. Map models never list graph charts.

## ECMWF ENS plume (owner 2026-11-25)

- Model `ifsens` ("ECMWF ENS"): the same plume charts (same chart ids and cities as AIFS-ENS, so the app keeps the
  city when switching) from `ifs/0p25/enfo`. Measured 2026-11-24/25: one `ef` file per step holds the 50 perturbed
  members (no control at 0.25 deg); steps 0-144 h every 3 h, then 150-360 h every 6 h for 00/12Z, 06/18Z only to
  +144 h, so only 00/12Z are used; final step about run + 8.5 h (Google); t850 / 2t about 0.3 MB per member and field.
  About 85 steps x 100 messages, 2.5 GB per run (estimated). App 0.8.2.

## KIM hourly precipitation, radar legend, hourly snowfall (owner 2026-11-25)
- `kimr`: precipitation window 1 h at every step (`tp_short_accum_h = tp_long_accum_h = 1`), so the msl chart shows
  mm/h. The window start is the previous hour, which is always fetched before (steps run in order), so no extra calls.
- `kimr` only: `charts.tp` uses a KMA radar-style legend (0.1-150 mm/h, 23 levels; colours read by eye from the
  owner's KMA RDAPS max-reflectivity chart). Other models keep their 3/6 h legend.
- Snow chart every hour: `data.hourly.pl` adds 925 hPa gh/t/u/v and 850 hPa t to the hourly steps, plus `r2`;
  `snow` joins `data.hourly.charts`; per-model `min_step = { snow = 1 }`. 1 h snowfall uses the KMA ratio with the
  1 h rain scaled to its 3 h rate (the existing assumption for 6 h windows; estimated, not validated against KMA).
- Cost (estimated): 783 + 56 x 13 = 1,511 field calls per run. `est_bytes_per_field` lowered from 1.6 MB to 0.9 MB:
  measured 2026112412 with the box cut, 1.16 GB for 1,736 requests (0.67 MB each). About 1.4 GB per run, 2.8 GB/day
  for two runs, below the 4.5 GB guard (estimated).
- Stored KIM runs are not re-rendered; the change appears from the next run.

## Ensemble meteograms: own model row, 500 hPa height panel (owner 2026-11-25)
- App 0.8.4: AIFS-ENS and ECMWF ENS buttons move to a second row ("앙상블") below the map models. While an
  ensemble model is open only the city chips show (서울 … 제주) and the time controls, step grid and WPSH toggle
  are hidden (one image per run); past-run comparison stays. Switching back to a map model returns to the last
  map chart; an ensemble model always opens its newest run.
- Plumes get a 500 hPa height panel on top: IFS ENS `gh` (gpm), AIFS-ENS `z` / g (param names measured in the step
  indexes 2026-11-25). Ensemble `render_version` 2, so stored ensemble runs are re-rendered once.
- Local run ECMWF ENS 2026112412 with the third field: 179 s, 8 images 1.16 MB (measured; before 114 s, 0.87 MB).
- 500 hPa reference lines: 5880 gpm red and 5400 gpm blue, dashed at 50 % opacity, drawn only when they fall inside
  the panel's data range (the axis is not stretched to show them). `ens.ref_lines` entries are
  `[value, opacity, colour]`. Ensemble `render_version` 3.
- Past-run comparison for meteograms: compares every run's graph (step 0) instead of one valid time, which only
  ever found the current run (measured in the app: 1 frame before, 11 AIFS-ENS runs after).

## 예보 tab: KMA 초단기예보 / 단기예보 images (owner 2026-11-26)
- Source: KMA API Hub grid images `nph-dfs_vsrt_ana2` (초단기) and `nph-dfs_shrt_ana_5d` (단기), copied unmodified.
  Measured with measure M9 (rounds 1-3, 133 calls):
  - PNG, 601x1034 at size=600 (16-64 KB), 1001x1723 at size=1000 (~90 KB); 1-3 s per request (some up to 17 s).
  - Bare maps: no legend, title or time; no `mode`, `legend` or `title` value changes that.
  - Elements: 초단기 T1H RN1 SKY PTY REH WSD LGT VEC; 단기 T3H POP PTY SKY REH WSD R06 S06 TMN TMX VEC WAV.
    TMP, PCP, SNO (the current 단기예보 names) return a 172-byte blank on this API.
  - Ranges: 초단기 +0..+5 h; 단기 02-17 KST issuances to 24 h of D+3, 20/23 KST issuances to 24 h of D+4 (the same end).
    An unavailable valid time returns a grey "no data" map, identical for every element of a product.
  - tm_fc: 초단기 snaps to 10-minute issues; 단기 must be an issuance hour (09:00 returns "no data").
- Publisher `render/dfs.py`, `python -m render dfs`, workflow `forecast.yml` (main only, every 30 min, its own
  concurrency group, so kim.yml's KIM runs never hold it back). Per product it fetches the newest issuance not
  yet published: the "no data" map first, then the first element until the first "no data" hour, then the other
  elements up to that hour. Layout `dfs/<product>/<issuance KST>/<element>_<valid KST>.png` + `dfs/index.json`;
  the newest 2 issuances per product are kept.
- Budget (estimated): a 단기 issuance ~700 calls (8 elements x ~87 h), 8 a day; 초단기 ~38 an hour; about 6,500
  calls and 0.6 GB a day. Own cap 8,000 calls/day (`_ops/kmadfs-<day>.json`); bytes checked together with the KIM
  count against kma_quota.max_bytes_per_day, and the KIM guard in kim.yml now adds this job's calls and bytes.
  Own Class A counter `_ops/dfs-<month>.json`, checked against the monthly limit minus the publish job's count.
- App 0.9.0: "예보" model button after KIM 한반도, chips 기온 강수형태 하늘상태 강수량 강수확률 적설 습도 풍속.
  The run selector holds the two modes: "초단기 → 단기" (초단기 where it has the hour, then 단기) and "단기만".
  Frames start at the current hour. No legend (none available from the API).
- Also fixed: the "이미지를 불러오지 못했어요" banner stayed after a later image loaded.
- Fix (2026-11-26, first real test from the sandbox): connection resets lost the "no data" reference image, so the
  range end could not be detected and the grey +6 h 초단기 map was taken as a frame (3 colours vs ~55, measured).
  A product whose reference cannot be fetched is now skipped for that run; 4 tries per request instead of 3.
- First real runs of forecast.yml (2026-11-26, measured):
  - Run 1 hit the 25-minute job limit during the 단기 issuance (~700 calls). The hub dropped many connections
    with 4 parallel requests, and some requests waited the full 60 s. Nothing was uploaded, because images went up
    only at the end, and the usage count was lost with the job.
  - Run 2 published 초단기 11:00 (36 images, 38 calls, 123 s). It reported 단기 11:00 as "not available":
    단기 starts at issuance + 1 h, and the probe stopped at the empty 11시 map.
  - Fixes:
    - Leading "no data" hours are skipped (up to 3).
    - Each element is uploaded, and `dfs/index.json` and the usage count saved, as soon as it is done.
    - Unfinished elements resume in the next run. Issuance entries carry first/last/done/complete.
    - Parallel 2, request timeout 25 s, 30-minute time budget, job limit 45 min.
    - App: per element, the newest issuance that has it. A mode with no images yet shows a message instead of
      breaking.
