import pytest
from fastapi.testclient import TestClient

from app.core import audit
from app.core.memory import remember, search
from app.models import AuditLog

pytestmark = pytest.mark.integration


def test_health_reports_database_and_pgvector():
    from app.main import app

    body = TestClient(app).get("/health").json()
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["database"]["pgvector"]
    assert body["checks"]["redis"]["ok"] is True


def test_config_endpoint_lists_app_yaml():
    from app.main import app

    body = TestClient(app).get("/api/config").json()
    assert "app" in body["files"]


def test_memory_search_finds_by_meaning(db_session):
    target = remember(db_session, "precedent", "Quick-commerce grocery apps are clones, reject", {"v": "fail"})
    remember(db_session, "precedent", "Developer tooling for GPU scheduling is novel", {"v": "pass"})

    hits = search(db_session, "precedent", "10-minute grocery delivery startup", k=1)

    assert hits[0][0].id == target.id
    assert target.usage_count == 1


def test_answers_update_in_place_but_precedents_keep_history(db_session):
    a1 = remember(db_session, "answer", "What is your notice period?", {"answer": "60 days"})
    a2 = remember(db_session, "answer", "What's your notice period?", {"answer": "30 days"})
    assert a1.id == a2.id and a2.content == {"answer": "30 days"}

    p1 = remember(db_session, "precedent", "Uber is a step up from Pfizer for analytics", {"v": "pass"})
    p2 = remember(db_session, "precedent", "Uber is a step-up from Pfizer for analytics", {"v": "pass"})
    assert p1.id != p2.id
    assert p1.status == "superseded" and p1.superseded_by_id == p2.id


def test_audit_record(db_session):
    audit.record(db_session, "m0", "selftest", "Wrote a test entry", detail={"why": "test"})
    db_session.flush()
    assert db_session.query(AuditLog).filter_by(module="m0").count() == 1
