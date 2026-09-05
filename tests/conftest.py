import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CDISC_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def lexicon():
    from campaign_discovery import config
    return config.lexicon()


@pytest.fixture()
def sources_cfg():
    from campaign_discovery import config
    return config.sources()
