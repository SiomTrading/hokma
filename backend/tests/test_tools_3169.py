"""Task 3169: Node and Bun portable, installed from lock.json with a sha256 check, and on the PATH of the engine."""
import hashlib
import io
import json
import os
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from hokma.core import tools
from hokma.engines import envfilter

LOCK = Path(__file__).resolve().parents[2] / "installer" / "lock.json"
PLATFORMS = ("linux-x86_64", "linux-aarch64", "macos-aarch64", "macos-x86_64", "windows-x86_64", "windows-aarch64")


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("BUN_INSTALL", raising=False)
    return tmp_path / "data"


def _sha(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _node_tar(version: str = "1.0.0") -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body, mode in ((f"node-v{version}-x/bin/node", b"#!/bin/sh\necho node\n", 0o755),
                                 (f"node-v{version}-x/lib/node_modules/npm/x.js", b"npm", 0o644),
                                 (f"node-v{version}-x/README.md", b"readme", 0o644)):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(body), mode
            tar.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo("node-v%s-x/bin/npm" % version)
        link.type, link.linkname = tarfile.SYMTYPE, "../lib/node_modules/npm/x.js"
        tar.addfile(link)
    return out.getvalue()


def _zip(top: str, name: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(f"{top}/{name}", b"binary")
    return out.getvalue()


def _lock(node: bytes, bun: bytes, node_version: str = "1.0.0") -> dict:
    key = tools.platform_key()
    ext = "zip" if key.startswith("windows") else "tar.gz"
    return {"node_version": node_version, "bun_version": "2.0.0", "platforms": {}, "tools": {
        f"node-{key}": {"url": f"https://example.invalid/node.{ext}", "sha256": _sha(node)},
        f"bun-{key}": {"url": "https://example.invalid/bun.zip", "sha256": _sha(bun)}}}


def _fetcher(blobs: dict):
    def fetch(url: str, dest: Path) -> str:
        blob = blobs[url.rsplit("/", 1)[-1]]
        dest.write_bytes(blob)
        return _sha(blob)
    return fetch


@pytest.mark.skipif(sys.platform == "win32", reason="posix layout")
def test_install_puts_node_and_bun_in_the_tools_folder(data):
    node, bun = _node_tar(), _zip("bun-linux-x64", "bun")
    lock = _lock(node, bun)
    seen = []
    done = tools.ensure_tools(seen.append, lock, _fetcher({"node.tar.gz": node, "bun.zip": bun}))
    assert done == ["node", "bun"]
    assert (data / "tools" / "node" / "bin" / "node").is_file() and os.access(data / "tools" / "node" / "bin" / "node", os.X_OK)
    assert (data / "tools" / "node" / "bin" / "npm").is_symlink() and (data / "tools" / "node" / "README.md").is_file()
    assert (data / "tools" / "bun" / "bun").is_file() and os.access(data / "tools" / "bun" / "bun", os.X_OK)
    assert tools.installed_version("node") == "1.0.0" and tools.installed_version("bun") == "2.0.0"
    assert tools.ensure_tools(seen.append, lock, _fetcher({})) == []                 # up to date: nothing is downloaded
    assert not list((data / "tmp").glob("*"))                                        # no leftovers


@pytest.mark.skipif(sys.platform == "win32", reason="posix layout")
def test_a_new_version_in_the_lock_replaces_the_old_one(data):
    bun = _zip("bun-linux-x64", "bun")
    node1, node2 = _node_tar("1.0.0"), _node_tar("1.1.0")
    tools.ensure_tools(lock=_lock(node1, bun), fetch=_fetcher({"node.tar.gz": node1, "bun.zip": bun}))
    assert tools.ensure_tools(lock=_lock(node2, bun, "1.1.0"), fetch=_fetcher({"node.tar.gz": node2})) == ["node"]
    assert tools.installed_version("node") == "1.1.0"


def test_a_wrong_sha256_installs_nothing(data):
    node, bun = _node_tar(), _zip("bun-linux-x64", "bun")
    lock = _lock(node, bun)
    with pytest.raises(tools.ToolsError, match="sha256 mismatch"):
        tools.ensure_tools(lock=lock, fetch=_fetcher({"node.tar.gz": node + b"x", "bun.zip": bun}))
    assert not (data / "tools").exists() or not any((data / "tools").iterdir())


def test_an_archive_without_the_program_installs_nothing(data):
    bad = _zip("x", "other")
    key = tools.platform_key()
    lock = {"node_version": "1", "tools": {f"node-{key}": {"url": "https://e.invalid/node.zip", "sha256": _sha(bad)}}}
    with pytest.raises(tools.ToolsError, match="no node"):
        tools.install_tool("node", lock, fetch=_fetcher({"node.zip": bad}))
    assert tools.installed_version("node") is None


def test_unsafe_archive_paths_are_refused(tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("top/../../escape", b"x")
    archive = tmp_path / "a.zip"
    archive.write_bytes(out.getvalue())
    with pytest.raises(tools.ToolsError, match="unsafe"):
        tools.unpack(archive, tmp_path / "out")
    assert not (tmp_path / "escape").exists()


def test_only_https_is_downloaded(tmp_path):
    with pytest.raises(tools.ToolsError, match="https"):
        tools.download("http://example.invalid/x", tmp_path / "x")
    with pytest.raises(tools.ToolsError, match="https"):
        tools.download("file:///etc/passwd", tmp_path / "x")


def test_lock_json_pins_node_and_bun_for_every_platform_with_a_sha256():
    lock = json.loads(LOCK.read_text())
    assert lock["node_version"] and lock["bun_version"]
    for tool, version in (("node", lock["node_version"]), ("bun", lock["bun_version"])):
        for platform in PLATFORMS:
            found = lock["tools"][f"{tool}-{platform}"]
            assert found["url"].startswith("https://") and version in found["url"]
            assert len(found["sha256"]) == 64 and int(found["sha256"], 16) >= 0
    assert lock["platforms"]["linux-x86_64"]["sha256"]          # the uv entries are untouched


def test_the_real_lock_is_readable_by_the_installer_code(data):
    found = tools.entry(tools.load_lock(LOCK), "node")
    assert found["url"].startswith("https://nodejs.org/dist/") and found["version"]


# ------------------------------------------------------------------------------------------------- uvx

@pytest.mark.skipif(sys.platform == "win32", reason="posix layout")
def test_uvx_comes_from_the_uv_archive_when_only_uv_was_copied(data):
    (data / "bin").mkdir(parents=True)
    (data / "bin" / "uv").write_text("uv")
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name in ("uv", "uvx"):
            info = tarfile.TarInfo(f"uv-x/{name}")
            info.size, info.mode = 3, 0o755
            tar.addfile(info, io.BytesIO(b"bin"))
    blob = out.getvalue()
    key = tools.platform_key()
    lock = {"platforms": {key: {"url": "https://e.invalid/uv.tar.gz", "sha256": _sha(blob)}}}
    assert tools.install_uvx(lock, fetch=_fetcher({"uv.tar.gz": blob})) is True
    assert tools.uvx_path().is_file() and os.access(tools.uvx_path(), os.X_OK)
    assert tools.install_uvx(lock, fetch=_fetcher({})) is False
    tools.uvx_path().unlink()
    with pytest.raises(tools.ToolsError, match="sha256 mismatch"):
        tools.install_uvx(lock, fetch=_fetcher({"uv.tar.gz": blob + b"x"}))
    assert not tools.uvx_path().exists()


def test_no_uvx_without_uv(data):
    assert tools.install_uvx({"platforms": {}}) is False


# --------------------------------------------------------------------------------- the PATH of the engine

def test_tool_folders_come_first_on_the_path(data):
    path = tools.path_with_tools(os.pathsep.join(["/usr/bin", str(data / "bin"), "/bin"]))
    parts = path.split(os.pathsep)
    assert parts[0] == str(data / "bin") and parts.count(str(data / "bin")) == 1
    assert str(tools.bin_dir("node")) in parts[:3] and str(tools.bin_dir("bun")) in parts[:3]
    assert parts.index(str(tools.bin_dir("bun"))) < parts.index("/usr/bin") < parts.index("/bin")
    home = Path(os.environ["HOME"])
    assert str(home / ".bun" / "bin") in parts and str(home / ".local" / "bin") in parts      # where plugin installers put things
    assert parts.index(str(home / ".bun" / "bin")) > parts.index("/bin")                     # after the owner's own PATH


def test_bun_install_decides_where_the_plugins_own_bun_goes(data, monkeypatch):
    monkeypatch.setenv("BUN_INSTALL", "/opt/bunhome")
    assert str(Path("/opt/bunhome") / "bin") in tools.path_with_tools("/usr/bin").split(os.pathsep)


def test_every_engine_gets_the_tool_folders_in_a_filtered_env(data):
    env = envfilter.filtered_env({"PATH": "/usr/bin", "HOME": "/h", "OPENAI_API_KEY": "sk-x", "BUN_INSTALL": "/b",
                                  "NPM_CONFIG_PREFIX": "/n", "GOPATH": "/g", "ANTHROPIC_API_KEY": "k"})
    assert env["PATH"].split(os.pathsep)[0] == str(data / "bin") and "/usr/bin" in env["PATH"]
    assert env["BUN_INSTALL"] == "/b" and env["NPM_CONFIG_PREFIX"] == "/n" and env["GOPATH"] == "/g"
    assert "OPENAI_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env


def test_the_windows_path_variable_keeps_its_letter_case(data):
    env = tools.with_tools({"Path": "C:\\Windows", "HOME": "x"})
    assert "PATH" not in env and "C:\\Windows" in env["Path"] and env["Path"].startswith(str(data / "bin"))


def test_the_terminal_gets_them_too(data):
    from hokma.terminal.session import build_env

    parts = build_env()["PATH"].split(os.pathsep)
    assert str(data / "bin") in parts and parts[0] != str(data / "bin")      # appended: it never shadows the owner's commands


def test_a_plugin_can_install_what_it_needs_in_its_own_folder(data, tmp_path):
    """The guard does not stand in the way: a plugin installer (a command the agent runs) and the plugin's own folder."""
    from hardblock_support import Box
    from hokma.kit import hardblock

    box = Box(tmp_path / "b")
    ctx = box.ctx()
    for command in ("curl -fsSL https://bun.sh/install | bash", "npm install --prefix {d}/claude-config/plugins/x",
                    "bun install", "uv tool install chroma-mcp", "pip install --target {d}/claude-config/plugins/x/py requests"):
        payload = {"tool_name": "Bash", "tool_input": {"command": command.format(d=box.data)}, "cwd": str(box.work)}
        assert hardblock.evaluate(payload, ctx) is None, command
