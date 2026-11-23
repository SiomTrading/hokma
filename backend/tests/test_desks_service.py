"""Desk repository and service (SPEC-014 P2): templates, all-or-nothing creation, one chief, team changes, archive."""
import os

import pytest
from dbsupport import db, env  # noqa: F401  (fixtures)

from hokma.db import IntegrityViolation
from hokma.desks import roles, service, templates
from hokma.desks.errors import DeskError
from hokma.i18n import use_language
from hokma.kit import safefs

SLUGS = ("desk-chief", "hugo", "risk", "iris", "signals", "axel", "vera", "bull-case", "bear-case", "judge")


@pytest.fixture
def desks_db(db):
    """The `db` fixture plus the ten agents the desk templates use, all turned on."""
    with db.write() as u:
        u.profile.save(owner_name="Ana", fund_name="Fondo Prueba")
        for slug in SLUGS:
            u.agents.upsert_kit(slug, slug.title(), f"{slug} agent", "balanced", f"agents/{slug}.md")
            u.agents.set_status(slug, "active")
    return db


def projects_dir():
    return os.path.join(os.environ["HOKMA_HOME"], "projects")


def counts(db):
    with db.read() as u:
        return {t: u.session.scalar(f"SELECT count(*) FROM {t}") for t in
                ("projects", "desks", "desk_agents", "desk_assets")}


def make(db, **kw):
    kw.setdefault("name", "Futuros NQ")
    kw.setdefault("capital", 10000)
    return service.create_desk(db=db, **kw)


# ------------------------------------------------------------------------------------------ templates

@pytest.mark.parametrize("key,total", [("minimal", 3), ("standard", 7), ("debate", 10)])
def test_template_creates_its_team_with_default_models(desks_db, key, total):
    desk = make(desks_db, template=key)
    assert desk["template"] == key and desk["agents_total"] == total == len(desk["agents"])
    expected = {m.agent_slug: (m.role, m.model) for m in templates.TEMPLATES[key].members}
    assert {a["agent_slug"]: (a["role"], a["model"]) for a in desk["agents"]} == expected
    assert all(a["active"] for a in desk["agents"])
    assert sum(1 for a in desk["agents"] if a["role"] == "chief") == 1


def test_listing_gives_three_templates_with_suggested_budget(desks_db):
    by_key = {t["key"]: t for t in service.list_templates()}
    assert [by_key[k]["suggested_budget"] for k in ("minimal", "standard", "debate")] == [150_000, 300_000, 600_000]
    assert [len(by_key[k]["agents"]) for k in ("minimal", "standard", "debate", "custom")] == [3, 7, 10, 1]


def test_roles_give_keys_and_unknown_roles_fall_back_to_custom():
    info = roles.describe("chief")
    assert info["can"] == "desks.role.chief.can" and info["cannot"] == "desks.role.chief.cannot"
    assert roles.describe("nonsense")["role"] == "custom"
    assert roles.describe("custom")["cannot"] == roles.CUSTOM_CANNOT


# ------------------------------------------------------------------------------------------ creation

def test_create_makes_project_row_folder_and_claude_file(desks_db):
    desk = make(desks_db, mandate="Trade NQ opening range", focus="NQ intraday", assets=["NQ", "ES"],
                objective_pct=4.5, token_budget=1000)
    assert desk["mode"] == "simulation" and desk["currency"] == "USD" and desk["status"] == "active"
    assert (desk["capital"], desk["objective_pct"], desk["token_budget"]) == (10000, 4.5, 1000)
    assert [a["symbol"] for a in desk["assets"]] == ["NQ", "ES"]
    with desks_db.read() as u:
        project = u.projects.get(desk["project_id"])
    assert project.kind == "strategy" and project.name == "Futuros NQ"
    folder = os.path.join(projects_dir(), project.slug)
    assert project.path == folder
    for sub in ("contexto", "documentos", "imagenes", "audios", "codigo", "resultados"):
        assert os.path.isdir(os.path.join(folder, sub))
    text = open(os.path.join(folder, "CLAUDE.md"), encoding="utf-8").read()
    assert "Futuros NQ" in text and "Trade NQ opening range" in text and "10,000 USD" in text
    assert "{{" not in text and "Ana" in text
    assert "real money moves only through hokma's order tools" in text.lower()


