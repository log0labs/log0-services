"""Turn text into vectors for similarity search.

``Embedder`` is the small interface the rest of the service uses; there are two implementations:

* ``HashingEmbedder``: built in, deterministic, no network. Words are hashed into a fixed number of
  buckets (the "hashing trick"), so texts that share words get similar vectors. It cannot match
  synonyms ("timeout" vs "slow"), but it is real, fast, and makes tests and local development
  need nothing external.
* ``HuggingFaceEmbedder``: a sentence-transformers model run by Hugging Face's inference router.
  This is the semantic one. The text of runbooks leaves the service, so it is opt-in.

Every vector has ``EMBEDDING_DIMENSION`` numbers: the database column has that size.
"""

import hashlib
import math
import re
from itertools import pairwise
from typing import Protocol

import httpx

from app.config import Settings

EMBEDDING_DIMENSION = 384  # all-MiniLM-L6-v2's size; also the size of the vector(384) columns
MAX_EMBED_CHARS = 4000  # longer input is cut: embedding models read a limited window anyway
_BATCH = 32

_TOKEN = re.compile(r"[a-z0-9_]+")
_STOP = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "will", "with"]
)


class EmbeddingError(Exception):
    """The embedding service failed or answered something unusable (no text of the input is included)."""


class Embedder(Protocol):
    model_id: str  # stored with every runbook: changing it means re-indexing

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return vector if norm == 0 else [x / norm for x in vector]


class HashingEmbedder:
    model_id = "hash-384-v1"

    def _embed(self, text: str) -> list[float]:
        words = [w for w in _TOKEN.findall(text[:MAX_EMBED_CHARS].lower()) if w not in _STOP]
        vector = [0.0] * EMBEDDING_DIMENSION
        # one-word and two-word features: the pairs let "connection pool" outrank "pool" alone
        features = [(w, 1.0) for w in words] + [(f"{a} {b}", 0.5) for a, b in pairwise(words)]
        for feature, weight in features:
            digest = hashlib.sha1(feature.encode()).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSION
            vector[bucket] += weight if digest[4] % 2 == 0 else -weight  # a sign spreads collisions out
        return _normalize(vector)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class HuggingFaceEmbedder:
    def __init__(self, settings: Settings) -> None:
        assert settings.hf_token is not None  # Settings refuses this configuration without a token
        self.model_id = settings.embedding_model
        base = settings.hf_embedding_base_url.rstrip("/")
        self._url = f"{base}/models/{settings.embedding_model}/pipeline/feature-extraction"
        self._client = httpx.AsyncClient(
            timeout=settings.embedding_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.hf_token.get_secret_value()}"},
        )

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"inputs": [t[:MAX_EMBED_CHARS] for t in texts], "options": {"wait_for_model": True}}
        try:
            response = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"the embedding service could not be reached ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise EmbeddingError(f"the embedding service answered {response.status_code}")
        try:
            vectors = response.json()
            ok = (
                isinstance(vectors, list)
                and len(vectors) == len(texts)
                and all(
                    isinstance(v, list)
                    and len(v) == EMBEDDING_DIMENSION
                    and all(isinstance(x, int | float) and math.isfinite(x) for x in v)
                    for v in vectors
                )
            )
        except ValueError:
            ok = False
        if not ok:
            raise EmbeddingError(
                f"the embedding service did not return {len(texts)} vectors of {EMBEDDING_DIMENSION} numbers"
                " (is EMBEDDING_MODEL a 384-dimension model?)"
            )
        return [_normalize([float(x) for x in v]) for v in vectors]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            vectors.extend(await self._embed_batch(texts[start : start + _BATCH]))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed_batch([text]))[0]

    async def close(self) -> None:
        await self._client.aclose()


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "huggingface":
        return HuggingFaceEmbedder(settings)
    return HashingEmbedder()


def to_pgvector(vector: list[float]) -> str:
    """The text form Postgres reads as a vector: ``[0.1,0.2,...]`` (used with ``CAST(:v AS vector)``)."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"
