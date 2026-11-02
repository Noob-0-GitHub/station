"""Live tests: real paid APIs and a real browser. Never part of the main suite.

Run on purpose, with the real keys in `secrets.dir`:

    STATION_LIVE=1 .venv/bin/python -P -m pytest -q tests_live --basetemp=/root/temp/station-live

Each test uses a fresh runtime under the base temp directory, so no real Station state is
touched. Costs per full run, at list price: well under 0.05 USD (a few embeddings, a few
seconds of transcription, 2 Tavily credits). Every paid call goes through `station api` and
lands in that temporary ledger, which the tests read back.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KEYS = Path(os.environ.get("STATION_LIVE_KEYS", "/root/.local/share/secrets/station"))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("STATION_LIVE") != "1":
        skip = pytest.mark.skip(reason="live test: set STATION_LIVE=1 to call real services")
        for item in items:
            item.add_marker(skip)


@pytest.fixture
def runtime(tmp_path):
    """A fresh Station runtime that reads the real keys."""
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "station.yaml").write_text(f"secrets: {{dir: {KEYS}}}\n")
    return root


@pytest.fixture
def station(runtime, tmp_path):
    def run(*args, check=True, stdin=None):
        env = {k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "TAVILY_API_KEY")}
        env["STATION_ROOT"] = str(runtime)
        r = subprocess.run([sys.executable, "-P", "-m", "station.cli", *args], cwd=tmp_path, env=env,
                           input=stdin, capture_output=True, text=True, timeout=300)
        if check:
            assert r.returncode == 0, r.stdout + r.stderr
        return r
    return run


@pytest.fixture
def ledger(runtime):
    from station.store import Store

    return lambda: Store(runtime / "state").ledger.rows()


def need(binary):
    if not shutil.which(binary) and not Path(binary).exists():
        pytest.skip(f"{binary} is not available")