def test_placeholders_typed_by_the_owner_are_not_substituted_again(desks_db):
    desk = make(desks_db, mandate="Keep {{capital}} and {{desk_name}} literal")
    with desks_db.read() as u:
        folder = u.projects.get(desk["project_id"]).path
    text = open(os.path.join(folder, "CLAUDE.md"), encoding="utf-8").read()
    assert "Keep {{capital}} and {{desk_name}} literal" in text


def test_empty_mandate_gets_the_catalog_sentence(desks_db):
    desk = make(desks_db)
    with desks_db.read() as u:
        folder = u.projects.get(desk["project_id"]).path
    assert "No mandate written yet" in open(os.path.join(folder, "CLAUDE.md"), encoding="utf-8").read()


def test_creating_a_desk_turns_on_every_agent_it_uses_and_leaves_the_others_as_they_were(desks_db):
    """An agent assigned to a desk works for it, so those the template uses are turned on (hugo too: the desk chief
    could not ask it otherwise); the ones the template does not use keep their state."""
    with desks_db.write() as u:
        for slug in ("desk-chief", "risk", "signals", "hugo"):
            u.agents.set_status(slug, "off")
    service.create_desk("Futuros", 10000, template="minimal", db=desks_db)
    with desks_db.read() as u:
        state = {slug: u.agents.get(slug).status for slug in ("desk-chief", "risk", "signals", "hugo")}
    assert state == {"desk-chief": "active", "risk": "active", "signals": "off", "hugo": "active"}


def test_missing_agent_creates_nothing(desks_db):
    with desks_db.write() as u:
        u.session.execute("DELETE FROM agents WHERE slug = 'judge'")
    before = counts(desks_db)
    with pytest.raises(DeskError) as err:
        make(desks_db, template="debate")
    assert (err.value.code, err.value.status) == ("agent_missing", 409)
    assert counts(desks_db) == before and not os.path.exists(projects_dir() + "/futuros-nq")


def test_failure_writing_the_folder_leaves_no_rows_and_no_folder(desks_db, monkeypatch):
    real = safefs.write_atomic

    def boom(root, rel, data, mode=0o644):
        if rel == "CLAUDE.md":
            raise OSError("disk full")
        return real(root, rel, data, mode)

    monkeypatch.setattr(safefs, "write_atomic", boom)
    before = counts(desks_db)
    with pytest.raises(DeskError) as err:
        make(desks_db, template="standard")
    assert err.value.code == "folder_failed"
    assert counts(desks_db) == before
    assert not os.path.exists(os.path.join(projects_dir(), "futuros-nq"))


def test_failure_in_the_database_leaves_no_rows_and_no_folder(desks_db, monkeypatch):
    from hokma.db.repos.desks import DesksRepo

    def boom(self, *a, **k):
        raise IntegrityViolation("forced")

    monkeypatch.setattr(DesksRepo, "replace_assets", boom)
    before = counts(desks_db)
    with pytest.raises(DeskError) as err:
        make(desks_db, assets=["NQ"])
    assert err.value.code == "db_failed"
    assert counts(desks_db) == before
    assert not os.path.exists(os.path.join(projects_dir(), "futuros-nq"))


def test_existing_folder_is_never_touched(desks_db):
    taken = os.path.join(projects_dir(), "futuros-nq")
    os.makedirs(taken)
    open(os.path.join(taken, "mine.txt"), "w").write("keep")
    desk = make(desks_db)
    with desks_db.read() as u:
        project = u.projects.get(desk["project_id"])
    assert project.slug == "futuros-nq-2" and open(os.path.join(taken, "mine.txt")).read() == "keep"


