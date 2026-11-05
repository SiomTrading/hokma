"""Voice notes: /api/transcribe with a fake transcriber, the model manager with a fake download, and the real
SQLite store. The real Whisper model is covered by a manual check run by the developers."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hokma.db import migrate, open_database
from hokma.engine import attachments as att
from hokma.engine.dbstore import DbChatStore
from hokma.engine.memory import MemoryStore
from hokma.rpc.ws import SESSION_TOKEN
from hokma.voice import model as voice_model
from hokma.voice.api import create_router
from hokma.voice.audio import sniff_audio
from hokma.voice.model import MARKER, ModelManager, classify_download_error
from hokma.voice.transcriber import Transcript

HEADERS = {"X-Hokma-Token": SESSION_TOKEN}
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50


class FakeTranscriber:
    def __init__(self, state="ready", available=True, text="hola mundo", fail=False):
        self.state, self._available, self.text, self.fail = state, available, text, fail
        self.calls: list[tuple[Path, str]] = []
        self.downloads = 0

    def available(self):
        return self._available

    def status(self):
        return {"state": self.state, "progress": 0.0, "bytes_done": 0, "bytes_total": 0, "error_code": None,
                "available": self._available, "model": "small"}

    def start_download(self):
        self.downloads += 1
        self.state = "downloading"

    def transcribe(self, path, language):
        self.calls.append((path, language))
        if self.fail:
            raise RuntimeError("decoder exploded with a secret path /x/y")
        return Transcript(self.text, "es", 1.5)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    store = MemoryStore()
    fake = FakeTranscriber()
    app = FastAPI()
    app.include_router(create_router(lambda: fake, lambda: SimpleNamespace(store=store)))
    return SimpleNamespace(client=TestClient(app), store=store, fake=fake, data=tmp_path / "data")


def post(setup, body=WEBM, ctype="audio/webm;codecs=opus", params="", headers=None):
    return setup.client.post(f"/api/transcribe{params}", content=body, headers={**HEADERS, "Content-Type": ctype,
                                                                              **(headers or {})})


def test_a_voice_note_is_stored_and_transcribed(setup):
    res = post(setup, headers={"X-Filename": "nota.webm"})
    assert res.status_code == 201
    body = res.json()
    assert body["text"] == "hola mundo" and body["mime"] == "audio/webm" and body["language"] == "es"
    assert body["name"] == "nota.webm" and body["size"] == len(WEBM)
    record = setup.store.get_attachment(body["attachment_id"])
    assert record is not None and record.mime == "audio/webm"
    assert (att.attachments_dir() / record.sha256).read_bytes() == WEBM
    assert setup.store.transcripts[record.id] == "hola mundo"
    assert setup.fake.calls[0][1] == "es_en"                     # default: auto between Spanish and English


def test_multipart_upload_and_language_choice(setup):
    res = setup.client.post("/api/transcribe?language=en", headers=HEADERS,
                            files={"file": ("voice-note.ogg", b"OggS" + b"\x00" * 100, "audio/ogg")})
    assert res.status_code == 201 and res.json()["name"] == "voice-note.ogg" and res.json()["mime"] == "audio/ogg"
    assert setup.fake.calls[0][1] == "en"


def test_token_is_required(setup):
    assert setup.client.post("/api/transcribe", content=WEBM).status_code == 401
    assert setup.client.post("/api/transcribe", content=WEBM, headers={"X-Hokma-Token": "wrong"}).status_code == 401
    assert setup.client.get("/api/transcribe/status").status_code == 401
    assert setup.client.post("/api/transcribe/model").status_code == 401
    assert post(setup, params=f"?token={SESSION_TOKEN}", headers={"X-Hokma-Token": ""}).status_code == 201


def test_what_is_not_audio_is_refused_and_not_stored(setup):
    res = post(setup, body=PNG, ctype="audio/webm")               # the bytes decide, not the declared type
    assert res.status_code == 415 and res.json()["detail"]["code"] == "not_audio"
    assert post(setup, body=b"").status_code == 400
    assert post(setup, params="?language=fr").status_code == 400
    assert setup.store.attachments == {} and setup.fake.calls == []


def test_size_limit(setup, monkeypatch):
    monkeypatch.setattr(att, "MAX_ATTACHMENT_BYTES", 100)
    monkeypatch.setattr(att, "save_blob", lambda data: pytest.fail("must not store"))
    assert post(setup, body=WEBM + b"\x00" * 70_000).status_code == 413


def test_model_not_downloaded_asks_for_the_download_and_keeps_nothing(setup):
    setup.fake.state = "missing"
    res = post(setup)
    assert res.status_code == 409 and res.json()["detail"]["code"] == "model_missing"
    assert setup.store.attachments == {} and setup.fake.calls == []
    started = setup.client.post("/api/transcribe/model", headers=HEADERS)
    assert started.status_code == 202 and started.json()["state"] == "downloading" and setup.fake.downloads == 1


def test_engine_missing_is_said_clearly(setup):
    setup.fake._available = False
    assert post(setup).json()["detail"]["code"] == "engine_missing"
    assert setup.client.post("/api/transcribe/model", headers=HEADERS).status_code == 501
    assert setup.client.get("/api/transcribe/status", headers=HEADERS).json()["available"] is False


def test_transcription_failure_does_not_leak_details_or_store_a_record(setup):
    setup.fake.fail = True
    res = post(setup)
    assert res.status_code == 500 and res.json()["detail"]["code"] == "transcribe_failed"
    assert "secret" not in res.text and setup.store.attachments == {}


def test_silence_returns_empty_text(setup):
    setup.fake.text = ""
    res = post(setup)
    assert res.status_code == 201 and res.json()["text"] == "" and setup.store.transcripts == {}


def test_messages_follow_accept_language(setup):
    setup.fake.state = "missing"
    es = post(setup, headers={"Accept-Language": "es"}).json()["detail"]["message"]
    en = post(setup, headers={"Accept-Language": "en-US,en;q=0.9"}).json()["detail"]["message"]
    assert es.startswith("El modelo") and en.startswith("The speech model")


def test_status_reports_the_download_error_localized(setup):
    data = setup.client.get("/api/transcribe/status", headers={**HEADERS, "Accept-Language": "en"}).json()
    assert data["state"] == "ready" and data["error"] is None and data["model"] == "small"


# ---- sniffing ----

@pytest.mark.parametrize("data,mime", [
    (WEBM, "audio/webm"), (b"OggS....", "audio/ogg"), (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
    (b"\x00\x00\x00\x18ftypM4A ", "audio/mp4"), (b"ID3\x04", "audio/mpeg"), (b"fLaC", "audio/flac"),
    (PNG, None), (b"hello", None)])
def test_sniff_audio(data, mime):
    assert sniff_audio(data) == mime


# ---- model manager ----

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path))
    return tmp_path


def wait_done(manager):
    manager.wait(10)
    return manager.snapshot()


def test_download_reports_progress_and_marks_the_model_ready(data_dir):
    seen = []

    def fetch(repo, target, report):
        assert repo == "Systran/faster-whisper-small"
        report("b", total=1000)
        report("b", done=250)
        seen.append(manager.snapshot())
        report("b", done=750)
        (target / "model.bin").write_bytes(b"x")

    manager = ModelManager("small", fetch=fetch)
    assert manager.snapshot()["state"] == "missing" and not manager.is_ready()
    manager.start_download()
    final = wait_done(manager)
    assert seen[0]["state"] == "downloading" and seen[0]["progress"] == 0.25
    assert final["state"] == "ready" and final["progress"] == 1.0 and manager.is_ready()
    assert (data_dir / "models" / "whisper" / "small" / MARKER).is_file()
    manager.start_download()                                       # already complete: nothing runs


def test_progress_follows_the_biggest_bar_not_the_sum_of_all(data_dir):
    manager = ModelManager("small", fetch=lambda *a: None)
    manager._download.state = "downloading"
    manager._report("tiny-file", total=2000, done=2000)          # a small file that finished
    manager._report("model", total=10_000)
    manager._report("reconstruct", total=10_000)                 # the library draws a second bar over the same bytes
    manager._report("model", done=2500)
    manager._report("reconstruct", done=2500)
    snap = manager.snapshot()
    assert (snap["bytes_done"], snap["bytes_total"], snap["progress"]) == (2500, 10_000, 0.25)


def test_offline_download_ends_in_a_clear_error_and_can_be_retried(data_dir):
    calls = []

    def fetch(repo, target, report):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("Name or service not known")
        (target / "model.bin").write_bytes(b"x")

    manager = ModelManager("small", fetch=fetch)
    manager.start_download()
    snap = wait_done(manager)
    assert snap["state"] == "error" and snap["error_code"] == "offline" and not manager.is_ready()
    manager.start_download()
    assert wait_done(manager)["state"] == "ready"


def test_partial_folder_without_marker_is_not_ready(data_dir):
    manager = ModelManager("small", fetch=lambda *a: None)
    manager.path.mkdir(parents=True)
    (manager.path / "model.bin").write_bytes(b"half")
    assert not manager.is_ready()
    manager.start_download()                                       # fetch writes nothing new but model.bin exists
    assert wait_done(manager)["state"] == "ready"


def test_error_classification():
    class ConnectError(Exception):
        pass

    wrapped = RuntimeError("x")
    wrapped.__cause__ = ConnectError("boom")
    assert classify_download_error(wrapped) == "offline"
    assert classify_download_error(ConnectionError()) == "offline"
    assert classify_download_error(OSError(28, "No space left")) == "disk"
    assert classify_download_error(ValueError("other")) == "download_failed"


def test_model_size_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("HOKMA_WHISPER_MODEL", "base")
    assert voice_model.model_size() == "base"
    monkeypatch.setenv("HOKMA_WHISPER_MODEL", "huge-unknown")
    assert voice_model.model_size() == "small"


# ---- real SQLite store ----

def test_audio_kind_and_transcript_in_sqlite(data_dir):
    migrate(data_dir / "hokma.db")
    db = open_database(data_dir / "hokma.db")
    store = DbChatStore(db)
    fake = FakeTranscriber(text="comprar mas")
    app = FastAPI()
    app.include_router(create_router(lambda: fake, lambda: SimpleNamespace(store=store)))
    res = TestClient(app).post("/api/transcribe", content=WEBM, headers={**HEADERS, "Content-Type": "audio/webm"})
    assert res.status_code == 201
    with db.read() as u:
        row = u.attachments.get(int(res.json()["attachment_id"]))
    assert row.kind == "audio" and row.transcript == "comprar mas" and row.mime_type == "audio/webm"
