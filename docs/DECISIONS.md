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
| 2026-11-14 | 300/200 hPa wind | Wind-speed shading: grey < 50 kt, yellow 50-75, orange 75-100, red 100-150, purple > 150 (each band darkens; bands join without jumps back to light); isotherms dark grey dashed; black coastlines. | Owner request. |
| 2026-11-14 | MSLP interval | Isobars every 2 hPa (labels every 4 hPa, bold every 20 hPa). `render_version` 3. | Owner request. |