def test_two_desks_with_the_same_name_get_two_folders(desks_db):
    a, b = make(desks_db), make(desks_db)
    assert a["project_id"] != b["project_id"] and a["id"] != b["id"]
    assert [d["sort_order"] for d in service.list_desks(db=desks_db)] == [1, 2]


@pytest.mark.parametrize("kw,code", [
    ({"name": "   "}, "bad_name"), ({"name": "x" * 81}, "bad_name"),
    ({"capital": 99}, "bad_capital"), ({"capital": 1_000_000_001}, "bad_capital"),
    ({"capital": True}, "bad_capital"), ({"capital": "10000"}, "bad_capital"), ({"capital": 1500.5}, "bad_capital"),
    ({"template": "huge"}, "bad_template"), ({"focus": "f" * 121}, "bad_focus"),
    ({"mandate": "m" * 4001}, "bad_mandate"), ({"objective_pct": 101}, "bad_objective"),
    ({"token_budget": 0}, "bad_budget"), ({"token_budget": -5}, "bad_budget"),
    ({"assets": [f"S{i}" for i in range(21)]}, "bad_assets"), ({"assets": ["NQ", "NQ"]}, "bad_assets"),
    ({"assets": [""]}, "bad_assets"),
])
def test_invalid_input_is_refused_and_writes_nothing(desks_db, kw, code):
    before = counts(desks_db)
    with pytest.raises(DeskError) as err:
        make(desks_db, **kw)
    assert err.value.code == code and err.value.status == 400
    assert counts(desks_db) == before


def test_a_whole_float_capital_is_accepted(desks_db):
    assert make(desks_db, capital=2500.0)["capital"] == 2500


def test_mode_cannot_be_real(desks_db):
    desk = make(desks_db)
    with pytest.raises(IntegrityViolation):
        with desks_db.write() as u:
            u.session.execute("UPDATE desks SET mode = 'real' WHERE id = :i", {"i": desk["id"]})


# ------------------------------------------------------------------------------------------ one chief

def test_second_chief_is_refused_by_the_index_and_by_the_service(desks_db):
    desk = make(desks_db, template="minimal")
    with pytest.raises(IntegrityViolation):
        with desks_db.write() as u:
            u.desks.add_agent(desk["id"], "judge", "chief", "sonnet")
    with pytest.raises(DeskError) as err:
        service.add_agent(desk["id"], "iris", "sonnet", role="chief", db=desks_db)
    assert err.value.code == "bad_role"


def test_chief_cannot_be_removed_or_turned_off(desks_db):
    desk = make(desks_db, template="minimal")
    for call in (lambda: service.remove_agent(desk["id"], "desk-chief", desks_db),
                 lambda: service.set_agent(desk["id"], "desk-chief", active=False, db=desks_db)):
        with pytest.raises(DeskError) as err:
            call()
        assert (err.value.code, err.value.status) == ("chief_required", 409)
    assert len(service.list_agents(desk["id"], desks_db)) == 3


# ------------------------------------------------------------------------------------------ team

def test_model_change_is_per_desk_and_does_not_touch_the_global_agent(desks_db):
    a, b = make(desks_db, template="standard"), make(desks_db, name="Otra", template="standard")
    out = service.set_agent(a["id"], "signals", model="opus", db=desks_db)
    assert out["model"] == "opus"
    model_of = lambda d: {x["agent_slug"]: x["model"] for x in service.list_agents(d["id"], desks_db)}["signals"]
    assert model_of(a) == "opus" and model_of(b) == "sonnet"
    with desks_db.read() as u:
        assert u.agents.get("signals").model_tier == "balanced"


def test_bad_model_and_unknown_agent(desks_db):
    desk = make(desks_db, template="minimal")
    with pytest.raises(DeskError) as err:
        service.set_agent(desk["id"], "hugo", model="gpt", db=desks_db)
    assert err.value.code == "bad_model"
    with pytest.raises(DeskError) as err:
        service.set_agent(desk["id"], "iris", model="haiku", db=desks_db)
    assert (err.value.code, err.value.status) == ("agent_not_in_desk", 404)


