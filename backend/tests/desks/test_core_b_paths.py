"""AH-63: every user path is under hokma_data_dir(), outside the package."""
import pytest

from hokma.core.config import hokma_data_dir
from hokma.desks.core import paths

ACCENT = "Jos" + chr(0xE9) + " Pe" + chr(0xF1) + "a"


@pytest.fixture
def data(tmp_path, monkeypatch):
    base = tmp_path / ("my data " + ACCENT)
    monkeypatch.setenv("HOKMA_DATA_DIR", str(base))
    return base


def test_every_user_path_is_under_the_data_dir_with_spaces_and_accents_AH_63(data):
    assert hokma_data_dir() == data
    for path in (paths.desks_dir(), paths.desk_dir(7), paths.cache_dir(), paths.llm_cache_dir(), paths.runs_dir(), paths.sealed_file(),
                 paths.user_path("a b", ACCENT)):
        assert paths.is_within(str(data), str(path))
    assert paths.desk_dir(7) == data / "desks" / "7"


def test_no_user_path_is_inside_the_package_AH_63(data):
    assert not paths.is_within(str(paths.package_dir()), str(paths.llm_cache_dir()))
    with pytest.raises(paths.PathEscape):
        paths.assert_outside_package(paths.package_dir() / "x")


@pytest.mark.parametrize("part", ["..", ".", "...", "", " x", "a/b", "a\\b", "C:", "/etc", "a" * 200, "x\x00y"])
def test_a_part_that_escapes_is_refused_AH_63(data, part):
    with pytest.raises(paths.PathEscape):
        paths.user_path(part)


@pytest.mark.parametrize("bad", [0, -1, True, "3", None])
def test_a_bad_desk_id_is_refused_AH_63(data, bad):
    with pytest.raises(paths.PathEscape):
        paths.desk_dir(bad)


def test_windows_rules_with_spaces_and_accents_AH_63():
    base = "C:\\Users\\" + ACCENT + "\\AppData\\Local\\Hokma"
    assert paths.is_within(base, base + "\\desks\\1", flavour="windows")
    assert paths.is_within(base.upper(), base.lower() + "\\Desks", flavour="windows")          # case does not matter on Windows
    assert not paths.is_within(base, "C:\\Users\\" + ACCENT + "\\AppData\\Local\\HokmaOther", flavour="windows")
    assert not paths.is_within(base, base + "\\..\\Elsewhere", flavour="windows")
    assert not paths.is_within(base, "D:\\Hokma\\desks", flavour="windows")


def test_unix_rules_with_spaces_and_accents_AH_63():
    base = "/home/" + ACCENT + "/my data/hokma"
    assert paths.is_within(base, base + "/desks/1", flavour="posix")
    assert not paths.is_within(base, base + "-other/x", flavour="posix")
    assert not paths.is_within(base, base + "/../x", flavour="posix")
    assert paths.is_within(base, base + "/a/../b", flavour="posix")
    assert paths.is_within(base, base, flavour="posix")


def test_ensure_dir_creates_only_when_asked_AH_63(data):
    target = paths.desk_dir(3)
    assert not target.exists()
    assert paths.ensure_dir(target).is_dir()
