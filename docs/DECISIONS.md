# Decisions log

Owner decisions taken after Stage 0 measurements. SPEC.md stays the verbatim brief; this file records
where the implementation deliberately differs from it.

| date | topic | decision | evidence |
|---|---|---|---|
| 2026-11-14 | MFC scale | Display div(qV) x 1e8, title "Convergence(E-8/s)" (brief: E-9). Smoothing sigma = 1 grid point. | M5: at E-9 15-16 % of points fall below -50 (bar saturates); at E-8 0.3-1.5 %. |
| 2026-11-14 | Map background | Coastlines and borders darker (coast #4d4d4d 0.65 px, borders #707070 0.5 px). | Owner review of M3 samples. |
| 2026-11-14 | tp lowest level | Shading starts at 0.5 mm (0.1 mm level dropped). | M2: GRIB packing quantum of the cumulative tp is 0.488 mm after about +117 h. |
| 2026-11-14 | gz500 temperature | Style A (colour fill + colour bar) for gz500 and gz500_wide; other levels keep style B (dashed isotherms) for now. | Owner choice from M3 A/B samples. |
| 2026-11-14 | wpsh500 line | White halo around the red line so it stays readable over the gz500 warm colour fill. | Follows from the gz500 style A choice. |
| 2026-11-14 | Code licence | Apache-2.0 for the source code; data/charts stay CC BY 4.0 via NOTICE. | Owner decision. |
| 2026-11-14 | MFC contours | Divergence contours thin grey (#6a6a6a, 0.35 px) and drawn below coastlines/borders. | Owner review: thick contours hid borders. |
| 2026-11-14 | WPSH run hours | The red line is drawn only for 00/12Z runs; 06/18Z overlays show "line: 00/12 UTC runs only". | M4: 06/18Z H about 16 gpm lower than 00/12Z on average. |
| 2026-11-14 | WPSH centre moves | Centre moves between far-apart candidates of similar height are accepted (H = max, computation continues); only H changes > 40 gpm between consecutive 00/12Z runs are flagged. The weak secondary maximum inside the box when the core is east of 180E is accepted. | Owner review of M4. |
| 2026-11-14 | Attribution | NOTICE checked against the ECMWF Terms of Use (CC BY 4.0 for public content, disclaimer); Natural Earth credit and a borders note added. | Owner supplied the Terms of Use text. |
| 2026-11-14 | Manifest | Rule-based `index.json` (schema_version 1): image URLs from a path pattern; runs list steps and per-step WPSH [H, H-50]. | Owner choice; about 10 KB for 10 runs (estimated). |
| 2026-11-14 | First deploy | Back-fill the newest 10 complete runs on the first publish run. | Owner choice. |
| 2026-11-14 | Schedule | Poll every 30 minutes (cron 7,37 * * * *). | Owner choice. |
| 2026-11-14 | Sources | Download order google -> ecmwf portal -> azure; aws not used. | M1: aws 503 on every download. |
| 2026-11-14 | App identity | App name "ECMWF 뷰어", package `io.github.brownie3748cell.ecmwfviewer`. | Owner choice (name); package id default. |
| 2026-11-14 | Web preview | The same viewer is published at `<site>/app/`. | Owner choice. |
| 2026-11-14 | APK delivery | Signed release APK as a GitHub Release, built only from main on manual run. | Owner choice. |
| 2026-11-14 | Past-run playback | Included in the first app version. | Owner choice. |
| 2026-11-14 | 925/850/700 temperature | Style C: fixed rainbow colour scale (same colour for the same temperature at every level), white isotherms every 3 deg C, black coastlines/borders; gh contours and barbs unchanged. gz500 keeps style A. | Owner request with a public reference chart (colour reference only, not committed). |
| 2026-11-14 | Surface + precipitation | `msl` now shades the 3 h / 6 h precipitation under the MSLP contours; the separate `tp` chart is no longer published (13 charts per step). `render_version` 2 re-renders the runs already on the site. | Owner request. |
| 2026-11-14 | 700 hPa RH | Continuous brown (dry) -> white (~50 %) -> teal (moist) shading over 0-100 % in 5 % steps, black coastlines; gh contours unchanged. | Owner request with a public reference chart (colour reference only, not committed). |
| 2026-11-14 | 300/200 hPa wind | Wind-speed shading: grey < 50 kt, yellow 50-75, orange 75-100, red 100-150, purple > 150 (each band starts light and darkens - owner preferred this first draft over a jump-free variant); isotherms dark grey dashed; black coastlines. | Owner request. |
| 2026-11-14 | MSLP interval | Isobars every 2 hPa (labels every 4 hPa, bold every 20 hPa). `render_version` 3. | Owner request. |
| 2026-11-15 | Retention | Keep the newest 12 complete runs (3 days) instead of 10; `lookback_runs` 18. Site about 0.48 GB on Pages (estimated from the measured ~0.40 GB for 10 runs). | Owner request. |
| 2026-11-15 | Rain/snow isotherms | Solid red isotherm at -5 C on 925 hPa and -8 C on 850 hPa over the temperature shading (`charts.gz.highlight_isotherm`). `render_version` 4. | Owner request (rain/snow discrimination aid). |
| 2026-11-15 | Chart order | Tabs grouped by level, surface first, 200 hPa last: msl, 925, 925 MFC, 850, 850 MFC, 700, 700 RH, 500, 500 wide, 300, 200. | Owner request. |
| 2026-11-15 | Hosting / new models (planned) | Owner chose to move image hosting to Cloudflare R2 (reverses the brief's "no Cloudflare") and to add IFS 0.1 deg and AIFS-single 0.25 deg. Measured on the Google mirror (run 2026-11-14 00Z): `ifs/0p1/oper` has 85 steps 0-360 h, split into `-pl` (about 380 MB at +0 h) and `-sfc` (about 80 MB) files per step; `aifs-single/0p25/oper` has 61 steps 0-360 h every 6 h and provides gh/t/u/v/q/w but no `r` (700 hPa RH must be derived from q and t). Not implemented yet. | Owner request. |
| 2026-11-15 | Storage: Cloudflare R2 | Images and `index.json` move to the owner's R2 bucket (public r2.dev URL, CORS GET/HEAD *); GitHub Pages keeps only the web app. Reverses the brief's "no Cloudflare". Guard: 9 GB cap checked before every upload, 900k Class A requests per month, job fails when either would be exceeded. | Owner request (capacity for more models; stay inside the 10 GB free tier). |
| 2026-11-15 | Models | IFS 0.25 deg (all 13 charts), IFS 0.1 deg (all except 300, 200, 500 wide), AIFS Single 0.25 deg (all 13). 12 runs each, 240 h (00/12Z) / 144 h (06/18Z). Manifest `schema_version` 2 (per-model charts and runs); the app has a model selector and keeps run and valid time when switching. | Owner choice. |
| 2026-11-15 | AIFS / 0.1 deg details | AIFS 700 hPa RH derived from q and t with the IFS mixed-phase saturation formula; AIFS precipitation is always 6 h (6-hourly output); AIFS tp is kg m-2 and is converted to m on read (measured). 0.1 deg: MSLP smoothed with a 0.2 deg gaussian and MFC divergence with 0.4 deg before contouring (smoke render showed terrain noise). | Measured on run 2026-11-15 00Z. |
| 2026-11-15 | Processing order | Within a cycle IFS 0.25, AIFS, then IFS 0.1 (slowest last); runs published while a job works are re-checked after every upload and processed first. Each run still appears in the app as soon as it is uploaded. | Owner request. |
