"""The curated-bundle contract that data_pipeline must satisfy later."""

import json
import shutil

from alphaforge.config import FIXTURE_DIR
from alphaforge.data.provider import load_from_dir


def test_fixture_bundle_is_valid_and_marked_placeholder(provider):
    st = provider.status()
    assert st["is_placeholder"] is True
    assert st["asset_count"] == 31
    for t in provider.assets:
        m = provider.metrics(t, "5y")
        assert m is not None and m["ann_vol"] is not None and m["max_drawdown"] <= 0


def test_window_fallback_is_reported(provider):
    window, note = provider.resolve_window("3y")
    assert window == "5y" and "not in the loaded data" in note


def test_correlation_matrix_replaces_heuristics(tmp_path):
    for name in ("assets.json", "asset_metrics.json"):
        shutil.copy(FIXTURE_DIR / name, tmp_path / name)
    (tmp_path / "correlations.json").write_text(json.dumps(
        {"windows": {"5y": {"tickers": ["NVDA", "QQQ"], "matrix": [[1.0, 0.81], [0.81, 1.0]]}}}))
    p = load_from_dir(tmp_path)
    assert p.correlation("NVDA", "QQQ", "5y") == (0.81, False)
    _, heuristic = p.correlation("NVDA", "GLD", "5y")
    assert heuristic is True
    assert "signals.json" in p.status()["files_missing"]