def test_bring_an_agent_rules(desks_db):
    desk = make(desks_db, template="minimal")
    with desks_db.write() as u:
        u.agents.set_status("iris", "off")
    with pytest.raises(DeskError) as err:
        service.add_agent(desk["id"], "iris", "sonnet", db=desks_db)
    assert (err.value.code, err.value.status) == ("agent_off", 409)
    with desks_db.read() as u:
        assert u.agents.get("iris").status == "off"                # nothing global changed in silence
    with desks_db.write() as u:
        u.agents.set_status("iris", "active")
    added = service.add_agent(desk["id"], "iris", "haiku", db=desks_db)
    assert (added["role"], added["model"]) == ("research", "haiku")
    with pytest.raises(DeskError) as err:
        service.add_agent(desk["id"], "iris", "haiku", db=desks_db)
    assert err.value.code == "agent_in_desk"
    with pytest.raises(DeskError) as err:
        service.add_agent(desk["id"], "ghost", "haiku", db=desks_db)
    assert (err.value.code, err.value.status) == ("agent_missing", 404)


def test_an_agent_outside_the_templates_joins_as_custom(desks_db):
    with desks_db.write() as u:
        u.agents.create_custom("mine", "Mine", description="my agent")
        u.agents.set_status("mine", "active")
    desk = make(desks_db, template="minimal")
    added = service.add_agent(desk["id"], "mine", "sonnet", db=desks_db)
    assert added["role"] == "custom" and added["cannot"] == roles.CUSTOM_CANNOT


def test_removing_an_agent_keeps_the_global_agent(desks_db):
    desk = make(desks_db, template="standard")
    service.remove_agent(desk["id"], "vera", desks_db)
    assert "vera" not in {a["agent_slug"] for a in service.list_agents(desk["id"], desks_db)}
    with desks_db.read() as u:
        assert u.agents.find("vera") is not None
    with pytest.raises(DeskError) as err:
        service.remove_agent(desk["id"], "vera", desks_db)
    assert err.value.code == "agent_not_in_desk"


def test_turning_an_agent_off_on_the_desk(desks_db):
    desk = make(desks_db, template="minimal")
    assert service.set_agent(desk["id"], "risk", active=False, db=desks_db)["active"] is False
    assert service.set_agent(desk["id"], "risk", active=True, db=desks_db)["active"] is True


# ------------------------------------------------------------------------------------------ update, archive, assets

def test_update_changes_only_what_is_given(desks_db):
    desk = make(desks_db, focus="NQ", mandate="old", token_budget=500)
    out = service.update_desk(desk["id"], {"capital": 25000, "mandate": "new", "token_budget": None}, desks_db)
    assert (out["capital"], out["mandate"], out["token_budget"], out["focus"], out["name"]) == (
        25000, "new", None, "NQ", "Futuros NQ")
    assert service.update_desk(desk["id"], {"status": "paused"}, desks_db)["status"] == "paused"
    with pytest.raises(DeskError) as err:
        service.update_desk(desk["id"], {"status": "real"}, desks_db)
    assert err.value.code == "bad_status"
    with pytest.raises(DeskError) as err:
        service.update_desk(999, {"name": "x"}, desks_db)
    assert (err.value.code, err.value.status) == ("not_found", 404)


def test_archive_hides_the_desk_but_deletes_nothing(desks_db):
    desk = make(desks_db, assets=["NQ"])
    before = counts(desks_db)
    assert service.archive_desk(desk["id"], desks_db)["status"] == "archived"
    assert service.list_desks(db=desks_db) == []
    assert [d["id"] for d in service.list_desks(include_archived=True, db=desks_db)] == [desk["id"]]
    assert counts(desks_db) == before
    assert service.get_desk(desk["id"], desks_db)["agents_total"] == 7
    with desks_db.read() as u:
        assert u.projects.get(desk["project_id"]).status == "active"


