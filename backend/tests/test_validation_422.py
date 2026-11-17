"""Schema rejections (422) come back in the panel's language with the usual {detail: {code, message}} envelope."""
import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)
from fastapi.testclient import TestClient

from hokma.core.app import create_app
from hokma.rpc.ws import SESSION_TOKEN
from hokma.setup.messages import t


@pytest.fixture
def client(db):
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
    c = TestClient(create_app())
    c.headers.update({"X-Hokma-Token": SESSION_TOKEN})
    return c


def detail(res):
    assert res.status_code == 422
    return res.json()["detail"]


def test_empty_and_too_long_names_are_translated(client):
    es = {"Accept-Language": "es"}
    d = detail(client.post("/api/projects", json={"name": "", "kind": "other"}, headers=es))
    assert d["code"] == "validation_error" and "demasiado" not in d["message"]
    assert "al menos 1" in d["message"] and "should have" not in d["message"]
    assert d["errors"][0]["type"] == "min_length"
    d = detail(client.post("/api/projects", json={"name": "x" * 500, "kind": "other"}, headers=es))
    assert "muy largo" in d["message"] and "characters" not in d["message"]
    d = detail(client.post("/api/projects", json={"name": "x" * 500, "kind": "other"}, headers={"Accept-Language": "en"}))
    assert "too long" in d["message"]


def test_wrong_type_and_required_are_translated(client):
    d = detail(client.put("/api/usage/cap", json={"cap": "abc"}, headers={"Accept-Language": "es"}))
    assert d["code"] == "validation_error" and "tipo" in d["message"] and "valid integer" not in d["message"]
    d = detail(client.post("/api/projects", json={}, headers={"Accept-Language": "es"}))
    assert d["message"] == t("validation.required", field="name").text("es")


def test_marketplace_pattern_is_translated(client):
    d = detail(client.post("/api/extensions/marketplaces", json={"repo": "no es repo"}, headers={"Accept-Language": "es"}))
    assert d["code"] == "validation_error" and "formato" in d["message"] and "pattern" not in d["message"].lower()


def test_a_body_that_is_not_json_says_so_in_the_language_of_the_request(client):
    import json
    from pathlib import Path

    catalog = Path(__file__).resolve().parents[1] / "hokma" / "catalog"
    for lang in ("es", "en"):
        res = client.post("/api/projects", content=b"{not json", headers={"Accept-Language": lang, "Content-Type": "application/json"})
        expected = json.loads((catalog / f"{lang}.json").read_text(encoding="utf-8"))["validation.bad_json"]
        assert res.status_code == 422 and res.json()["detail"]["message"] == expected
