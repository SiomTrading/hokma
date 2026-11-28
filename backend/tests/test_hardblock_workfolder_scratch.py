"""The working folder of the fund and its scratch folder are never "stored keys" (1.2.1): a folder that the system cannot number (inode 0) does not
make the working folder look like Hokma's data folder, and the real secrets stay refused."""
import os
import types

import pytest
from hardblock_support import Box

from hokma.kit import guard, hardblock


@pytest.fixture
def box(tmp_path):
    b = Box(tmp_path)
    (b.work / "00. Hokma" / "scratch").mkdir(parents=True)
    return b


def unnumbered(monkeypatch):
    """A file system that reports inode 0 for every folder (a cloud synced or redirected folder on Windows)."""
    real = os.stat

    def stat(path, *a, **k):
        r = real(path, *a, **k)
        return types.SimpleNamespace(st_dev=r.st_dev, st_ino=0, st_mode=r.st_mode, st_nlink=r.st_nlink, st_size=r.st_size)

    monkeypatch.setattr(guard, "os", _fake_os(stat))


def _fake_os(stat):
    ns = types.SimpleNamespace(**{k: getattr(os, k) for k in dir(os) if not k.startswith("__")})
    ns.stat = stat
    return ns


def paths(box):
    return [str(box.work / "00. Hokma" / "scratch" / "probe.py"), str(box.work / "scratch" / "probe.py")]


def test_the_scratch_folder_of_the_fund_is_free_to_read_write_and_run(box, monkeypatch):
    unnumbered(monkeypatch)
    for p in paths(box):
        assert box.hit("Read", file_path=p) is None
        assert box.hit("Write", file_path=p, content="print(1)") is None
        assert box.hit("Bash", command=f"cat '{p}'") is None
        assert box.hit("Bash", command=f"ls '{os.path.dirname(p)}'") is None


def test_the_check_by_name_lets_the_working_folder_through_even_when_the_identity_says_it_is_the_data_folder(box, monkeypatch):
    monkeypatch.setattr(guard, "under_any", lambda path, roots: True)             # the worst case: everything looks like the data folder
    monkeypatch.setattr(hardblock, "under_any", lambda path, roots: True)
    p = paths(box)[0]
    ctx = box.ctx()
    hardblock.check_real_path(p, ctx)
    hardblock.check_read_path(p, ctx, str(box.work), content=True)


def test_the_real_secrets_stay_refused(box, monkeypatch):
    unnumbered(monkeypatch)
    for name in ("database.json", "claude-config/.credentials.json"):
        assert box.hit("Read", file_path=str(box.data / name)) == "secrets"
        assert box.hit("Bash", command=f"cat '{box.data / name}'") == "secrets"
    assert box.hit("Read", file_path=str(box.tmp / "home" / ".ssh" / "id_rsa")) == "secrets"
