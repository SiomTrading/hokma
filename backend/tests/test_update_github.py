"""GitHub Release update source: no network (the single `_open` and `subprocess.run` are mocked)."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from hokma.i18n import t
from hokma.update import archive, github, runner, safe_exec
from hokma.update.source import ManifestSource, UpdateError, resolve_source

TOKEN = "ghp_TESTTOKEN"
API = "https://api.github.com/repos/SiomTrading/hokma"
PACKAGE_BYTES = b"not a real archive"
PACKAGE_SHA = hashlib.sha256(PACKAGE_BYTES).hexdigest()


@pytest.fixture(autouse=True)
def lang(monkeypatch, tmp_path):
    monkeypatch.setenv("HOKMA_LANG", "en")
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOKMA_HOME", str(tmp_path / "work"))
    monkeypatch.delenv("HOKMA_UPDATE_SOURCE", raising=False)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def http_error(url, code, **headers):
    msg = Message()
    for k, v in headers.items():
        msg[k.replace("_", "-")] = v
    return urllib.error.HTTPError(url, code, "err", msg, None)


def release(tag="v0.2.0", sha_ok=True, with_sha_asset=True, with_package=True):
    version = tag.lstrip("v")
    assets = []
    if with_package:
        assets.append({"name": f"hokma-{version}.tar.gz", "url": f"{API}/releases/assets/11"})
    if with_sha_asset:
        assets.append({"name": f"hokma-{version}.tar.gz.sha256", "url": f"{API}/releases/assets/12"})
    return {"tag_name": tag, "assets": assets}


class Net:
    """Programmable fake for `_open`: routes by URL, records every request."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.routes[request.full_url]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            raise http_error(request.full_url, answer)
        return Resp(answer if isinstance(answer, bytes) else json.dumps(answer).encode())


def good_routes(rel=None, sha_line=None):
    return {
        f"{API}/releases/latest": rel or release(),
        f"{API}/releases/assets/12": (sha_line or f"{PACKAGE_SHA}  hokma-0.2.0.tar.gz\n").encode(),
    }


def with_login(monkeypatch, token=TOKEN):
    monkeypatch.setattr(github, "github_token", lambda: token)


# ---- token discovery ----------------------------------------------------------------------

GIT_BIN = "/opt/fake/bin/git"
GH_BIN = "/opt/fake/bin/gh"


def fake_run(git_out=None, git_rc=0, gh_out=None, gh_rc=0, calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if cmd[:3] == [GIT_BIN, "credential", "fill"]:
            if git_rc is None:
                raise OSError("no git")
            return subprocess.CompletedProcess(cmd, git_rc, git_out or "", "")
        return subprocess.CompletedProcess(cmd, gh_rc, gh_out or "", "")
    return run


def fake_programs(monkeypatch, git=True, gh=True):
    found = {"git": GIT_BIN if git else None, "gh": GH_BIN if gh else None}
    monkeypatch.setattr(safe_exec, "find_executable", lambda name: found[name])


def test_token_comes_from_git_credential_fill_in_an_isolated_process(monkeypatch, tmp_path):
    calls = []
    fake_programs(monkeypatch)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "evil"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "evil.cfg"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!touch x")
    monkeypatch.setenv("GIT_ASKPASS", "/evil/askpass")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "ghcfg"))
    monkeypatch.setattr(safe_exec.subprocess, "run",
                        fake_run(git_out=f"protocol=https\nhost=github.com\nusername=x\npassword={TOKEN}\n", calls=calls))
    assert github.github_token() == TOKEN
    cmd, kw = calls[0]
    assert cmd == [GIT_BIN, "credential", "fill"] and kw["input"] == "protocol=https\nhost=github.com\n\n"
    assert kw["timeout"] == 15
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0" and kw["env"]["GCM_INTERACTIVE"] == "never"
    bad = [k for k in kw["env"] if k.upper().startswith("GIT_CONFIG") or k in safe_exec.STRIPPED_NAMES]
    assert bad == []
    assert not any(TOKEN in str(c[0]) for c in calls)
    assert not any(c[0][1:3] in (["credential", "approve"], ["credential", "reject"]) for c in calls)


