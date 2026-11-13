"""SPEC-011 P11: backups as assets of a private GitHub Release, against a simulated GitHub."""
import io
import json
import re
import urllib.error

import pytest

from hokma.backup import github_release as gr
from hokma.backup.bundle import BackupError
from hokma.i18n import t

REPO = "student/proyectos"
TOKEN = "ghp_secret_token_value"


class FakeGitHub:
    """In-memory GitHub: one repo, optional release, assets with bytes."""

    def __init__(self, repo_exists=True):
        self.repo_exists, self.release, self.assets, self.next_id = repo_exists, None, {}, 1
        self.requests, self.fail_upload_at = [], None

    def _err(self, request, code):
        return urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b"{}"))

    def _asset_json(self, aid):
        a = self.assets[aid]
        return {"id": aid, "name": a["name"], "size": len(a["data"]), "created_at": a["name"][6:21],
                "url": f"{gr.API_BASE}/repos/{REPO}/releases/assets/{aid}"}

    def __call__(self, request):
        method, url = request.get_method(), request.full_url
        self.requests.append((method, url, request.headers.get("Authorization"), request.unredirected_hdrs))
        path = url.replace(gr.API_BASE, "").replace(gr.UPLOAD_BASE, "")
        if not self.repo_exists:
            raise self._err(request, 404)
        if method == "GET" and path == f"/repos/{REPO}":
            return self._ok({})
        if method == "GET" and path.endswith(f"/releases/tags/{gr.TAG}"):
            if self.release is None:
                raise self._err(request, 404)
            return self._ok(self.release)
        if method == "POST" and path == f"/repos/{REPO}/releases":
            body = json.loads(request.data)
            assert body["tag_name"] == gr.TAG and body["draft"] is False
            self.release = {"id": 7, "url": f"{gr.API_BASE}/repos/{REPO}/releases/7", "tag_name": gr.TAG}
            return self._ok(self.release)
        if method == "GET" and "/releases/7/assets?" in path:
            page = int(re.search(r"[?&]page=(\d+)", path).group(1))
            rows = [self._asset_json(i) for i in sorted(self.assets)]
            return self._ok(rows[(page - 1) * 100:page * 100])
        if method == "POST" and path.startswith("/repos/%s/releases/7/assets?" % REPO):
            name = re.search(r"name=([^&]+)", path).group(1)
            if self.fail_upload_at is not None and len(self.assets) == self.fail_upload_at:
                raise self._err(request, 500)
            data = request.data.read() if hasattr(request.data, "read") else request.data
            assert int(request.get_header("Content-length")) == len(data)
            aid, self.next_id = self.next_id, self.next_id + 1
            self.assets[aid] = {"name": name, "data": data}
            return self._ok(self._asset_json(aid))
        m = re.fullmatch(rf"/repos/{REPO}/releases/assets/(\d+)", path)
        if m and method == "DELETE":
            del self.assets[int(m.group(1))]
            return self._ok(None)
        if m and method == "GET":
            return io.BytesIO(self.assets[int(m.group(1))]["data"])
        raise AssertionError(f"unexpected {method} {url}")

    @staticmethod
    def _ok(payload):
        return io.BytesIO(b"" if payload is None else json.dumps(payload).encode())


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(gr, "_open", fake)
    return fake


def bundle(tmp_path, stamp, size=1000):
    path = tmp_path / f"hokma-{stamp}.hkb"
    path.write_bytes(bytes(i % 251 for i in range(size)))
    return path


def test_first_upload_creates_the_release_and_lists_the_backup(gh, tmp_path):
    path = bundle(tmp_path, "20260101T000000Z")
    result = gr.upload(path, REPO, token=TOKEN)
    assert result == {"name": path.name, "size": 1000, "parts": 1}
    assert gh.release["tag_name"] == gr.TAG
    listed = gr.list_backups(REPO, token=TOKEN)
    assert [(b["name"], b["size"], b["parts"]) for b in listed] == [(path.name, 1000, 1)]


def test_list_is_empty_before_the_first_backup(gh):
    assert gr.list_backups(REPO, token=TOKEN) == []
    assert gh.release is None                           # listing never creates the release


