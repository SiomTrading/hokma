"""Library search (SPEC-010 P12, section 4.6): meaning first, words as an inner complement, RRF, the "nothing found"
threshold and the Library folder filter. Synthetic vectors, no model, the same cases on every store.

The `slow` cases at the end use the real embedding model (HOKMA_TEST_MODEL_DIR) and are the ones that calibrate
MIN_SEMANTIC_SIM. Postgres cases are skipped (with the reason) when HOKMA_TEST_POSTGRES_URL is not set.
"""
import os
from pathlib import Path

import pytest
from dbsupport import env  # noqa: F401  (fixture)
from pgsupport import pg_admin_url, pg_template, pg_url, pg_url_migrated  # noqa: F401  (fixtures)
from test_vector_store import MODEL, fill, seed, similarity, store_kind, store_of, unit_vector  # noqa: F401

from hokma.db.errors import NotFound
from hokma.library import search as lib_search
from hokma.library.search import MIN_SEMANTIC_SIM, RRF_K, fuse, search


def asker(vector):
    return lambda _text: vector


def ask(db, kind, query, vector, **kw):
    with db.read() as u:
        return search(u, asker(vector), query, model_id=MODEL, store=store_of(u, kind), **kw)


def names(hits):
    return [h.document for h in hits]


# ------------------------------------------------------------------------------------------------ fusion
def test_fuse_raises_a_passage_that_appears_in_both_lists():
    fused = dict(fuse([1, 2, 3], [3, 4]))
    assert fused[3] == pytest.approx(1 / (RRF_K + 3) + 1 / (RRF_K + 1))
    assert [cid for cid, _ in fuse([1, 2, 3], [3, 4])][0] == 3          # 3 beats 1 because both lists have it
    assert fuse([], []) == []
    assert [cid for cid, _ in fuse([5], [4])] == [4, 5]                  # equal scores: the smaller id first


