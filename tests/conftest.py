import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Point nrw.db at a throwaway SQLite file for this test only."""
    from nrw import db
    from nrw.config import Config

    test_config = Config(data_dir=tmp_path / "data")
    test_config.data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(db, "CONFIG", test_config)
    db.init_db()
    return db