def test_assets_are_replaced_in_the_given_order(desks_db):
    desk = make(desks_db, assets=["NQ"])
    out = service.set_assets(desk["id"], ["ES", {"symbol": "CL", "note": "oil"}, "NQ"], desks_db)
    assert [(a["symbol"], a["note"]) for a in out] == [("ES", None), ("CL", "oil"), ("NQ", None)]
    assert service.set_assets(desk["id"], [], desks_db) == []
    with pytest.raises(DeskError) as err:
        service.set_assets(desk["id"], ["A"] * 2, desks_db)
    assert err.value.code == "bad_assets"


def test_connections_are_stored_per_desk(desks_db):
    desk = make(desks_db)
    with desks_db.write() as u:
        u.desks.set_connection(desk["id"], "tradovate", True)
        u.desks.set_connection(desk["id"], "tradovate", False)
        u.desks.set_connection(desk["id"], "fred", True)
    chosen = {c["provider_id"]: c["enabled"] for c in service.get_desk(desk["id"], desks_db)["connections"]}
    assert chosen["fred"] is True and chosen["tradovate"] is False        # the provider already connected is on from the start (coinbase has no key)


def test_by_project_and_missing_desk(desks_db):
    desk = make(desks_db)
    with desks_db.read() as u:
        assert u.desks.by_project(desk["project_id"]).id == desk["id"]
        assert u.desks.by_project(99999) is None
    with pytest.raises(DeskError) as err:
        service.get_desk(404, desks_db)
    assert err.value.code == "not_found"


def test_error_messages_come_from_the_catalog_in_both_languages(desks_db):
    with use_language("es"):
        spanish = DeskError("chief_required").message
    with use_language("en"):
        english = DeskError("chief_required").message
    assert english == "The desk chief cannot be removed or turned off."
    assert spanish and spanish != english and spanish != "chief_required"
    from hokma.desks import errors

    assert set(errors._MESSAGES) >= {"not_found", "chief_required", "agent_off", "bad_assets", "db_failed"}


def test_a_desk_agent_that_was_turned_off_later_is_turned_on_when_the_desk_works(desks_db):
    desk = make(desks_db, template="minimal")
    with desks_db.write() as u:
        for slug in ("hugo", "risk"):
            u.agents.set_status(slug, "off")
        u.agents.set_status("iris", "off")                       # not on this desk
    assert service.ensure_agents_on(None, db=desks_db) == []
    assert sorted(service.ensure_agents_on(desk["project_id"], db=desks_db)) == ["hugo", "risk"]
    with desks_db.read() as u:
        assert (u.agents.get("hugo").status, u.agents.get("iris").status) == ("active", "off")
    service.set_agent(desk["id"], "risk", active=False, db=desks_db)
    with desks_db.write() as u:
        u.agents.set_status("risk", "off")
    assert service.ensure_agents_on(desk["project_id"], db=desks_db) == []     # a desk-off agent stays off


def test_reorder_assets_is_saved_keeps_notes_and_is_returned(db):
    from hokma.desks import service
    from hokma.kit import hokma_tools
    desk = service.create_desk("Orden", 10000, template="minimal", db=db,
                               assets=[{"symbol": "NQ", "note": "indice"}, "ES", "CL"])
    out = service.reorder_assets(desk["id"], ["CL", "NQ", "ES"], db=db)
    assert [a["symbol"] for a in out] == ["CL", "NQ", "ES"] and out[1]["note"] == "indice"
    assert [a["symbol"] for a in service.get_desk(desk["id"], db=db)["assets"]] == ["CL", "NQ", "ES"]
    shown = hokma_tools._desk_out(service.get_desk(desk["id"], db=db))          # what desk_show gives the agents
    assert shown["assets"] == ["CL", "NQ", "ES"]


