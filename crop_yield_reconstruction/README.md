# Crop-yield dataset reconstruction — Task 1

Reproducible extraction, reconstruction, provenance, and quality audit for the Indian district-year-crop panel described in [sid09008mane/crop-yield](https://github.com/sid09008mane/crop-yield), especially its [DATASET_README](https://github.com/sid09008mane/crop-yield/blob/main/DATASET_README.md) and original [`fetch_*` / `build_dataset.py`](https://github.com/sid09008mane/crop-yield/blob/main/build_dataset.py).

**Status of this handoff:** Code is written and unit/integration-tested on synthetic fixtures. The authentic 198,369-row dataset is available upstream; **this archive does not purport to contain a freshly fetched, fully reconstructed 198,369-row CSV**, because the execution environment creating this package cannot connect to external data servers. Run the commands below in an Internet-enabled environment to get the CSVs. Do not mix the synthetic test fixtures with real records.

## 1. Reconstruct data from sources

Requires Python 3.10+; on macOS/Linux/Windows run:

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows:    .venv\Scripts\activate
pip install -r requirements.txt
python reconstruct.py all --data-dir data --source auto
python -m pytest -q
```

`all` runs **fetch-icrisat → fetch-pesticide → fetch-weather → build → audit**. Resumable downloads are cached. In particular NASA POWER is queried once per district centroid (typically over 300 requests). Missing or failed requests are reported; it will not silently invent API values.

If ICRISAT's DLD API is down, `--source auto` uses the original repository's raw files as a **labelled fallback**, not as an independent ICRISAT re-download. `--source api` requires primary endpoints; `--source mirror` uses the upstream Github raw ICRISAT files and pesticide data. The FAO ZIP is tried first in `auto`, then the GitHub mirror if unavailable. Exact endpoint URLs, SHA-256, and timestamps are recorded in `data/reports/source_manifest.json`.

To run stage-by-stage:

```bash
python reconstruct.py fetch-icrisat  --source auto
python reconstruct.py fetch-pesticide --source auto
python reconstruct.py fetch-weather --weather-start 1981 --end 2017
python reconstruct.py build --start 1966 --end 2017
python reconstruct.py audit
```

For a partial networking smoke test: `python reconstruct.py fetch-weather --max-districts 2` — but **do not** treat the resulting CSV as complete. Rerun without `--max-districts` afterwards.

Download the repository's *existing* two CSVs for provenance and numeric comparison:

```bash
python reconstruct.py fetch-reference
python reconstruct.py compare-reference
```

These CSVs are publicly viewable at:

- [Existing 15-column dataset](https://github.com/sid09008mane/crop-yield/blob/main/crop_yield_dataset.csv)
- [Existing provenance dataset](https://github.com/sid09008mane/crop-yield/blob/main/crop_yield_dataset_with_provenance.csv)

Reference/production differences are possible: NASA precipitation uses actual days per year here, FAOSTAT may have revised historical estimates, source releases can change, and a new run may have different missingness. **Do not demand exact row/byte equality before examining its cause.**

## 2. Outputs

| File | Grain | Intended use |
|---|---|---|
| `data/processed/crop_yield_research_panel.csv` | district × calendar year × crop | Recommended modeling foundation: retains missing covariates rather than using future data to fill historical years; contains district IDs, source flags, and soil-taxonomy proxy markers. |
| `data/processed/crop_yield_dataset.csv` | district × calendar year × crop | The upstream-compatible 15-column **complete-case** format. Uses disclosed climatological and historical backfills; for comparison and demonstration, **not strictly forecast-safe**. |
| `data/processed/crop_yield_dataset_with_provenance.csv` | same as complete-case | Additional source flags, state, district, district code, and pH proxy flag. |
| `data/reports/data_quality_report.json` | summary | Row counts, time coverage, missing values, key collisions, range checks, fertilizer consistency checks, and field provenance. |
| `data/reports/build_summary.json` | summary | Rows from APY, rows retained, coverage and year limits. |
| `data/reports/source_manifest.json` | source files | Download timestamp, source URL, SHA-256, bytes; preserves which source and version was actually used. |
| `data/reports/reference_comparison.json` | summary | Numeric/key comparison against repository-supplied reference (optional). |

If any source fields are missing, research panel rows remain. **Do not indiscriminately `dropna` in the research panel.** Missing-data strategy belongs inside train-only fit/transform pipelines after temporal splitting, with provenance features and missingness flags where appropriate.

## 3. Sources, definitions and API map

| Feature | Source/API | Frequency & units | Caveat |
|---|---|---|---|
| District code, names & centroids | [ICRISAT DLD](https://data.icrisat.org/dld/), `/dldAPI/apportioned/districts` | District | Historic apportioned boundaries, not modern GIS IDs. |
| Crop area + yield | `/dldAPI/apportioned/area-production-yield` | District-year-crop; yield kg/ha | Only records with strictly positive APY area and yield are included; yield is target. |
| N, phosphate, potash, total fertilizer | `/dldAPI/apportioned/fertilizer-consumption` | District-year; kg/ha of gross cropped area | P and K refer to fertilizer convention P2O5 and K2O, not elemental P and K. |
| Soil order | `/dldAPI/apportioned/soil-type` | District; categorical | Dominant order from soil composition; check strings and unknown orders. |
| Annual gauge rainfall | `/dldAPI/apportioned/monthly-rainfall` | District-year, mm | Used before NASA. |
| Long-term normal rainfall | `/dldAPI/apportioned/normal-rainfall` | District climatology, mm | Legacy fallback only (not an observed year's rainfall). |
| Temperature, relative humidity, precipitation | [NASA POWER monthly point API](https://power.larc.nasa.gov/docs/services/api/temporal/monthly/) `T2M`, `RH2M`, `PRECTOTCORR` | District centroid, `YYYY13` annual (1981–2017); C, %, mm/day before conversion | Annual precip = mm/day × 365/366; gridpoint estimates, not station measurements. |
| Pesticide use | [FAOSTAT RP](https://www.fao.org/faostat/en/#data/RP), [bulk ZIP](https://bulks-faostat.fao.org/production/Inputs_Pesticides_Use_E_All_Data_(Normalized).zip) | India-national-year, *Pesticides (total)*, *Use per area of cropland*, kg/ha | Same national value across all districts, **not** district/crop application. Exact Item + Element + Unit filtered. |
| Season | Upstream crop-to-dominant-season lookup | Kharif / Rabi / Whole Year | Dominant calendar, not observed planting season. |
| pH | Approximate area-weighted pH from soil order as in original repository | District-only numeric proxy | **Not measured pH**, and should ideally be replaced with trusted measured or geospatial soil indicators. |

FAOSTAT current release contains data through 2024; the reconstruction restricts the prediction panel to the repository's **1966–2017** crop-yield window.

### Additional sources worth testing in a *separate, versioned data improvement*

- [CHIRPS v3 precipitation](https://www.chc.ucsb.edu/data/chirps3): 0.05° rainfall fields since 1981. District polygon aggregation is preferable to using district centroid only, but must harmonize historic boundaries first.
- [ISRIC SoilGrids](https://docs.isric.org/globaldata/soilgrids/): quantitative soil pH, N and other properties. **The REST API was reported temporarily paused in 2026**; use documented stable gridded/WebDAV/WCS alternatives when accessible. Static estimates are not historical district-year fertilizer inputs.
- ICRISAT irrigation, cropping intensity, land use and other agricultural variables: add only after checking historical availability and feature-availability-at-inference for the proposed frontend.

These are *optional improvements*, **not secretly merged** into the reference-reconstruction CSV. A district-mean/polygon extraction would need geospatial boundaries and separate ETL.

## 4. Dataset audit and time-series feasibility

Repo documentation claims ~**198,369 rows, 309 districts, 20 states, 52 years (1966–2017), 21 crops**. These are **reference expectations, not guaranteed invariant counts**. The audit checks what the downloaded data really contain instead of fabricating a match to those numbers.

Before modeling:

1. Check district codes and unique keys `(DIST, Year, Crop)`. District naming changes can create collisions if you use `Region` alone. Compare assembled rows against APY yield records.
2. Inspect missingness **by year/crop/district and by source**. The legacy 0-null count is achieved partly by complete-case filtering plus historical proxy/backfills, **not** proof of measurement quality.
3. Identify temporal information availability. `1966–1980` weather backfills use 1981–1990 climatology, and pre-1990 pesticide legacy values use 1990 data: both include information from after the prediction year.
4. Define prediction timing: an annual rainfall/temperature feature cannot be input in an **early-season** yield forecast unless it is a forecast rather than end-of-year observation. Historical experiments using annual realized weather measure retrospective/end-of-season performance.
5. Use temporal split by *year*, e.g. development train ≤2007, validation 2008–2012, final test 2013–2017, provided row counts and availability justify. Fit scalers/encoders/imputation/feature selection only on training years. For robust tuning, use rolling/expanding year-based CV with a gap if relevant. Never randomly shuffle district-crop rows across years.
6. Features based on historical yields, rolling stats, or target encoding must be shifted by ≥1 full year per district/crop; do not compute rolling averages including current-year yield. Features for one district-year are shared across crops, so split at **year** (not row), and cluster uncertainty by district/time where possible.
7. If deploying with **current** user-entered soil, rainfall and fertilizer inputs, document the training–serving mismatch. A legacy 2017 model does not automatically support modern districts or seasons.

## 5. Failure handling, transparency, and operational notes

- Fetch functions retry transient HTTP failures, reject invalid payload schemas, and retain resumable weather progress after each successful district.
- API mirror usage is visible in `source_manifest.json`. Primary ICRISAT URLs originate from the reference repository's original scripts; access may depend on server state and may require HTTP in some environments.
- The script raises on duplicate join keys to prevent silent row multiplication; it raises on unexpected APY columns instead of dropping affected crops.
- All timestamps/hashed artifacts refer to the exact source fetched. The FAOSTAT bulk file may be updated in-place; to reproduce later, archive the ZIP and manifest.
- Weather lacks 1966–1980 annual measured values in NASA POWER. In the **research** CSV, they remain missing; the **legacy** CSV marks them as `NASA_1981_1990_future_climatic_normal` if backfilled.
- `data/reports/data_quality_report.json` is a technical audit, not a quality certificate. Inspect extreme yield and fertilizer observations, unit changes, unexplained anomalies, and soil string parsing manually.

**Scope:** This archive handles Task 1 only (data fetching, rebuilding, source provenance, preliminary audit). EDA, feature engineering, chronological validation and model/backend/frontend work are deliberately not claimed as implemented.

### Additional audit CSVs

After `audit`, the `reports` directory also contains `coverage_by_year.csv` (district, crop and observed-covariate counts), `missingness_by_year.csv` (fraction missing per variable and year), and `source_distribution.csv` (how many legacy rows came from each source category). `reconstruction_inputs.json` fingerprints the **exact raw input files and produced CSVs** for reproducibility.

**Calendar alignment caution:** ICRISAT's crop `Year` may refer to an agricultural year rather than the calendar-year window used by NASA POWER; this project mirrors the reference's year key to reproduce it, but research-grade **seasonal forecasting should validate the crop-year convention and join weather by actual sowing/harvest months**. Annual full-year weather may leak information unavailable at the point a farmer asks for a yield forecast.
