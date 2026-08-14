import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobpipe.db import open_db  # noqa: E402


@pytest.fixture()
def conn(tmp_path):
    c = open_db(tmp_path / "test.db")
    yield c
    c.close()
