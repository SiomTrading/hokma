"""Message catalogs: Spanish by default, English available, same keys and placeholders, nothing unused."""
import json
import re
from pathlib import Path

import pytest

from hokma import i18n
from hokma.i18n import t

ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r"\{([a-z_0-9]+)\}")
KEY_USE = re.compile(r"""(?<![A-Za-z0-9])_?t\(\s*["']([a-z_.0-9]+)["']""")  # t("key") and _t("key")


def load(lang):
    return json.loads((i18n.CATALOG_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def properties(lang):
    out = {}
    for line in (ROOT / "installer" / "messages" / f"{lang}.properties").read_text(encoding="utf-8").splitlines():
        name, _, text = line.partition("=")
        out[name] = text
    return out


def test_python_catalogs_have_the_same_keys_and_placeholders():
    es, en = load("es"), load("en")
    assert set(es) == set(en)
    for key in es:
        assert set(PLACEHOLDER.findall(es[key])) == set(PLACEHOLDER.findall(en[key])), key


def test_installer_catalogs_have_the_same_keys_and_placeholders():
    es, en = properties("es"), properties("en")
    assert set(es) == set(en)
    for key in es:
        assert PLACEHOLDER.findall(es[key]) == PLACEHOLDER.findall(en[key]), key


def test_every_key_used_in_the_code_exists_and_every_key_is_used():
    used = set()
    for path in [*(ROOT / "backend" / "hokma").rglob("*.py"), *(ROOT / "cli").rglob("*.py")]:
        used |= set(KEY_USE.findall(path.read_text(encoding="utf-8")))
    known = set(load("en"))
    assert used - known == set(), "keys used in the code but missing from the catalogs"
    # `docs.detail.reason.<code>` is looked up by a computed key; the test below checks it against every code
    assert {k for k in known - used if not k.startswith(("docs.detail.reason.", "power.wake.", "github.", "telegram.reset_"))} == set(), "catalog keys that no code uses"


def test_every_unreadable_code_has_a_reason_in_both_catalogs():
    from hokma.docs import readers

    source = (ROOT / "backend" / "hokma" / "docs" / "readers.py").read_text(encoding="utf-8")
    raised = set(re.findall(r"""UnreadableDocument\(.*\bcode="([a-z_]+)"\)""", source))
    assert raised and raised <= set(readers.UNREADABLE_CODES), "a reader raises a code that is not declared"
    codes = {*readers.UNREADABLE_CODES, readers.UnsupportedFormat.code}
    reasons = {lang: {k.removeprefix("docs.detail.reason.") for k in load(lang) if k.startswith("docs.detail.reason.")}
               for lang in ("es", "en")}
    for lang, found in reasons.items():
        assert found == codes, f"{lang}: reasons out of sync with the codes: {found ^ codes}"


def test_unreadable_reason_shown_to_the_student_is_localized(monkeypatch):
    from hokma.docs import readers

    monkeypatch.delenv("HOKMA_LANG", raising=False)
    for code in readers.UNREADABLE_CODES:
        shown = t("docs.detail.unreadable", reason=t(f"docs.detail.reason.{code}"))
        assert not shown.startswith("docs.") and "The " not in shown, code


def test_installer_keys_are_used_by_the_scripts():
    scripts = "".join((ROOT / "installer" / name).read_text(encoding="utf-8")
                      for name in ("install.sh", "uninstall.sh", "install.ps1", "uninstall.ps1", "messages.sh", "messages.ps1"))
    # `tag.*` and `shortcut.*` are looked up by a computed key (the uninstallers loop over the three actions)
    unused = [k for k in properties("en") if not k.startswith(("tag.", "shortcut.")) and k not in scripts]
    assert unused == []


def test_spanish_is_the_default_and_english_is_available(monkeypatch, tmp_path):
    monkeypatch.delenv("HOKMA_LANG", raising=False)
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    assert i18n.language() == "es" and t("cli.ext.rejected") == load("es")["cli.ext.rejected"]
    monkeypatch.setenv("HOKMA_LANG", "en")
    assert i18n.language() == "en" and t("cli.ext.rejected") == load("en")["cli.ext.rejected"]
    monkeypatch.setenv("HOKMA_LANG", "fr")                      # unsupported: back to the default
    assert i18n.language() == "es"


def test_language_file_in_the_data_dir_is_used_when_the_variable_is_not_set(monkeypatch, tmp_path):
    monkeypatch.delenv("HOKMA_LANG", raising=False)
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    (tmp_path / "language").write_text("en\n")
    assert i18n.language() == "en"
    monkeypatch.setenv("HOKMA_LANG", "es")                      # the variable wins
    assert i18n.language() == "es"


def test_unknown_keys_and_missing_params_do_not_crash(monkeypatch):
    monkeypatch.delenv("HOKMA_LANG", raising=False)
    assert t("no.such.key") == "no.such.key"
    assert "{version}" in t("cli.update.up_to_date")            # missing parameter: text returned as is


@pytest.mark.parametrize("lang", ["es", "en"])
def test_cli_prints_in_the_selected_language(monkeypatch, capsys, tmp_path, lang):
    from hokma_cli import main as cli

    monkeypatch.setenv("HOKMA_LANG", lang)
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert cli.main(["ext", "requests"]) == 0
    assert load(lang)["cli.ext.none_pending"] in capsys.readouterr().out
