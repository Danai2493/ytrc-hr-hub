import os
import re
import tempfile
from pathlib import Path

import pytest

_tmp = Path(tempfile.mkdtemp(prefix="hrhub-test-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp / 'test.db'}")
os.environ["STORAGE_DIR"] = str(_tmp / "files")
os.environ["AI_PROVIDER"] = "mock"
os.environ["DEMO_MODE"] = "true"
os.environ["DEFAULT_LANGUAGE"] = "en"  # assertions read English text

from app.db import Base, engine, session_scope  # noqa: E402
import app.models  # noqa: E402,F401
from app.main import app as fastapi_app  # noqa: E402
from app.seed.demo import DEMO_PASSWORD, seed_demo  # noqa: E402
from app.seed.reference import seed_reference  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with session_scope() as db:
        seed_reference(db)
        seed_demo(db)
    yield


@pytest.fixture
def db():
    from app.db import SessionLocal

    s = SessionLocal()
    yield s
    s.rollback()
    s.close()


class Client:
    def __init__(self, username: str):
        from fastapi.testclient import TestClient

        self.c = TestClient(fastapi_app)
        r = self.c.get("/login")
        self.csrf = re.search(r'name="csrf" content="([^"]+)"', r.text).group(1)
        r = self.c.post("/login", data={"username": username, "password": DEMO_PASSWORD, "_csrf": self.csrf}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", f"login failed for {username}"
        r = self.c.get("/")
        self.csrf = re.search(r'name="csrf" content="([^"]+)"', r.text).group(1)

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, files=None, **kw):
        data = dict(data or {})
        data["_csrf"] = self.csrf
        return self.c.post(url, data=data, files=files, follow_redirects=False, **kw)


@pytest.fixture
def login():
    return Client