def test_helpers_run_outside_the_workdir_and_the_inherited_cwd(monkeypatch, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("HOKMA_HOME", str(work))
    monkeypatch.chdir(work)
    calls = []
    fake_programs(monkeypatch)
    monkeypatch.setattr(safe_exec.subprocess, "run", fake_run(git_rc=1, gh_out=TOKEN, calls=calls))
    assert github.github_token() == TOKEN
    assert [c[0][0] for c in calls] == [GIT_BIN, GH_BIN]                       # absolute paths
    for _, kw in calls:
        cwd = Path(kw["cwd"]).resolve()
        assert cwd != work.resolve() and work.resolve() not in cwd.parents and cwd != Path.cwd().resolve()
        assert kw["env"]["GIT_CEILING_DIRECTORIES"] == str(cwd.parent)
        assert not cwd.exists()                                               # removed after the call


def test_token_falls_back_to_gh_when_git_has_none(monkeypatch):
    calls = []
    fake_programs(monkeypatch)
    monkeypatch.setattr(safe_exec.subprocess, "run", fake_run(git_rc=1, gh_out=TOKEN + "\n", calls=calls))
    assert github.github_token() == TOKEN
    assert calls[1][0] == [GH_BIN, "auth", "token", "--hostname", "github.com"]


def test_no_token_anywhere(monkeypatch):
    fake_programs(monkeypatch, git=False, gh=False)
    assert github.github_token() is None


def test_programs_are_found_by_absolute_path_and_never_through_the_cwd_or_workdir(monkeypatch, tmp_path):
    work, bin_ok, bin_work, bin_cwd = (tmp_path / n for n in ("work", "ok", "work/bin", "cwdbin"))
    for d in (work, bin_ok, bin_work, bin_cwd):
        d.mkdir(parents=True, exist_ok=True)
    for d in (bin_ok, bin_work, bin_cwd):
        exe = d / "git"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    monkeypatch.setenv("HOKMA_HOME", str(work))
    monkeypatch.chdir(bin_cwd)
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "relative/bin", str(bin_cwd), str(bin_work), str(bin_ok)]))
    assert safe_exec.find_executable("git") == str(bin_ok / "git")
    monkeypatch.setenv("PATH", os.pathsep.join([".", str(bin_cwd), str(bin_work)]))
    assert safe_exec.find_executable("git") is None
    # a symlink in a trusted folder that points into the workdir is refused too
    link = bin_ok / "gh"
    link.symlink_to(bin_work / "git")
    monkeypatch.setenv("PATH", str(bin_ok))
    assert safe_exec.find_executable("gh") is None


