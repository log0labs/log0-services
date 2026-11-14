"""Step 5.2: the embedders. The Hugging Face one is tested against a faked HTTP server (respx)."""

import math

import httpx
import pytest
import respx

from app.config import Settings, get_settings
from app.retrieval.embeddings import (
    EMBEDDING_DIMENSION,
    EmbeddingError,
    HashingEmbedder,
    HuggingFaceEmbedder,
    build_embedder,
    to_pgvector,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")

BASE = "https://hf.test/hf-inference"
URL = f"{BASE}/models/sentence-transformers/all-MiniLM-L6-v2/pipeline/feature-extraction"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # the vectors are unit length


# ------------------------------------------------------------------------- hashing


async def test_hash_vectors_have_the_right_size_and_unit_length():
    [v] = await HashingEmbedder().embed_documents(["the connection pool is exhausted"])
    assert len(v) == EMBEDDING_DIMENSION
    assert math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0)


async def test_hash_embedding_is_deterministic_across_calls():
    e = HashingEmbedder()
    assert await e.embed_query("database timeout") == await e.embed_query("database timeout")


async def test_shared_words_mean_similar_vectors():
    e = HashingEmbedder()
    [pool, cache] = await e.embed_documents(
        ["Connection pool exhausted: raise the pool size", "Flush the redis cache after a deploy"]
    )
    query = await e.embed_query("connection pool exhausted")
    assert cosine(query, pool) > 0.5
    assert cosine(query, pool) > cosine(query, cache) + 0.4


async def test_case_and_punctuation_do_not_matter():
    e = HashingEmbedder()
    a, b = await e.embed_query("Connection-Pool EXHAUSTED!"), await e.embed_query("connection pool exhausted")
    assert cosine(a, b) == pytest.approx(1.0)


async def test_word_order_changes_the_vector_a_little_through_word_pairs():
    e = HashingEmbedder()
    a, b = await e.embed_query("pool connection"), await e.embed_query("connection pool")
    assert 0.5 < cosine(a, b) < 1.0


async def test_empty_text_gives_a_zero_vector_not_an_error():
    v = await HashingEmbedder().embed_query("   the of and   ")  # only stop words
    assert v == [0.0] * EMBEDDING_DIMENSION


async def test_very_long_text_is_cut_not_refused():
    v = await HashingEmbedder().embed_query("word " * 100_000)
    assert len(v) == EMBEDDING_DIMENSION


# ---------------------------------------------------------------------- pgvector text


async def test_pgvector_text_form():
    assert to_pgvector([1.0, 0.5, -2.0]) == "[1.0,0.5,-2.0]"
    assert to_pgvector([1, 2]) == "[1.0,2.0]"


# ------------------------------------------------------------------------ hugging face


def hf(monkeypatch) -> HuggingFaceEmbedder:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "huggingface")
    monkeypatch.setenv("HF_TOKEN", "hf_secret_token_value")
    monkeypatch.setenv("HF_EMBEDDING_BASE_URL", BASE)
    get_settings.cache_clear()
    embedder = build_embedder(get_settings())
    assert isinstance(embedder, HuggingFaceEmbedder)
    return embedder


def vectors(n: int, value: float = 0.5) -> list[list[float]]:
    return [[value] * EMBEDDING_DIMENSION for _ in range(n)]


@respx.mock
async def test_hf_sends_the_token_and_returns_normalised_vectors(monkeypatch):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=vectors(2, 3.0)))
    embedder = hf(monkeypatch)
    result = await embedder.embed_documents(["one", "two"])

    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer hf_secret_token_value"
    assert b'"inputs"' in request.content and b"one" in request.content
    assert all(math.sqrt(sum(x * x for x in v)) == pytest.approx(1.0) for v in result)
    assert embedder.model_id == "sentence-transformers/all-MiniLM-L6-v2"
    await embedder.close()


@respx.mock
async def test_hf_sends_documents_in_batches_of_32(monkeypatch):
    route = respx.post(URL).mock(side_effect=lambda req: httpx.Response(200, json=vectors(len(__import__("json").loads(req.content)["inputs"]))))
    embedder = hf(monkeypatch)
    result = await embedder.embed_documents([f"text {i}" for i in range(70)])
    assert len(result) == 70
    assert route.call_count == 3  # 32 + 32 + 6
    await embedder.close()


@respx.mock
async def test_hf_query_is_one_vector(monkeypatch):
    respx.post(URL).mock(return_value=httpx.Response(200, json=vectors(1)))
    embedder = hf(monkeypatch)
    assert len(await embedder.embed_query("q")) == EMBEDDING_DIMENSION
    await embedder.close()


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": "bad token"}),
        httpx.Response(503, text="loading"),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"error": "x"}),
        httpx.Response(200, json=vectors(2)),  # one text asked, two vectors back
        httpx.Response(200, json=[[0.1, 0.2]]),  # wrong dimension
        httpx.Response(200, json=[[None] * EMBEDDING_DIMENSION]),
        httpx.Response(200, json=[[[0.1] * EMBEDDING_DIMENSION]]),  # token-level output, not a sentence vector
    ],
)
async def test_hf_refuses_anything_unusable_with_a_safe_message(monkeypatch, response):
    respx.post(URL).mock(return_value=response)
    embedder = hf(monkeypatch)
    with pytest.raises(EmbeddingError) as error:
        await embedder.embed_query("secret runbook text")
    assert "secret runbook text" not in str(error.value)
    assert "hf_secret_token_value" not in str(error.value)
    await embedder.close()


@respx.mock
async def test_hf_network_failure_is_an_embedding_error(monkeypatch):
    respx.post(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    embedder = hf(monkeypatch)
    with pytest.raises(EmbeddingError, match="could not be reached"):
        await embedder.embed_query("q")
    await embedder.close()


async def test_settings_refuse_hf_embeddings_without_a_token(monkeypatch):
    with pytest.raises(ValueError, match="HF_TOKEN"):
        Settings(_env_file=None, embedding_provider="huggingface", hf_token=None)


async def test_the_default_embedder_is_the_built_in_one():
    assert isinstance(build_embedder(get_settings()), HashingEmbedder)
