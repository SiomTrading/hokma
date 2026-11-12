"""SPEC-010 P10: embedder with a fake tokenizer and a fake ONNX session (CI never loads the 118 MB model)."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from hokma.library import embedder as emb


class FakeTokenizer:
    """One token per character, pad id 0; records what it was asked to encode."""

    def __init__(self):
        self.seen: list[list[str]] = []

    def encode_batch(self, texts):
        self.seen.append(list(texts))
        longest = max(len(t) for t in texts)
        return [SimpleNamespace(ids=[ord(c) % 50 + 1 for c in t] + [0] * (longest - len(t)),
                                attention_mask=[1] * len(t) + [0] * (longest - len(t))) for t in texts]


class FakeSession:
    """Hidden state = token id broadcast over 384 dims, plus a huge value on padding to prove it is masked out."""

    def __init__(self, with_types=False):
        self.calls = []
        self.threads = set()
        self.with_types = with_types

    def get_inputs(self):
        names = ["input_ids", "attention_mask"] + (["token_type_ids"] if self.with_types else [])
        return [SimpleNamespace(name=n) for n in names]

    def run(self, _outputs, feeds):
        self.calls.append(feeds)
        self.threads.add(threading.current_thread().name)
        ids = feeds["input_ids"].astype(np.float32)
        hidden = np.repeat(ids[:, :, None], emb.DIMENSIONS, axis=2)
        hidden[feeds["attention_mask"] == 0] = 1e6
        hidden = hidden + np.arange(emb.DIMENSIONS, dtype=np.float32)[None, None, :]
        return [hidden]


def make(batch=4, with_types=False):
    e = emb.Embedder("m.onnx", "t.json", batch_size=batch)
    e._tokenizer = FakeTokenizer()
    e._session = FakeSession(with_types)
    e._input_names = {i.name for i in e._session.get_inputs()}
    return e


def test_vectors_are_384_float32_and_unit_length():
    e = make()
    out = e.embed_passages(["alpha", "beta gamma", "x"])
    assert out.shape == (3, 384) and out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)


def test_prefixes_query_and_passage():
    e = make()
    e.embed_query("what is risk")
    e.embed_passages(["some text"])
    seen = [t for batch in e._tokenizer.seen for t in batch]
    assert seen == ["query: what is risk", "passage: some text"]


def test_padding_is_excluded_from_the_mean():
    e = make()
    alone = e.embed_passages(["aa"])[0]
    padded = e.embed_passages(["aa", "a much longer passage that forces padding"])[0]
    assert np.allclose(alone, padded, atol=1e-5)


def test_order_preserved_across_length_sorted_batches():
    e = make(batch=2)
    texts = ["zz" * 30, "a", "mid length", "b" * 5, "q" * 50]
    together = e.embed_passages(texts)
    for i, text in enumerate(texts):
        assert np.allclose(together[i], e.embed_passages([text])[0], atol=1e-5)


def test_batches_have_at_most_batch_size_rows():
    e = make(batch=3)
    e.embed_passages([f"t{i}" for i in range(8)])
    assert [c["input_ids"].shape[0] for c in e._session.calls] == [3, 3, 2]


def test_token_type_ids_only_when_the_model_asks():
    plain, typed = make(), make(with_types=True)
    plain.embed_query("q")
    typed.embed_query("q")
    assert "token_type_ids" not in plain._session.calls[0]
    assert not typed._session.calls[0]["token_type_ids"].any()


def test_empty_input_gives_empty_matrix_without_loading():
    e = emb.Embedder("missing.onnx", "missing.json")
    assert e.embed_passages([]).shape == (0, 384)


def test_inference_runs_in_the_dedicated_low_priority_thread(monkeypatch):
    lowered = []
    monkeypatch.setattr(emb, "lower_current_thread_priority", lambda: lowered.append(threading.current_thread().name))
    e = emb.Embedder("m.onnx", "t.json")
    e._tokenizer, e._session = FakeTokenizer(), FakeSession()
    e._input_names = {"input_ids", "attention_mask"}
    e.embed_query("q")
    assert lowered and lowered[0].startswith("hokma-embed")
    assert e._session.threads == {lowered[0]}
    assert threading.current_thread().name not in e._session.threads


def test_threads_are_capped_at_two():
    assert emb.Embedder("m", "t", threads=16).threads == 2
    assert emb.Embedder("m", "t", threads=0).threads == 1


def test_missing_model_raises_embedder_error(tmp_path):
    e = emb.Embedder(tmp_path / "no.onnx", tmp_path / "no.json")
    with pytest.raises(emb.EmbedderError):
        e.embed_query("q")


def test_lowering_priority_never_raises(monkeypatch):
    monkeypatch.setattr(emb.os, "setpriority", lambda *a: (_ for _ in ()).throw(PermissionError()), raising=False)
    emb.lower_current_thread_priority()