def test_reorder_assets_refuses_a_different_set(db):
    import pytest
    from hokma.desks import service
    desk = service.create_desk("Orden", 10000, template="minimal", db=db, assets=["NQ", "ES"])
    for bad in (["NQ"], ["NQ", "ES", "CL"], ["NQ", "NQ"], ["NQ", "XX"], "NQ"):
        with pytest.raises(service.DeskError):
            service.reorder_assets(desk["id"], bad, db=db)
    assert [a["symbol"] for a in service.get_desk(desk["id"], db=db)["assets"]] == ["NQ", "ES"]


# ------------------------------------------------------------------------------------------ connections of a new desk

class _FakeConnections:
    """The part of the connection service a new desk reads: the catalog, the order of the bars and who is connected."""

    loaded = True

    def __init__(self, added):
        from hokma.market.providers import get_catalog

        self.catalog = get_catalog()
        self._added = added

    def is_added(self, provider_id):
        return provider_id in self._added

    def order(self, asset_class):
        return list(self._added)

    def is_enabled(self, provider_id):
        return provider_id in self._added

    def has_key(self, provider_id):
        return provider_id in self._added


def _rows(db, desk_id):
    with db.read() as u:
        return {c.provider_id: c.enabled for c in u.desks.list_connections(desk_id)}


def test_a_new_desk_is_born_with_the_connected_providers_turned_on(desks_db, monkeypatch):
    monkeypatch.setattr(service, "_market_service", lambda: _FakeConnections(["schwab", "finnhub"]))
    made = service.create_desk("Con Schwab", 10000, db=desks_db)
    assert _rows(desks_db, made["id"]) == {"schwab": True, "finnhub": True}
    assert made["connections"] == {"providers": ["schwab", "finnhub"], "fallback_only": False}
    assert {c["provider_id"]: c["enabled"] for c in service.list_connections(made["id"], desks_db)}["schwab"] is True


def test_a_new_desk_with_nothing_connected_says_it_only_has_the_fallback(desks_db, monkeypatch):
    monkeypatch.setattr(service, "_market_service", lambda: _FakeConnections([]))
    made = service.create_desk("Sin nada", 10000, db=desks_db)
    assert _rows(desks_db, made["id"]) == {}
    assert made["connections"] == {"providers": [], "fallback_only": True, "notice": service.FALLBACK_NOTICE, "link": "connections"}
    assert "Yahoo" in service.FALLBACK_NOTICE and "Schwab" in service.FALLBACK_NOTICE and "Finnhub" in service.FALLBACK_NOTICE


def test_activate_my_connections_only_makes_the_missing_rows_and_never_overrides_a_choice(desks_db, monkeypatch):
    monkeypatch.setattr(service, "_market_service", lambda: _FakeConnections([]))
    made = service.create_desk("Antes de conectar", 10000, db=desks_db)           # born with nothing connected: no rows
    assert made["connections"]["fallback_only"] is True
    monkeypatch.setattr(service, "_market_service", lambda: _FakeConnections(["schwab", "finnhub"]))
    service.set_connection(made["id"], "finnhub", False, desks_db)                # explicit choice: finnhub off
    first = service.activate_my_connections(made["id"], desks_db)
    assert first == {"activated": ["schwab"], "providers": ["schwab"], "fallback_only": False}
    assert _rows(desks_db, made["id"]) == {"schwab": True, "finnhub": False}      # the explicit off was not overridden
    again = service.activate_my_connections(made["id"], desks_db)
    assert again == {"activated": [], "providers": ["schwab"], "fallback_only": False}
    assert _rows(desks_db, made["id"]) == {"schwab": True, "finnhub": False}


def test_activate_my_connections_with_nothing_connected_says_fallback_only(desks_db, monkeypatch):
    monkeypatch.setattr(service, "_market_service", lambda: _FakeConnections([]))
    made = service.create_desk("Nada", 10000, db=desks_db)
    assert service.activate_my_connections(made["id"], desks_db) == {"activated": [], "providers": [], "fallback_only": True}
    with pytest.raises(service.DeskError):
        service.activate_my_connections(999999, desks_db)