def test_a_repository_in_the_cwd_cannot_run_its_credential_helper(monkeypatch, tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    repo, marker = tmp_path / "repo", tmp_path / "marker"
    repo.mkdir()
    (tmp_path / "home").mkdir()                       # no global helper may answer before the repository's
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run([git, "init", "-q"], cwd=repo, check=True)
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(f"[credential]\n\thelper = !touch {marker}; true\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    env.update(GIT_TERMINAL_PROMPT="0")
    subprocess.run([git, "credential", "fill"], cwd=repo, env=env, input="protocol=https\nhost=github.com\n\n",
                   capture_output=True, text=True)
    assert marker.exists(), "control: the repository config does run its helper when git is used naively"
    marker.unlink()

    monkeypatch.setenv("HOKMA_HOME", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))                          # even with the variable set
    monkeypatch.setattr(github, "_token_from_gh", lambda: None)
    github.github_token()
    assert not marker.exists()


def without_login(monkeypatch):
    monkeypatch.setattr(github, "github_token", lambda: None)


# ---- fetch ----------------------------------------------------------------------------------

def test_fetch_builds_the_manifest_from_the_latest_release(monkeypatch):
    with_login(monkeypatch)
    net = Net(good_routes())
    monkeypatch.setattr(github, "_open", net)
    manifest = github.GitHubReleaseSource().fetch()
    assert manifest.version == "0.2.0" and manifest.sha256 == PACKAGE_SHA
    assert manifest.url == f"{API}/releases/assets/11"             # API URL, not browser_download_url
    assert manifest.headers["Accept"] == "application/octet-stream"
    for request in net.requests:
        assert request.unredirected_hdrs["Authorization"] == f"Bearer {TOKEN}"
        assert "Authorization" not in request.headers
        assert request.headers["X-github-api-version"] == "2022-11-28"
    assert net.requests[0].headers["Accept"] == "application/vnd.github+json"
    assert TOKEN not in repr(manifest)


def test_fetch_errors_use_their_catalog_key(monkeypatch):
    with_login(monkeypatch)
    latest, repo = f"{API}/releases/latest", API
    cases = {
        "no_access": {latest: 404, repo: 404},
        "no_release": {latest: 404, repo: {"full_name": "SiomTrading/hokma"}},
        "rate_limited": {latest: http_error(latest, 403, X_RateLimit_Remaining="0")},
        "unreachable": {latest: urllib.error.URLError("name resolution failed")},
    }
    for key, routes in cases.items():
        monkeypatch.setattr(github, "_open", Net(routes))
        with pytest.raises(UpdateError) as exc:
            github.GitHubReleaseSource().fetch()
        expected = t(f"update.github.{key}", error="name resolution failed") if key == "unreachable" else t(f"update.github.{key}")
        assert str(exc.value) == expected, key
        assert TOKEN not in str(exc.value)


def test_fetch_without_token_is_anonymous(monkeypatch):
    without_login(monkeypatch)
    net = Net(good_routes())
    monkeypatch.setattr(github, "_open", net)
    manifest = github.GitHubReleaseSource().fetch()
    assert manifest.version == "0.2.0"
    assert len(net.requests) == 2
    for request in net.requests:
        assert "Authorization" not in request.headers and "Authorization" not in request.unredirected_hdrs
    assert "Authorization" not in manifest.headers and manifest.headers["Accept"] == "application/octet-stream"


def test_rejected_token_is_retried_anonymously(monkeypatch):
    with_login(monkeypatch)
    latest = f"{API}/releases/latest"
    net = Net(good_routes())
    real = net.__call__

    def open_(request):
        if request.unredirected_hdrs.get("Authorization"):
            net.requests.append(request)
            raise http_error(request.full_url, 401)
        return real(request)

    monkeypatch.setattr(github, "_open", open_)
    manifest = github.GitHubReleaseSource().fetch()
    assert manifest.version == "0.2.0" and "Authorization" not in manifest.headers
    assert net.requests[0].full_url == latest and net.requests[0].unredirected_hdrs["Authorization"]
    assert all("Authorization" not in r.unredirected_hdrs for r in net.requests[1:])


def test_anonymous_401_is_reported_as_unreachable(monkeypatch):
    without_login(monkeypatch)
    monkeypatch.setattr(github, "_open", Net({f"{API}/releases/latest": 401}))
    with pytest.raises(UpdateError) as exc:
        github.GitHubReleaseSource().fetch()
    assert str(exc.value) == t("update.github.unreachable", error="HTTP 401")


def test_anonymous_rate_limit(monkeypatch):
    without_login(monkeypatch)
    latest = f"{API}/releases/latest"
    monkeypatch.setattr(github, "_open", Net({latest: http_error(latest, 403, X_RateLimit_Remaining="0")}))
    with pytest.raises(UpdateError) as exc:
        github.GitHubReleaseSource().fetch()
    assert str(exc.value) == t("update.github.rate_limited")


def test_missing_asset_or_bad_sha_means_release_incomplete(monkeypatch):
    with_login(monkeypatch)
    expected = t("update.github.release_incomplete", version="0.2.0")
    bad = [good_routes(release(with_sha_asset=False)), good_routes(release(with_package=False)),
           good_routes(sha_line="abc  hokma-0.2.0.tar.gz\n"), good_routes(sha_line="z" * 64 + "  x\n")]
    for routes in bad:
        net = Net(routes)
        monkeypatch.setattr(github, "_open", net)
        with pytest.raises(UpdateError) as exc:
            github.GitHubReleaseSource().fetch()
        assert str(exc.value) == expected
        assert not any(r.full_url.endswith("/assets/11") for r in net.requests)       # package never requested


# ---- run_update with the GitHub source -------------------------------------------------------

def installed(monkeypatch, tmp_path, version="0.1.0"):
    (tmp_path / "data").mkdir(exist_ok=True)
    runner.write_pointer("a")
    monkeypatch.setattr(runner, "__version__", version)


def test_older_release_changes_nothing(monkeypatch, tmp_path):
    installed(monkeypatch, tmp_path)
    with_login(monkeypatch)
    sha_line = f"{PACKAGE_SHA}  hokma-0.0.9.tar.gz\n"
    routes = {f"{API}/releases/latest": release("v0.0.9"), f"{API}/releases/assets/12": sha_line.encode()}
    monkeypatch.setattr(github, "_open", Net(routes))
    with pytest.raises(UpdateError, match="older"):
        runner.run_update(github.GitHubReleaseSource())
    assert (tmp_path / "data" / "current").read_text().strip() == "a"
    assert not (tmp_path / "data" / "runtime").exists()


def test_same_version_is_up_to_date(monkeypatch, tmp_path):
    installed(monkeypatch, tmp_path, "0.2.0")
    with_login(monkeypatch)
    monkeypatch.setattr(github, "_open", Net(good_routes()))
    result = runner.run_update(github.GitHubReleaseSource())
    assert result.status == "up-to-date"


def test_package_that_does_not_match_the_sha_asset_is_not_extracted(monkeypatch, tmp_path):
    installed(monkeypatch, tmp_path)
    with_login(monkeypatch)
    monkeypatch.setattr(github, "_open", Net(good_routes(sha_line=f"{'0' * 64}  hokma-0.2.0.tar.gz\n")))
    seen = {}
    monkeypatch.setattr(archive.urllib.request, "urlopen", lambda req, timeout: seen.setdefault("req", req) and Resp(PACKAGE_BYTES))
    extracted = []
    monkeypatch.setattr(runner, "extract", lambda *a: extracted.append(a))
    with pytest.raises(UpdateError, match=t("update.package_sha_mismatch")):
        runner.run_update(github.GitHubReleaseSource())
    assert extracted == []
    assert (tmp_path / "data" / "current").read_text().strip() == "a"
    # the token went to the API asset URL only as an unredirected header
    assert seen["req"].full_url == f"{API}/releases/assets/11"
    assert seen["req"].unredirected_hdrs["Authorization"] == f"Bearer {TOKEN}"
    assert "Authorization" not in seen["req"].headers


def test_fetch_archive_without_headers_keeps_working_for_local_files(tmp_path):
    src = tmp_path / "p.bin"
    src.write_bytes(PACKAGE_BYTES)
    archive.fetch_archive(str(src), tmp_path / "out", PACKAGE_SHA)
    assert (tmp_path / "out").read_bytes() == PACKAGE_BYTES


# ---- the token never leaks --------------------------------------------------------------------

def test_token_is_not_in_errors_repr_json_or_files(monkeypatch, tmp_path, capsys):
    installed(monkeypatch, tmp_path)
    with_login(monkeypatch)
    monkeypatch.setattr(github, "_open", Net(good_routes(sha_line=f"{'0' * 64}  hokma-0.2.0.tar.gz\n")))
    monkeypatch.setattr(archive.urllib.request, "urlopen", lambda req, timeout: Resp(PACKAGE_BYTES))
    manifest = github.GitHubReleaseSource().fetch()
    assert TOKEN not in repr(manifest) and TOKEN not in str(manifest)
    with pytest.raises(UpdateError) as exc:
        runner.run_update(github.GitHubReleaseSource())
    assert TOKEN not in str(exc.value) and TOKEN not in repr(exc.value)

    # the CLI path, text and --json
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cli"))
    from hokma_cli import main as cli
    for flag in ([], ["--json"]):
        rc = cli.main(["update", *flag])
        out = capsys.readouterr()
        assert rc == 1 and TOKEN not in out.out and TOKEN not in out.err
    for path in (tmp_path).rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


# ---- override and resolution ------------------------------------------------------------------

def test_override_uses_the_manifest_source(monkeypatch, tmp_path):
    assert isinstance(resolve_source(None), github.GitHubReleaseSource)
    assert isinstance(resolve_source("m.json"), ManifestSource)
    monkeypatch.setenv("HOKMA_UPDATE_SOURCE", "https://x.test/m.json")
    source = resolve_source(None)
    assert isinstance(source, ManifestSource) and source.ref == "https://x.test/m.json"
    assert resolve_source("other.json").ref == "other.json"                # --from beats the variable
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / ("update-" + "source.txt")).write_text("legacy.json")      # the old file is no longer read
    monkeypatch.delenv("HOKMA_UPDATE_SOURCE")
    assert isinstance(resolve_source(None), github.GitHubReleaseSource)