def test_only_the_seven_most_recent_backups_are_kept(gh, tmp_path):
    for day in range(1, 11):
        gr.upload(bundle(tmp_path, f"202601{day:02d}T000000Z", 50), REPO, token=TOKEN)
    names = [b["name"] for b in gr.list_backups(REPO, token=TOKEN)]
    assert len(names) == 7 and names == sorted(names, reverse=True)
    assert names[0].startswith("hokma-20260110") and names[-1].startswith("hokma-20260104")


def test_a_large_file_goes_in_parts_and_downloads_joined(gh, tmp_path):
    path = bundle(tmp_path, "20260201T000000Z", 2500)
    assert gr.upload(path, REPO, token=TOKEN, part_size=1000)["parts"] == 3
    assert sorted(a["name"] for a in gh.assets.values()) == [f"{path.name}.part00{i}" for i in (1, 2, 3)]
    assert [(b["size"], b["parts"]) for b in gr.list_backups(REPO, token=TOKEN)] == [(2500, 3)]
    out = tmp_path / "restore"
    out.mkdir()
    assert gr.download(REPO, path.name, out, token=TOKEN).read_bytes() == path.read_bytes()


def test_pruning_removes_every_part_of_an_old_backup(gh, tmp_path):
    gr.upload(bundle(tmp_path, "20260101T000000Z", 30), REPO, token=TOKEN, part_size=10, keep=1)
    gr.upload(bundle(tmp_path, "20260102T000000Z", 30), REPO, token=TOKEN, part_size=10, keep=1)
    assert {a["name"].split(".")[0] for a in gh.assets.values()} == {"hokma-20260102T000000Z"}
    assert len(gh.assets) == 3


def test_a_failed_upload_leaves_no_partial_backup_and_prunes_nothing(gh, tmp_path):
    gr.upload(bundle(tmp_path, "20260101T000000Z", 30), REPO, token=TOKEN, keep=1)
    gh.fail_upload_at = 2                               # first part of the new backup lands, the second fails
    with pytest.raises(BackupError):
        gr.upload(bundle(tmp_path, "20260102T000000Z", 30), REPO, token=TOKEN, part_size=10, keep=1)
    assert [a["name"] for a in gh.assets.values()] == ["hokma-20260101T000000Z.hkb"]


def test_download_of_a_missing_backup_says_so(gh, tmp_path):
    gr.upload(bundle(tmp_path, "20260101T000000Z"), REPO, token=TOKEN)
    with pytest.raises(BackupError) as caught:
        gr.download(REPO, "hokma-19990101T000000Z.hkb", tmp_path, token=TOKEN)
    assert str(caught.value) == t("backup.cloud.not_found")


def test_an_unreachable_repo_gives_a_plain_message(monkeypatch, tmp_path):
    monkeypatch.setattr(gr, "_open", FakeGitHub(repo_exists=False))
    with pytest.raises(BackupError, match="GitHub"):
        gr.upload(bundle(tmp_path, "20260101T000000Z"), REPO, token=TOKEN)


def test_network_down_gives_a_plain_message(monkeypatch, tmp_path):
    def down(request):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(gr, "_open", down)
    with pytest.raises(BackupError, match="no route"):
        gr.list_backups(REPO, token=TOKEN)


def test_without_a_token_it_asks_to_connect_github(monkeypatch):
    monkeypatch.setattr(gr.github, "github_token", lambda: None)
    with pytest.raises(BackupError, match="GitHub"):
        gr.list_backups(REPO)


def test_the_token_comes_from_update_and_never_leaves_the_auth_header(gh, monkeypatch, tmp_path):
    monkeypatch.setattr(gr.github, "github_token", lambda: TOKEN)
    gr.upload(bundle(tmp_path, "20260101T000000Z"), REPO)
    for method, url, plain_auth, unredirected in gh.requests:
        assert plain_auth is None                      # unredirected: a redirect to the CDN never gets it
        assert unredirected["Authorization"] == f"Bearer {TOKEN}"
        assert TOKEN not in url
