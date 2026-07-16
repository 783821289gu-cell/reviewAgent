from collections import Counter
from dataclasses import dataclass
import hashlib
from math import isfinite, sqrt

from providers.embedding_provider import (
    EmbeddingCallMetadata,
    EmbeddingProvider,
    EmbeddingRequest,
    create_embedding_provider,
    tokenize_text,
)


EmbeddingCache = dict[str, tuple[float, ...]]


@dataclass(frozen=True)
class EmbeddingBatch:
    vectors: list[list[float]]
    mode: str
    model: str
    vector_dimension: int


def embed_texts(
    texts: list[str],
    cache: EmbeddingCache | None = None,
    call_records: list[EmbeddingCallMetadata] | None = None,
    provider: EmbeddingProvider | None = None,
) -> EmbeddingBatch:
    if not texts:
        raise ValueError("at least one embedding text is required")
    if not all(isinstance(text, str) for text in texts):
        raise ValueError("embedding texts must be strings")

    resolved_provider = provider or create_embedding_provider()
    resolved_cache = cache if cache is not None else {}
    keys = [_cache_key(resolved_provider, text) for text in texts]
    missing_by_key: dict[str, str] = {}
    for key, text in zip(keys, texts):
        if key not in resolved_cache:
            missing_by_key[key] = text

    if missing_by_key:
        missing_keys = list(missing_by_key)
        response = resolved_provider.embed(
            EmbeddingRequest(texts=[missing_by_key[key] for key in missing_keys]),
            call_records=call_records,
        )
        if len(response.vectors) != len(missing_keys):
            raise ValueError("Embedding provider returned an unexpected vector count")
        for key, vector in zip(missing_keys, response.vectors):
            resolved_cache[key] = tuple(_validated_vector(vector))

    vectors = [_validated_vector(resolved_cache[key]) for key in keys]
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1:
        raise ValueError("Embedding vectors must have a fixed dimension")
    dimension = dimensions.pop()
    return EmbeddingBatch(
        vectors=vectors,
        mode=resolved_provider.mode,
        model=resolved_provider.model,
        vector_dimension=dimension,
    )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding vector dimensions must match")
    if not left:
        return 0.0
    left_length = sqrt(sum(value * value for value in left))
    right_length = sqrt(sum(value * value for value in right))
    if not left_length or not right_length:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_length * right_length)


def lexical_sparse_vector(text: str) -> dict[str, float]:
    tokens = tokenize_text(text)
    if not tokens:
        return {}
    counts = Counter(tokens)
    length = sqrt(sum(value * value for value in counts.values()))
    if not length:
        return {}
    return {token: value / length for token, value in counts.items()}


def lexical_sparse_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(token, 0.0) for token, value in left.items())


def _cache_key(provider: EmbeddingProvider, text: str) -> str:
    payload = f"{provider.mode}\0{provider.model}\0{text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_vector(vector) -> list[float]:
    if not isinstance(vector, (list, tuple)) or not vector:
        raise ValueError("Embedding vector must be a non-empty sequence")
    parsed = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Embedding vector values must be numeric")
        number = float(value)
        if not isfinite(number):
            raise ValueError("Embedding vector values must be finite")
        parsed.append(number)
    return parsed
