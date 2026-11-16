import os
import sys
from pathlib import Path

import pytest

# The starter data ends on 2026-11-12. Without this, confidence would drop once the data is
# more than 10 days old, which would make results depend on the day the tests run.
os.environ.setdefault("ALPHAFORGE_STALE_DAYS", "100000")
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "data_pipeline"))
sys.path.insert(0, str(ROOT / "backend"))

STARTER = ROOT.parent / "alphaforge_starter"


@pytest.fixture(scope="session")
def bundle_dir(tmp_path_factory):
    import build_bundle

    if not (STARTER / "prices_daily.csv").exists():
        pytest.skip(f"starter data not found at {STARTER}")
    out = tmp_path_factory.mktemp("bundle")
    build_bundle.build(STARTER, out)
    return out


@pytest.fixture(scope="session")
def real_provider(bundle_dir):
    from alphaforge.data.provider import load_from_dir

    return load_from_dir(bundle_dir)
