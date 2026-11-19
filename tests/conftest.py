import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _own_settings_folder(tmp_path_factory, monkeypatch):
    """Learning and autopilot keep their files in the user's settings folder. A test must never read or write a real one."""
    monkeypatch.setenv("AUTOCLIP_CONFIG_DIR", str(tmp_path_factory.mktemp("settings")))
    import time
    from autoclip import style, taste
    taste._cache.clear()
    taste._model_cache.clear()
    yield
    deadline = time.time() + 15                    # a background training thread must not outlive its test
    while (taste._running or style._running) and time.time() < deadline:
        time.sleep(0.05)
