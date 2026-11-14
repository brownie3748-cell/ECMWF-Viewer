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
