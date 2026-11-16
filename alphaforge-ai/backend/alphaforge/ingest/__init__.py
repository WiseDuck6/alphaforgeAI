"""Ingest jobs: live prices and rates, SEC EDGAR filings and fundamentals, official statements and news.

Every source is allow-listed in alphaforge.ingest.sources. Jobs write into the curated bundle
(local folder or S3) and then rebuild asset_features.json.
"""
