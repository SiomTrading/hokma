"""C5, backend and CLI side: provider keys and provider choices are configured in one place, `/api/connections`.
These tests read the source of the backend and of the CLI and fail when anything else offers to take a key or
pick a provider."""
import ast
import re
from pathlib import Path

from hokma.connections import api as connections_api
from hokma.market.ws import router as market_ws_router

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [*(ROOT / "backend" / "hokma").rglob("*.py"), *(ROOT / "cli").rglob("*.py")]
ROUTE_METHODS = {"get", "put", "post", "delete", "patch", "websocket", "api_route"}
# Words that mean "a key, a secret or a data provider". The path of a route, a CLI command or a CLI option that
# contains one of them belongs to Connections.
CONFIG_WORDS = re.compile(r"(?<![a-z])(keys?|api[-_]?keys?|secrets?|credentials?|providers?|connections?)(?![a-z])",
                          re.IGNORECASE)
CONNECTIONS_FOLDER = ROOT / "backend" / "hokma" / "connections"


def string_args(call: ast.Call) -> list[str]:
    return [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def routes_in(path: Path) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ROUTE_METHODS:
            found += [(arg, node.lineno) for arg in string_args(node) if arg.startswith("/")]
    return found


def cli_names_in(path: Path) -> list[tuple[str, int]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
                "add_parser", "add_argument", "add_subparsers", "add_mutually_exclusive_group"):
            found += [(arg, node.lineno) for arg in string_args(node)]
    return found


def test_no_route_outside_connections_takes_a_key_or_a_provider():
    offenders = []
    for path in SOURCES:
        if CONNECTIONS_FOLDER in path.parents:
            continue
        for route, line in routes_in(path):
            if CONFIG_WORDS.search(route):
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {route}")
    assert offenders == [], "keys and providers are configured only under /api/connections"


def test_every_route_of_the_connections_module_lives_under_its_prefix():
    paths = {route.path for route in connections_api.router.routes}
    assert paths and all(p.startswith("/api/connections") for p in paths)
    assert {route.path for route in market_ws_router.routes} == {"/ws/market"}


def test_the_cli_has_no_command_or_option_for_keys_or_providers():
    offenders = []
    for path in (ROOT / "cli").rglob("*.py"):
        for name, line in cli_names_in(path):
            if CONFIG_WORDS.search(name):
                offenders.append(f"{path.relative_to(ROOT)}:{line}: {name}")
    assert offenders == []


def test_hokma_help_has_nothing_about_keys_or_providers(capsys):
    from hokma_cli import main as cli

    texts = []
    for argv in (["--help"], ["ext", "--help"], ["update", "--help"], ["doctor", "--help"]):
        try:
            cli.main(argv)
        except SystemExit:
            pass
        texts.append(capsys.readouterr().out)
    assert texts[0].strip()
    for text in texts:
        assert not CONFIG_WORDS.search(text), text


def test_nothing_in_the_backend_reads_or_writes_a_market_settings_file_or_a_key_variable():
    offenders = []
    for path in (ROOT / "backend" / "hokma").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in ("market.json", "MARKET_API_KEY", "MARKET_KEY", "ALPACA_API_KEY", "FINNHUB_API_KEY",
                       "DATABENTO_API_KEY"):
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == []


def test_the_connections_module_never_touches_the_process_environment_or_a_file_with_a_key():
    for path in CONNECTIONS_FOLDER.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "os.environ[" not in text and "putenv" not in text and "setdefault(" not in text, path
    store = (CONNECTIONS_FOLDER / "store.py").read_text(encoding="utf-8")
    assert "keyring.set_password" in store and store.count("write_text") == 0


def test_the_scanner_finds_a_planted_key_route(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text('@router.put("/api/market/api-key")\ndef save(): ...\n'
                       '@router.get("/api/market/candles")\ndef candles(): ...\n'
                       'parser.add_argument("--provider")\n', encoding="utf-8")
    assert [r for r, _ in routes_in(planted) if CONFIG_WORDS.search(r)] == ["/api/market/api-key"]
    assert [n for n, _ in cli_names_in(planted) if CONFIG_WORDS.search(n)] == ["--provider"]
