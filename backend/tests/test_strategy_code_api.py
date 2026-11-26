"""The route of the owner for the strategies written as code (SPEC-031 A2, 7): the token and the confirmation of the owner's tab are needed to approve, and the reviewer comes first."""
from code_support import BRIEF, CODER, REVIEWER, RESEARCHER, make_bank, put_files
from e2e_h4.support import caller, ok
from permissions_support import HEADERS, owner  # noqa: F401  (fixture)

from hokma.rpc.ws import SESSION_TOKEN


def draft(slug="inside_bar"):
    bank_id = make_bank(None, f"Strategy {slug}")
    made = ok("strategy_brief_write", {"bank_id": bank_id, "slug": slug, "title": slug, "brief": BRIEF}, caller(RESEARCHER))
    put_files(slug)
    return ok("strategy_code_save", {"rule_id": made["rule_id"]}, caller(CODER))


def approve_by_reviewer(kept):
    return ok("strategy_code_verdict", {"rule_version_id": kept["rule_version_id"], "verdict": "approved"}, caller(REVIEWER))


def test_every_route_needs_the_token_and_the_two_that_decide_need_the_confirmation_of_the_owner(owner):
    kept = draft()
    path = f"/api/strategy-code/{kept['rule_version_id']}"
    for method, url in (("get", "/api/strategy-code"), ("get", path), ("post", path + "/approve"), ("post", path + "/reject")):
        assert getattr(owner.client, method)(url, **({"json": {"confirm": True}} if method == "post" else {})).status_code == 401, url
    only_token = {"X-Hokma-Token": SESSION_TOKEN}
    for url, body in ((path + "/approve", {"confirm": True}), (path + "/reject", {})):
        res = owner.client.post(url, json=body, headers=only_token)
        assert res.status_code == 403 and res.json()["detail"]["code"] == "owner_confirmation_required", url
    assert owner.client.get(path, headers=only_token).status_code == 200            # reading needs only the token


def test_the_owner_cannot_approve_what_the_reviewer_has_not_approved_and_needs_to_confirm(owner):
    kept = draft()
    path = f"/api/strategy-code/{kept['rule_version_id']}"
    res = owner.client.post(path + "/approve", json={"confirm": True}, headers=HEADERS)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "agent_review_required"
    approve_by_reviewer(kept)
    no_confirm = owner.client.post(path + "/approve", json={}, headers=HEADERS)
    assert no_confirm.status_code == 400 and no_confirm.json()["detail"]["code"] == "confirm_required"
    assert owner.client.post("/api/strategy-code/9999/approve", json={"confirm": True}, headers=HEADERS).status_code == 404


def test_the_notice_reads_the_generated_versions_and_the_owner_approves_after_confirming(owner):
    kept = draft()
    assert owner.client.get("/api/strategy-code?stage=generated", headers=HEADERS).json()["rows"] == []
    approve_by_reviewer(kept)
    [row] = owner.client.get("/api/strategy-code?stage=generated", headers=HEADERS).json()["rows"]
    assert (row["registry_id"], row["stage"], row["origin"], row["tests"], row["reviewer"]) == ("u_inside_bar_v1", "generated", {"kind": "hokma"}, 4, REVIEWER)
    assert "inside bar" in row["summary"]
    detail = owner.client.get(f"/api/strategy-code/{kept['rule_version_id']}", headers=HEADERS).json()
    assert "class InsideBarBreakout" in detail["code"] and detail["agent_reviewed"] is True and detail["owner_reviewed"] is False
    done = owner.client.post(f"/api/strategy-code/{kept['rule_version_id']}/approve", json={"confirm": True, "note": "read it"}, headers=HEADERS)
    assert done.status_code == 200 and done.json()["status"] == "owner_reviewed"
    assert owner.client.get("/api/strategy-code?stage=generated", headers=HEADERS).json()["rows"] == []
    assert owner.client.post(f"/api/strategy-code/{kept['rule_version_id']}/approve", json={"confirm": True}, headers=HEADERS).status_code == 400


def test_the_owner_can_reject_with_a_note_and_the_version_is_never_proposed(owner):
    kept = draft("breakout_two")
    approve_by_reviewer(kept)
    done = owner.client.post(f"/api/strategy-code/{kept['rule_version_id']}/reject", json={"note": "not my kind of trade"}, headers=HEADERS)
    assert done.status_code == 200 and done.json()["status"] == "rejected"
    from hokma.factory import user_rules

    try:
        user_rules.propose_gate(kept["registry_id"])
        raise AssertionError("a rejected version cannot be proposed")
    except user_rules.UserRuleError:
        pass
