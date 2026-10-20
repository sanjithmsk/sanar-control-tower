import os
import tempfile

import pytest

os.environ["SANAR_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "TOWER_DOMAIN"):
    os.environ.pop(var, None)

from app import db  # noqa: E402


def _fresh(domain):
    conn = db.connect()
    db.reset(conn, domain)
    return conn


@pytest.fixture
def c():
    conn = _fresh("pharmacy")
    yield conn
    conn.close()


@pytest.fixture
def lc():
    conn = _fresh("logistics")
    yield conn
    conn.close()


@pytest.fixture
def on():
    """Switch disruption scenarios on."""
    def activate(conn, *keys):
        for key in keys:
            conn.execute("UPDATE scenarios SET active=1 WHERE key=?", (key,))
    return activate


@pytest.fixture
def api(c):
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app)
