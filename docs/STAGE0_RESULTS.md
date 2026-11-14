# Stage 0 results (summary)

Measured on GitHub-hosted runners (ubuntu-latest, Python 3.13) on 2026-11-14; full tables were in the
`stage0-results` artifacts of the measure runs (14-day retention). "Estimated" values are computed
from measured per-step numbers.

| item | result |
|---|---|
| Install | pinned stack installs from wheels in 15-18 s |
| M1 sources | one step's 30 fields = 14.2-14.3 MB. google 1.6-6.9 s, ecmwf portal 10-12 s, azure 24-34 s, aws: HTTP 503 on every download (HEAD fine) |
| M1 publication delay | final step Last-Modified - run time: median 7.0 h (6.5-7.6 h) |
| M1 retention | portal: latest 12 runs; aws/azure/google: runs from 2024-11-14 still present |
| M2 fields | 30/30; tp in m, cumulative from step 0; GRIB packing quantum of tp 0.061 mm early, 0.488 mm after about +117 h |
| M3 render | 3.8-7.9 s per step for 14 images (single process); PNG8 average 88 KB; 10 runs about 0.39 GB (estimated) |
| M4 WPSH | 06/18Z H about 16 gpm lower than 00/12Z on average -> line drawn for 00/12Z only; no H jump > 40 gpm between consecutive 00/12Z runs |
| M5 MFC | at E-9 15-16 % of points saturate the colour bar; E-8 adopted (max 1.5 %); sign consistent at 925 hPa, inconclusive at 850 hPa |
| M6 Pages | 100 MB site deployed in 8 s; JSON and PNG served with `Access-Control-Allow-Origin: *`, `Cache-Control: max-age=600`, ETag and Last-Modified |

Decisions taken from these results: `docs/DECISIONS.md`.

## M7 per-step publication delay (2026-11-14, measured on a runner)

Last-Modified of each step's `.index` file minus run time, median over the 6 runs of each group before
14:41 UTC. Every source publishes **all steps of a run at the same time**:

| source | 00/12Z runs (+0h ... +240h) | 06/18Z runs (+0h ... +144h) |
|---|---|---|
| ecmwf portal | 7.57 h at every step | 6.45 h at every step |
| google | 7.57-7.59 h | 6.47-6.52 h |
| azure | 7.57-7.58 h | 6.45-6.46 h |
| aws | 7.57 h | 6.45-6.46 h |

Consequence: publishing the early forecast steps first would not make anything available sooner, so the
staged (72 h / 144 h / 240 h) publication idea was not implemented.
