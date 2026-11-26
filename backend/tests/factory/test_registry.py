"""Registry of alphas and strategies (SPEC-020: AH-12)."""
from pathlib import Path

import pytest

from hokma.factory.registry import Registry, RegistryError, factory_list


def write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


GOOD = """\
id: fast_ema
kind: alpha
class: hokma.factory.alphas.ema_cross.EmaCross
line: DT
description: Fast exponential average cross for intraday bars.
params:
  fast: 5
  slow: 20
"""


def test_builtins_are_listed_and_a_yaml_alpha_registers_at_load(tmp_path):
    registry = Registry.with_builtins()
    ids = {row["id"] for row in factory_list(registry)}
    assert {"ema_cross", "tsmom", "macd"} <= ids
    write(tmp_path, "fast_ema.yaml", GOOD)
    assert registry.load_dir(tmp_path) == 1
    rows = {row["id"]: row for row in factory_list(registry)}
    assert rows["fast_ema"] == {"id": "fast_ema", "kind": "alpha", "class": "hokma.factory.alphas.ema_cross.EmaCross", "line": "DT",
                                "description": "Fast exponential average cross for intraday bars.", "params": {"fast": 5, "slow": 20},
                                "source": "fast_ema.yaml:1"}
    assert rows["tsmom"]["source"] == "builtin"
    assert [r["id"] for r in factory_list(registry, line="DT")] == ["fast_ema"]
    assert all(r["kind"] == "alpha" for r in factory_list(registry, kind="alpha"))
    assert factory_list(registry, kind="strategy") == []


def test_build_validates_and_overrides_the_declared_params(tmp_path):
    registry = Registry.with_builtins()
    write(tmp_path, "a.yaml", GOOD)
    registry.load_dir(tmp_path)
    alpha = registry.build("fast_ema", {"slow": 30})
    assert (alpha.fast, alpha.slow) == (5, 30)
    with pytest.raises(ValueError, match="parameter 'fast' must be between 1 and 500"):
        registry.build("fast_ema", {"fast": 900})
    with pytest.raises(KeyError):
        registry.get("missing")


def error(tmp_path, text, name="bad.yaml") -> RegistryError:
    registry = Registry.with_builtins()
    write(tmp_path, name, text)
    with pytest.raises(RegistryError) as caught:
        registry.load_dir(tmp_path)
    return caught.value


def test_an_unknown_key_fails_with_file_and_line(tmp_path):
    caught = error(tmp_path, GOOD.replace("line: DT\n", "line: DT\nleverage: 3\n"))
    assert caught.code == "unknown_key" and (caught.file, caught.line) == ("bad.yaml", 5)
    assert str(caught).startswith("bad.yaml:5: unknown key 'leverage'")


def test_an_alpha_without_a_class_fails_with_the_line_of_the_entry(tmp_path):
    caught = error(tmp_path, "# comment\nid: no_class\nkind: alpha\n")
    assert caught.code == "missing_class" and (caught.file, caught.line) == ("bad.yaml", 2)
    assert "no_class" in str(caught)


@pytest.mark.parametrize("text, code, line", [
    ("id: x1\nkind: alpha\nclass: os.path.join\n", "bad_class", 3),                           # code outside the factory is never imported
    ("id: x1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.Nothing\n", "bad_class", 3),
    ("id: x1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.dataclass\n", "bad_class", 3),
    ("id: x1\nkind: wizard\nclass: hokma.factory.alphas.ema_cross.EmaCross\n", "bad_kind", 2),
    ("id: X-1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.EmaCross\n", "bad_id", 1),
    ("id: x1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.EmaCross\nline: XX\n", "bad_line", 4),
    ("id: x1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.EmaCross\nparams:\n  fast: 9000\n", "bad_param", 4),
    ("id: x1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.EmaCross\ndescription: <b>x</b>\n", "bad_description", 4),
    ("id: x1\nid: x2\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.EmaCross\n", "duplicate_key", 2),
    ("id: x1\nkind: [alpha]\nclass: hokma.factory.alphas.ema_cross.EmaCross\n", "bad_value", 2),
    ("id: x1\nkind: alpha\nclass: hokma.factory.alphas.ema_cross.EmaCross\nparams: [1]\n", "bad_value", 4),
    ("- id: a1\n  kind: alpha\n  class: hokma.factory.alphas.ema_cross.EmaCross\n- just text\n", "bad_entry", 4),
    ("id: x1\n  bad: [\n", "bad_yaml", 1),
    ("", "empty_file", 1),
])
def test_strict_loader_failures(tmp_path, text, code, line):
    caught = error(tmp_path, text)
    assert caught.code == code
    if code != "bad_yaml":
        assert caught.line == line
    assert caught.file == "bad.yaml"


def test_two_alphas_with_the_same_id_fail(tmp_path):
    registry = Registry.with_builtins()
    write(tmp_path, "a.yaml", GOOD)
    write(tmp_path, "b.yaml", GOOD)
    with pytest.raises(RegistryError) as caught:
        registry.load_dir(tmp_path)
    assert caught.value.code == "duplicate_id" and caught.value.file == "b.yaml" and "a.yaml:1" in str(caught.value)
    with pytest.raises(RegistryError) as builtin:
        Registry.with_builtins().load_file(write(tmp_path, "c.yaml", GOOD.replace("fast_ema", "tsmom")))
    assert builtin.value.code == "duplicate_id" and "builtin" in str(builtin.value)


def test_a_file_with_a_list_registers_every_entry_and_oversized_files_fail(tmp_path):
    registry = Registry()
    text = "- id: first\n  kind: alpha\n  class: hokma.factory.alphas.rsi.RsiReversion\n- id: second\n  kind: alpha\n  class: hokma.factory.alphas.macd.Macd\n"
    assert registry.load_file(write(tmp_path, "many.yaml", text)) == 2
    assert [r["id"] for r in factory_list(registry)] == ["first", "second"]
    with pytest.raises(RegistryError) as big:
        registry.load_file(write(tmp_path, "big.yaml", "id: x\n" + "# padding\n" * 10_000))
    assert big.value.code == "file_too_large"
    with pytest.raises(RegistryError) as gone:
        registry.load_file(tmp_path / "nothing.yaml")
    assert gone.value.code == "unreadable"


def test_a_strategy_entry_needs_init_and_on_bar(tmp_path):
    caught = error(tmp_path, "id: s1\nkind: strategy\nclass: hokma.factory.alphas.ema_cross.EmaCross\n")
    assert caught.code == "bad_class" and "does not implement init" in str(caught)
    with pytest.raises(ValueError):
        factory_list(Registry(), kind="portfolio")
