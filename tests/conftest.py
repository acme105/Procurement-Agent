import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Point every module at a throwaway state folder."""
    from app import config, live_rfx
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live_rfx, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(live_rfx, "RFQ_DIR", tmp_path / "live" / "rfqs")
    monkeypatch.setattr(live_rfx, "CURRENT_FILE", tmp_path / "live" / "current.txt")
    return tmp_path
