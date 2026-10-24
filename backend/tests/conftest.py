import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session


def _database_reachable() -> bool:
    try:
        from app.db import get_engine

        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    if _database_reachable():
        return
    skip = pytest.mark.skip(reason="database not reachable (run inside the compose stack)")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def db_session():
    """A session whose writes are rolled back after the test."""
    from app.db import get_engine

    conn = get_engine().connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture(autouse=True)
def _no_real_employers(monkeypatch):
    """Your real employers (from your profile) never block test companies; the employer tests set their own."""
    from app.modules.m1_companies import employers

    monkeypatch.setattr(employers, "_from_profile", lambda: [])