# ------------------------------------------------------------------------------------------------ meaning
def test_a_question_close_in_meaning_finds_the_passage_without_sharing_any_word(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    target = layout["chunks"]["papers.pdf"][2]
    question = unit_vector(1, near=vecs[target], noise=0.02)               # "a question in Spanish" for an English text
    assert similarity(question, vecs[target]) > MIN_SEMANTIC_SIM
    hits = ask(db, kind, "como se reduce el riesgo", question, library_ids=[layout["lib"]])
    assert hits and hits[0].chunk_id == target
    assert hits[0].document == "papers.pdf" and hits[0].origin == "library" and hits[0].library_path == "Papers"
    assert hits[0].section == "page 2" and "papers.pdf" in hits[0].excerpt and hits[0].format == "txt"


def test_ten_questions_with_no_relation_find_nothing(store_kind):
    db, kind = store_kind
    layout = seed(db)
    fill(db, kind, layout)
    for n in range(10):
        stray = unit_vector(900 + n)                                       # unrelated to every passage
        assert ask(db, kind, f"pregunta sin relacion numero{n}", stray, library_ids=[layout["lib"]]) == []


def test_the_threshold_is_the_line_between_an_answer_and_nothing(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    target = layout["chunks"]["root.pdf"][0]
    question = unit_vector(7, near=vecs[target], noise=0.02)
    sim = similarity(question, vecs[target])
    assert ask(db, kind, "algo distinto", question, library_ids=[layout["lib"]], min_similarity=sim - 0.01)
    assert ask(db, kind, "algo distinto", question, library_ids=[layout["lib"]], min_similarity=sim + 0.01) == []


# ------------------------------------------------------------------------------------------------ words
def test_an_exact_word_is_found_even_when_the_meaning_does_not_point_to_it(store_kind):
    db, kind = store_kind
    layout = seed(db)
    fill(db, kind, layout)
    target = layout["chunks"]["other.pdf"][3]                              # body: "text of other.pdf 3"
    stray = unit_vector(4242)                                              # the vector of the question points elsewhere
    hits = ask(db, kind, "other.pdf 3", stray, library_ids=[layout["lib"]])
    assert [h.chunk_id for h in hits] == [target]


def test_a_partial_word_overlap_does_not_make_a_passage_an_answer(store_kind):
    """Sharing one common word is not an exact match: without meaning the passage stays out."""
    db, kind = store_kind
    layout = seed(db)
    fill(db, kind, layout)
    assert ask(db, kind, "text of something else entirely", unit_vector(5151), library_ids=[layout["lib"]]) == []


def test_words_and_meaning_together_rank_the_passage_in_both_first(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    both = layout["chunks"]["deep.pdf"][1]
    near = layout["chunks"]["root.pdf"][4]
    question = unit_vector(11, near=vecs[near], noise=0.02)
    hits = ask(db, kind, "deep.pdf 1", question, library_ids=[layout["lib"]])
    assert {h.chunk_id for h in hits} >= {both, near}                      # one by words, one by meaning


# ------------------------------------------------------------------------------------------------ Library folders
def test_a_library_folder_filter_includes_its_subfolders_and_nothing_else(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    probe = unit_vector(21, near=vecs[layout["chunks"]["deep.pdf"][0]], noise=0.5)   # a wide probe: many close passages
    everywhere = ask(db, kind, "zzz", probe, library_ids=[layout["lib"]], min_similarity=-1.0, limit=50)
    assert {"root.pdf", "papers.pdf", "deep.pdf", "other.pdf"} <= set(names(everywhere))
    inside = ask(db, kind, "zzz", probe, library_ids=[layout["lib"]], library_folder_id=layout["papers"],
                 min_similarity=-1.0, limit=50)
    assert set(names(inside)) == {"papers.pdf", "deep.pdf"}                  # Papers and Papers/2026
    deep = ask(db, kind, "zzz", probe, library_ids=[layout["lib"]], library_folder_id=layout["y2026"],
               min_similarity=-1.0, limit=50)
    assert set(names(deep)) == {"deep.pdf"} and deep[0].library_path == "Papers/2026"


def test_a_folder_that_does_not_exist_is_an_error_not_an_empty_answer(store_kind):
    db, kind = store_kind
    layout = seed(db)
    fill(db, kind, layout)
    with pytest.raises(NotFound):
        ask(db, kind, "x", unit_vector(1), library_ids=[layout["lib"]], library_folder_id=999_999)


def test_with_a_library_folder_the_disk_folders_stay_out(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    probe = unit_vector(31, near=vecs[layout["chunks"]["disk.md"][0]], noise=0.5)
    both = ask(db, kind, "zzz", probe, library_ids=[layout["lib"]], folder_ids=[layout["pf"]],
               min_similarity=-1.0, limit=50)
    assert "disk.md" in names(both)
    only = ask(db, kind, "zzz", probe, library_ids=[layout["lib"]], folder_ids=[layout["pf"]],
               library_folder_id=layout["papers"], min_similarity=-1.0, limit=50)
    assert "disk.md" not in names(only)


# ------------------------------------------------------------------------------------------------ origin
def test_a_passage_from_a_disk_folder_says_where_it_was_read(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    target = layout["chunks"]["disk.md"][1]
    hits = ask(db, kind, "zzz", unit_vector(41, near=vecs[target], noise=0.02), folder_ids=[layout["pf"]])
    assert hits[0].chunk_id == target and hits[0].origin == "folder:notes" and hits[0].library_path == ""
    assert set(hits[0].public()) == {"document", "section", "excerpt", "path", "format", "source_url", "origin",
                                     "library_path"}                       # no "match" kind for the student


def test_documents_still_indexing_and_empty_questions_give_nothing(store_kind):
    db, kind = store_kind
    layout = seed(db, status="indexing")
    vecs = fill(db, kind, layout)
    target = layout["chunks"]["root.pdf"][0]
    question = unit_vector(51, near=vecs[target], noise=0.02)
    assert ask(db, kind, "root.pdf 0", question, library_ids=[layout["lib"]]) == []
    assert ask(db, kind, "   ", question, library_ids=[layout["lib"]]) == []
    assert ask(db, kind, "root.pdf 0", question) == []                     # nothing to search in


def test_the_limit_is_respected(store_kind):
    db, kind = store_kind
    layout = seed(db)
    vecs = fill(db, kind, layout)
    probe = unit_vector(61, near=vecs[layout["chunks"]["root.pdf"][0]], noise=0.5)
    assert len(ask(db, kind, "zzz", probe, library_ids=[layout["lib"]], min_similarity=-1.0, limit=3)) == 3


# ------------------------------------------------------------------------------------------------ real model
MODEL_DIR = os.environ.get("HOKMA_TEST_MODEL_DIR")

PASSAGES = [
    "Position sizing limits the risk of each trade to a fixed percentage of the account, usually between one and two percent, so that a losing streak cannot wipe out the capital.",
    "The moving average crossover strategy buys when the fast average rises above the slow average and sells when it falls below, and it works best in markets that trend.",
    "Implied volatility reflects the market's expectation of future price swings and it tends to rise before earnings announcements, which makes options more expensive.",
    "Diversification across uncorrelated assets reduces the variance of a portfolio without necessarily lowering its expected return.",
    "A stop loss order closes a position automatically when the price reaches a chosen level, which caps the loss on a single trade.",
    "Backtesting a strategy on historical data can be misleading because of overfitting: the rules fit the past noise and fail on new data.",
    "The Federal Reserve raises interest rates to slow inflation, and higher rates usually put pressure on growth stocks and long duration bonds.",
    "Market makers quote both a bid and an ask price and earn the spread, which compensates them for the risk of holding inventory.",
    "The NVDA ticker closed the session above its 50 day average after the earnings release.",
]
ANSWERED = [   # (question in Spanish, index of the English passage that answers it)
    ("¿Qué porcentaje de la cuenta debo arriesgar en cada operación?", 0),
    ("¿Cuándo compra la estrategia de cruce de medias móviles?", 1),
    ("¿Por qué las opciones se encarecen antes de los resultados de una empresa?", 2),
    ("¿Cómo se reduce la varianza de un portafolio?", 3),
    ("¿Para qué sirve una orden de stop loss?", 4),
    ("¿Por qué un backtest puede engañar?", 5),
    ("¿Qué pasa con las acciones de crecimiento cuando suben las tasas?", 6),
    ("¿Cómo ganan dinero los creadores de mercado?", 7),
]
UNRELATED = [
    "¿Cuál es la capital de Mongolia?", "¿Cuántas patas tiene una araña?", "¿Cómo cambio la llanta de una bicicleta?",
    "¿Cómo se escribe una carta de renuncia?", "¿Quién ganó el mundial de fútbol de 1998?",
    "¿Qué significa la palabra serendipia?", "¿Qué temperatura hace en Bogotá en diciembre?",
    "¿Cuál es la mejor serie de ciencia ficción?", "¿Cómo se llama el río más largo de África?",
    "¿Cuántos huesos tiene el cuerpo humano?",
]


@pytest.fixture
def real_library(env):  # noqa: F811
    from hokma.db import get_database, migrate
    from hokma.library.embedder import Embedder
    folder = Path(MODEL_DIR)
    embedder = Embedder(folder / "model_quantized.onnx", folder / "tokenizer.json")
    migrate()
    db = get_database()
    from hokma.db.vectors import prepare
    assert prepare(db).store == "sqlite-vec"
    with db.write() as u:
        lib = u.library.ensure_student_library()
        doc = u.documents.add_document(
            lib.id, name="notes.md", stored_path="/data/notes.md", mime_type="text/markdown", format="md",
            size_bytes=10, sha256="a" * 64, status="indexing", chunks=[(f"part {i}", p) for i, p in enumerate(PASSAGES)])
        ids = [int(r["id"]) for r in u.session.query("SELECT id FROM doc_chunks WHERE document_id = :d ORDER BY ord",
                                                     {"d": doc.id})]
    from hokma.db.vectors import backfill
    result = backfill(db, lambda texts: embedder.embed_passages(texts), MODEL)
    assert result.error is None and result.documents_ready == 1
    yield db, lib.id, ids, embedder
    embedder.close()


@pytest.mark.slow
@pytest.mark.skipif(not MODEL_DIR, reason="HOKMA_TEST_MODEL_DIR is not set")
def test_real_model_a_spanish_question_finds_the_english_passage(real_library):
    db, lib, ids, embedder = real_library
    for question, index in ANSWERED:
        with db.read() as u:
            hits = search(u, embedder.embed_query, question, model_id=MODEL, library_ids=[lib])
        assert hits and hits[0].chunk_id == ids[index], question


@pytest.mark.slow
@pytest.mark.skipif(not MODEL_DIR, reason="HOKMA_TEST_MODEL_DIR is not set")
def test_real_model_ten_questions_with_no_relation_find_nothing(real_library):
    db, lib, ids, embedder = real_library
    for question in UNRELATED:
        with db.read() as u:
            assert search(u, embedder.embed_query, question, model_id=MODEL, library_ids=[lib]) == [], question


@pytest.mark.slow
@pytest.mark.skipif(not MODEL_DIR, reason="HOKMA_TEST_MODEL_DIR is not set")
def test_real_model_a_ticker_appears_by_its_words(real_library):
    db, lib, ids, embedder = real_library
    with db.read() as u:
        hits = search(u, embedder.embed_query, "NVDA", model_id=MODEL, library_ids=[lib])
    assert hits and hits[0].chunk_id == ids[8]


@pytest.mark.slow
@pytest.mark.skipif(not MODEL_DIR, reason="HOKMA_TEST_MODEL_DIR is not set")
def test_real_model_the_threshold_sits_between_the_two_groups(real_library):
    db, lib, ids, embedder = real_library
    with db.read() as u:
        store = lib_search.open_store(u.session)
        best = lambda q: store.search(embedder.embed_query(q), library_ids=[lib], limit=1, model_id=MODEL)[0].similarity
        answered = min(best(q) for q, _ in ANSWERED)
        stray = max(best(q) for q in UNRELATED)
    assert stray < MIN_SEMANTIC_SIM <= answered, (stray, answered)
