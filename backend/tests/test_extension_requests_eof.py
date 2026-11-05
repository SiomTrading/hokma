import pytest

from hokma.kit import extensions, requests
from hokma_cli import main as cli


def test_a_closed_terminal_confirms_nothing(tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    req = requests.request_mcp("local", {"command": "./run.sh"})
    answers = iter([req.id])

    def fake_input(prompt=""):
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)
    assert cli.main(["ext", "approve", req.id]) == 1          # id typed, then the terminal closed at the accept-word prompt
    assert extensions.load()[0]["mcpServers"] == {}
    assert [r.id for r in requests.list_pending()] == [req.id]
