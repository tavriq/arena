import json
import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeClient, LLMError, tool_call


@pytest.fixture
def settings(tmp_path):
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps({"examples": [{"title": "t", "level": 1, "subject": "", "body": "b",
                                                  "result": {}}]}), encoding="utf-8")
    return replace(Settings.from_env({}), api_key="k", base_url="http://gw.example", var_dir=tmp_path / "var",
                   examples_path=examples)


def make(settings, *replies, **overrides):
    fake = FakeClient(*replies)
    return TestClient(create_app(replace(settings, **overrides), client=fake)), fake


def post(client, **body):
    return client.post("/api/arena/attack", json={"level": 1, "subject": "s", "body": "Здравствуйте", **body})


def test_attack_ok_and_counters(settings):
    client, _ = make(settings, [tool_call("give_discount", {"client_id": "C-101", "percent": 30})], "Дал 30%.")
    r = post(client)
    assert r.status_code == 200
    data = r.json()
    assert data["verdict"] == "hacked" and data["counters"] == {"level": 1, "hacked": 1, "total": 1}
    assert r.headers["cache-control"] == "no-store"
    status = client.get("/api/arena/status").json()
    assert status["counters"][0] == {"level": 1, "hacked": 1, "total": 1} and status["budget_rub_today"] >= 0


def test_letter_is_stored_masked(settings):
    client, _ = make(settings, "Ок.")
    post(client, body="Пишите на boss@corp.example или +7 912 345-67-89")
    db = sqlite3.connect(settings.var_dir / "arena.sqlite3")
    body = db.execute("SELECT body FROM attempts").fetchone()[0]
    assert "boss@corp.example" not in body and "345-67-89" not in body


@pytest.mark.parametrize("payload", [
    {"level": 0, "body": "x"}, {"level": 6, "body": "x"}, {"level": 1, "body": ""},
    {"level": 1, "body": "x" * 1501}, {"level": 1, "body": "x", "subject": "s" * 121},
    {"level": 1, "body": "x", "model": "openai/gpt-5.6-terra"},
])
def test_bad_requests(settings, payload):
    client, fake = make(settings)
    r = client.post("/api/arena/attack", json=payload)
    assert r.status_code == 422 and r.json()["code"] == "bad_request" and fake.requests == []


def test_too_large_body(settings):
    client, _ = make(settings)
    r = client.post("/api/arena/attack", content=b"x" * 20000, headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_rate_limit(settings):
    client, fake = make(settings, "Ок.", "Ок.", rate_limit_per_hour=2)
    assert post(client).status_code == 200 and post(client).status_code == 200
    r = post(client)
    assert r.status_code == 429 and r.json()["code"] == "rate_limited" and r.json()["retry_after_s"] > 0
    assert len(fake.requests) == 2


def test_daily_budget(settings):
    client, fake = make(settings, daily_budget_rub=0.01)
    r = post(client)
    assert r.status_code == 503 and r.json()["code"] == "budget_exhausted" and fake.requests == []
    assert client.get("/api/arena/status").json()["live"] is False


def test_model_error_is_502_and_charged(settings):
    client, _ = make(settings, LLMError("HTTP 500"))
    r = post(client)
    assert r.status_code == 502 and r.json()["code"] == "model_error"
    assert "HTTP" not in r.json()["message"]


def test_no_key_means_not_live(settings):
    app = create_app(replace(settings, api_key=""))
    client = TestClient(app)
    assert post(client).status_code == 503
    assert client.get("/api/arena/status").json()["live"] is False
    assert client.get("/api/arena/examples").json()["examples"][0]["title"] == "t"


def test_health(settings):
    client, _ = make(settings)
    assert client.get("/api/arena/health").json() == {"ok": True}
