# CLAUDE.md

Full project brief: [docs/SPEC.md](docs/SPEC.md). Read it first.

## Working rules
- The owner is a weather-observation practitioner at KMA, not a professional software engineer.
  Reply to the owner in Korean. Code, comments, commit messages and repo docs are in English.
- Work on a branch and open a pull request. Never push to or merge into `main`; the owner reviews and merges.
  Anything that publishes (GitHub Pages deploy, releases) runs only from `main`.
- Verify, don't assume. Items listed as UNVERIFIED in SPEC.md section 9 must be measured; label every
  reported number "measured" or "estimated". If a measurement contradicts the brief, stop and tell the
  owner instead of silently changing the design.
- Do not expand scope beyond the current stage.
- This repository is PUBLIC: never commit secrets, tokens, personal data, KMA-internal material or
  screenshots (reference screenshots pasted in chat are for visual reference only).
- Data-dependent steps that the sandbox cannot reach run in GitHub Actions; results go to the job summary
  and artifacts.
- Code licence: Apache-2.0 (LICENSE); data/charts: CC BY 4.0 (NOTICE). Never generate or commit a signing keystore.

## Layout
- `render/` renderer package; `render/config.toml` is the single config (domains, charts, thresholds, style).
- `python -m render {poll,render-run,latest}`: pipeline (`render/pipeline.py`), manifest (`render/site.py`),
  storage with the size / request guard (`render/store.py`, Cloudflare R2). Models: `[[models]]` in the config
  (`config.for_model`). See `docs/STAGE3.md`.
- `app/`: viewer (`app/www`, plain JS) + Capacitor Android project (`app/android`); see `docs/STAGE2.md`.
- Workflows: `publish.yml` (main only: poll, render, upload to R2), `pages.yml` (main only: web app to Pages), `ci.yml` (branches: tests + smoke render),
  `android.yml` (debug APK on branches; signed release from main only),
  `measure.yml` (Stage 0 measurements, manual), `pages-test.yml` (M6, manual).
- Design notes: `docs/STAGE1.md`; decisions: `docs/DECISIONS.md`.
- `measure/` Stage 0 measurement scripts (M1-M6).
- Attribution: `NOTICE` and the on-chart credit line (`style.credit` in the config).
