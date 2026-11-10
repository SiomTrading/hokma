"""An attached picture reaches the model whatever the engine of the conversation (task 3142).

Before 0.0.13 only Claude Code got the image block: the direct engine (API key) and Codex/Gemini kept the text and
dropped the picture without a word, so the model answered that it had no way to read images."""
import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from hardblock_support import Box, box  # noqa: F401  (fixture)
from hokma.engine import attachments as att
from hokma.engine.engine import ChatEngine
from hokma.engine.protocols import AttachmentRecord
from hokma.engines.base import user_images
from hokma.engines.direct import transcript
from hokma.engines.direct.loop import DirectAdapter
from test_engine_direct import Server, env, make_ctx, oa_text  # noqa: F401  (fixture)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC")
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(PNG).decode("ascii")}}


async def send(adapter, ctx, blocks):
    session = await adapter.open(ctx)
    events = [ev async for ev in session.send(blocks)]
    await session.close()
    return events


def test_user_images_keeps_only_base64_image_blocks():
    blocks = [IMAGE, {"type": "image", "source": {"type": "url"}}, {"type": "text", "text": "hi"}]
    assert user_images(blocks) == [IMAGE]


def test_direct_engine_openai_wire_sends_the_picture(env, box):
    srv = Server([oa_text("red")])
    try:
        events = asyncio.run(send(DirectAdapter(), make_ctx(box, srv.url), [IMAGE, {"type": "text", "text": "color?"}]))
    finally:
        srv.stop()
    assert events[-1]["status"] == "complete"
    user = srv.requests[0]["body"]["messages"][-1]
    assert user["role"] == "user" and user["content"][0] == {"type": "text", "text": "color?"}
    assert user["content"][1]["image_url"]["url"] == "data:image/png;base64," + IMAGE["source"]["data"]


def test_direct_engine_anthropic_wire_sends_the_picture(env, box):
    reply = [json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "red"}}),
             json.dumps({"type": "message_delta", "usage": {"output_tokens": 1}})]
    srv = Server([reply])
    try:
        ctx = make_ctx(box, srv.url, wire="anthropic", provider="anthropic")
        asyncio.run(send(DirectAdapter(), ctx, [IMAGE, {"type": "text", "text": "color?"}]))
    finally:
        srv.stop()
    content = srv.requests[0]["body"]["messages"][-1]["content"]
    assert content[0] == IMAGE and content[1] == {"type": "text", "text": "color?"}


def test_direct_engine_transcript_keeps_text_not_the_picture(env, box):
    srv = Server([oa_text("red")])
    try:
        asyncio.run(send(DirectAdapter(), make_ctx(box, srv.url), [IMAGE, {"type": "text", "text": "color?"}]))
    finally:
        srv.stop()
    saved = transcript.load("direct-9")
    assert saved[0] == {"role": "user", "text": "color?"}
    assert DirectAdapter().caps()["images"] is True


def _fake_engine(tmp_path, monkeypatch, caps_images):
    monkeypatch.setenv("HOKMA_DATA_DIR", str(tmp_path / "data"))
    digest = att.save_blob(PNG)
    record = AttachmentRecord(id="a1", name="photo.png", mime="image/png", size=len(PNG), sha256=digest)
    adapter = SimpleNamespace(caps=lambda: {"images": caps_images})
    store = SimpleNamespace(get_attachment=lambda i: record if i == "a1" else None)
    return SimpleNamespace(store=store, resolve_engine=lambda conv: (adapter, None, "m"))


def _build(fake, project_dir):
    inline = ChatEngine._inline_images(fake, conv=None)
    return inline, ChatEngine._build_content(fake, "what is this?", ["a1"], SimpleNamespace(path=str(project_dir)),
                                             inline)


def test_engine_with_images_gets_the_image_block(tmp_path, monkeypatch):
    inline, (blocks, meta) = _build(_fake_engine(tmp_path, monkeypatch, True), tmp_path / "proj")
    assert inline is True and blocks[0]["type"] == "image" and meta == {"attachments": ["a1"]}
    assert not (tmp_path / "proj" / "attachments").exists()


def test_engine_without_images_gets_the_picture_as_a_readable_file(tmp_path, monkeypatch):
    inline, (blocks, _) = _build(_fake_engine(tmp_path, monkeypatch, False), tmp_path / "proj")
    assert inline is False and [b["type"] for b in blocks] == ["text"]
    assert "attachments/photo.png" in blocks[0]["text"]
    assert Path(tmp_path / "proj" / "attachments" / "photo.png").read_bytes() == PNG
