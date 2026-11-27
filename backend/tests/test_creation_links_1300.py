"""1.3.0 (task 3433), B3 of the review: a symlink or a junction planted where the creator writes is refused, and nothing leaves the working folder."""
import os

import pytest

from code_support import db, env, pg_admin_url, pg_template, pg_url_migrated  # noqa: F401  (fixtures)
from e2e_h4.support import caller, failed, ok
from test_pandas_checks_1300 import REFERENCE
from test_strategy_create_1300 import ARGS, CREATOR, SPEC

from hokma.core.config import hokma_home
from hokma.factory import bank, creation, user_rules

symlinks = pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="symlinks need privileges on Windows: the junction is simulated below")


def who(n):
    return caller(CREATOR, conversation=n)


def plant_after_card(monkeypatch, plant):
    """Let the card be made, then plant something in the place where its folder goes: the moment an attacker has between the number and the files."""
    real = bank.create

    def create(**kw):
        made = real(**kw)
        plant(user_rules.drafts_dir() / f"{user_rules.code_of(made.bank_id)}-{made.slug}")
        return made

    monkeypatch.setattr(creation.bank, "create", create)


def empty(path):
    return not any(path.iterdir())


@symlinks
def test_a_folder_planted_as_a_symlink_is_refused_and_the_target_stays_empty(db, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    user_rules.drafts_dir().mkdir(parents=True, exist_ok=True)
    plant_after_card(monkeypatch, lambda folder: folder.symlink_to(outside, target_is_directory=True))
    assert failed("strategy_create", ARGS, who(1))["error_code"] == "blocked"
    assert empty(outside)


def test_a_folder_that_was_already_there_is_never_taken_by_a_new_card(db, tmp_path, monkeypatch):
    user_rules.drafts_dir().mkdir(parents=True, exist_ok=True)
    plant_after_card(monkeypatch, lambda folder: folder.mkdir())
    assert failed("strategy_create", ARGS, who(2))["error_code"] == "blocked"


def test_a_junction_is_refused_even_if_it_looks_like_a_plain_folder(db, monkeypatch):
    """No junction can be made here: the check that Windows answers is simulated and the folder is a plain one that the check calls a junction."""
    user_rules.drafts_dir().mkdir(parents=True, exist_ok=True)
    made = ok("strategy_create", ARGS, who(3))
    folder = hokma_home() / made["folder"]
    monkeypatch.setattr(user_rules, "_is_junction", lambda p: p.name == "versions")
    changed = REFERENCE.replace('"default": 2.0, "min": 1.0', '"default": 2.5, "min": 1.0')
    answer = failed("strategy_create", {**ARGS, "code": changed, "bank_id": made["bank_id"], "spec": SPEC + "\nChange.\n"}, who(3))
    assert answer["error_code"] == "blocked"
    assert not (folder / "versions" / "v2" / "strategy.py").exists()


@symlinks
def test_a_versions_folder_or_a_version_folder_planted_as_a_link_is_refused(db, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    made = ok("strategy_create", ARGS, who(4))
    folder = hokma_home() / made["folder"]
    (folder / "versions" / "v2").symlink_to(outside, target_is_directory=True)
    changed = REFERENCE.replace('"default": 2.0, "min": 1.0', '"default": 2.5, "min": 1.0')
    args = {**ARGS, "code": changed, "bank_id": made["bank_id"], "spec": SPEC + "\nChange.\n"}
    assert failed("strategy_create", args, who(4))["error_code"] == "blocked"
    assert empty(outside)
    (folder / "versions" / "v2").unlink()
    (folder / "versions").rename(folder / "versions_real")
    (folder / "versions").symlink_to(outside, target_is_directory=True)
    assert failed("strategy_create", {**args, "code": changed + "\n# other\n"}, who(4))["error_code"] == "blocked"
    assert empty(outside)


@symlinks
def test_a_spec_or_a_subfolder_replaced_by_a_link_is_refused(db, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "victim.txt"
    target.write_text("keep", encoding="utf-8")
    made = ok("strategy_create", ARGS, who(5))
    folder = hokma_home() / made["folder"]
    (folder / "spec.md").unlink()
    (folder / "spec.md").symlink_to(target)
    changed = REFERENCE.replace('"default": 2.0, "min": 1.0', '"default": 2.5, "min": 1.0')
    args = {**ARGS, "code": changed, "bank_id": made["bank_id"], "spec": SPEC + "\nChange.\n"}
    assert failed("strategy_create", args, who(5))["error_code"] == "blocked"
    assert target.read_text(encoding="utf-8") == "keep"
    (folder / "spec.md").unlink()
    (folder / "spec.md").write_text("# spec\n", encoding="utf-8")
    (folder / "adr").rmdir()
    (folder / "adr").symlink_to(outside, target_is_directory=True)
    assert failed("strategy_create", args, who(5))["error_code"] == "blocked"
    assert empty(outside)


def test_a_path_whose_real_place_is_outside_the_working_folder_is_refused(tmp_path):
    root = tmp_path / "service" / "strategies"
    root.mkdir(parents=True)
    with pytest.raises(user_rules.UserRuleError):
        user_rules.check_inside(tmp_path / "elsewhere" / "STR-001-x", root)
    with pytest.raises(user_rules.UserRuleError):
        user_rules.check_inside(root / ".." / "elsewhere", root)
    user_rules.check_inside(root / "STR-001-x" / "versions" / "v1", root)          # missing levels inside are fine


def test_a_file_with_a_second_name_is_not_written_through(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    other = tmp_path / "other.txt"
    other.write_text("keep", encoding="utf-8")
    os.link(other, root / "spec.md")
    with pytest.raises(user_rules.UserRuleError):
        user_rules.write_text_inside(root / "spec.md", "attack", root)
    assert other.read_text(encoding="utf-8") == "keep"
