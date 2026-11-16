import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Each test gets an empty local store and fresh cached settings."""
    monkeypatch.setenv("ALPHAFORGE_LOCAL_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setenv("ALPHAFORGE_AUTH_MODE", os.environ.get("TEST_AUTH_MODE", "header"))
    from alphaforge.config import get_settings
    from alphaforge.data.provider import get_provider
    from alphaforge.storage import get_store

    for fn in (get_settings, get_provider, get_store):
        fn.cache_clear()
    yield
    for fn in (get_settings, get_provider, get_store):
        fn.cache_clear()


@pytest.fixture
def provider():
    from alphaforge.data.provider import get_provider

    return get_provider()


@pytest.fixture
def persona(provider):
    from alphaforge.profiles import preset_profile

    return lambda pid: preset_profile(pid, set(provider.assets))
