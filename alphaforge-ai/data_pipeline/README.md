# data_pipeline
Turns `../../alphaforge_starter/*.csv` into the curated bundle the backend reads. The contract (file names, fields, processing rules, sanity checks and the choices made here) is in [`docs/data_contract.md`](../docs/data_contract.md).

Standard library only (Python 3.9+). Run from the repository root:

```bash
python3 data_pipeline/build_bundle.py                       # full bundle -> data_pipeline/out/
python3 data_pipeline/build_prices.py                       # price files only -> the fixture bundle's prices/
backend/.venv/bin/python -m pytest data_pipeline/tests      # checks against the raw CSVs and the brief's persona tests
```

`build_bundle.py` writes `assets.json`, `asset_metrics.json`, `correlations.json`, `macro_snapshot.json`, `data_quality_report.json`, `prices/` (via `build_prices.py`), the feature-engine inputs `fx_usdcad.json`, `macro_series.json` and `dividends.json`, and `asset_features.json` (via `backend/alphaforge/features`). Ingest outputs already in the folder (`live_extension.json`, `fundamentals.json`, `signals.json`) are kept and used; add them with `python -m alphaforge.ingest.run` from `backend/` (see the main README). Point the backend at it with `ALPHAFORGE_DATA_SOURCE=dir:/abs/path/to/data_pipeline/out`, or upload the folder to `s3://alphaforge-data-<account>/curated/`. Re-run it whenever the starter CSVs change; `out/` is generated and not committed.

Not built yet: Parquet output for Athena.
