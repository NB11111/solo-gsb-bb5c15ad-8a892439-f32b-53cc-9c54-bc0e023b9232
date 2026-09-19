"""测试环境：独立临时 SQLite 库，每个用例前清空所有表。"""
import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="cpc-test-")
os.environ["APP_DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["APP_PENALTY"] = "12.0"
os.environ["APP_MIN_SIZE"] = "3"
os.environ["APP_MIN_CONFIDENT_LEN"] = "6"

import pytest
from fastapi.testclient import TestClient

from app.database import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c
