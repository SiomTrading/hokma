"""The repository rules checker (scripts/check_repo_rules.py): the repo passes, and violations are caught."""
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_repo_rules", ROOT / "scripts" / "check_repo_rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)

# Names and phrases are stored reversed so this file does not contain them either (the checker reads words).
def rev(text):
    return text[::-1]


NAMES = [rev(x) for x in ["semreH", "hcraeseR suoN", "stnegAgnidarT", "ecilAnepO", "dnuf-egdeh-ia", "redart-yad",
                          "srewoprepus", "edoc-edualc-gnihtyreve", "slliks-edualc", "kcatsg", "dnim-tnauq"]]
PHRASES = [rev(x) for x in ["yb deripsni", "no desab", "morf detpada", "morf detrop", "fo trop"]]


def repo(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_the_repository_passes_its_own_rules():
    assert rules.check(ROOT) == []


@pytest.mark.parametrize("name", NAMES)
def test_external_names_are_caught_in_any_case_and_form(tmp_path, name):
    for text in (f"see {name}", f"see {name.upper()}", f"see {name.lower().replace('-', '_')}", f"x\n{name}\ny"):
        problems = rules.check(repo(tmp_path, {"docs/a.md": text}))
        assert problems and "origin reference" in problems[0], text
        (tmp_path / "docs" / "a.md").unlink()


@pytest.mark.parametrize("phrase", PHRASES)
def test_origin_phrases_are_caught_even_across_a_line_break(tmp_path, phrase):
    first, second = phrase.split(" ")
    for text in (f"a note {phrase} someone", f"a note {first}\n{second} someone", f"# {phrase.title()}"):
        problems = rules.check(repo(tmp_path, {"src/a.py": text}))
        assert problems, text
        (tmp_path / "src" / "a.py").unlink()


def test_names_in_paths_are_caught(tmp_path):
    problems = rules.check(repo(tmp_path, {f"docs/{NAMES[0].lower()}-notes.md": "neutral text"}))
    assert problems and "path" in problems[0]


def test_only_the_legal_folder_may_name_authors(tmp_path):
    root = repo(tmp_path, {"legal/notices.md": f"Copyright {NAMES[1]}", "docs/a.md": f"Copyright {NAMES[1]}"})
    problems = rules.check(root)
    assert {p.split(":")[0] for p in problems} == {"docs/a.md"}


def test_innocent_text_is_not_flagged(tmp_path):
    text = "the report is ready; an open airport; supers and powers; mind the gap"
    assert rules.check(repo(tmp_path, {"docs/a.md": text})) == []


def test_handwritten_spanish_is_caught_outside_the_catalogs_only(tmp_path):
    accented = "café con niño"
    root = repo(tmp_path, {
        "src/a.py": f"# {accented}",
        "src/b.py": "print('abre la carpeta del alumno')",
        "carpeta-uno/c.md": "plain English text",
        "backend/hokma/catalog/es.json": f'{{"k": "{accented}"}}',
        "installer/messages/es.properties": f"k={accented}",
        "dashboard/src/i18n/index.ts": f"const s = '{accented}'",
    })
    flagged = sorted({p.split(":")[0] for p in rules.check(root)})
    assert flagged == ["carpeta-uno/c.md", "src/a.py", "src/b.py"]


def test_the_script_runs_as_a_command_and_fails_on_a_violation(tmp_path):
    root = repo(tmp_path, {"README.md": f"derived {PHRASES[0]} {NAMES[0]}"})
    script = ROOT / "scripts" / "check_repo_rules.py"
    ok = subprocess.run(["python3", str(script)], capture_output=True, text=True)
    assert ok.returncode == 0 and "repo rules ok" in ok.stdout
    spec2 = importlib.util.spec_from_file_location("again", script)
    mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(mod)
    mod.ROOT = root
    assert mod.main() == 1


def test_a_dev_notes_folder_fails_the_check(tmp_path):
    problems = rules.check(repo(tmp_path, {"dev-notes/a/CONTRACT.md": "internal"}))
    assert any(line.startswith("dev-notes/") for line in problems)
