from collections.abc import Callable
from math import isfinite, sqrt
from pathlib import Path
from threading import Lock
from time import perf_counter

from config import Settings
from providers.embedding_provider import (
    EmbeddingCallMetadata,
    EmbeddingProviderError,
    EmbeddingRequest,
    EmbeddingResponse,
)


BGE_EMBEDDING_DIMENSION = 1024


class BGEEmbeddingProvider:
    mode = "bge_local"

    def __init__(
        self,
        app_settings: Settings,
        *,
        model_factory: Callable | None = None,
    ):
        self.settings = app_settings
        self.model = app_settings.bge_embedding_model
        self.revision = app_settings.bge_embedding_revision
        self._model_factory = model_factory or _load_sentence_transformer
        self._model = None
        self._load_lock = Lock()
        self._inference_lock = Lock()

    def embed(
        self,
        request: EmbeddingRequest,
        call_records: list[EmbeddingCallMetadata] | None = None,
    ) -> EmbeddingResponse:
        start = perf_counter()
        try:
            if not request.texts:
                raise ValueError("BGE embedding requires at least one text")
            model = self._get_model()
            with self._inference_lock:
                raw_vectors = model.encode(
                    request.texts,
                    batch_size=self.settings.bge_embedding_batch_size,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            vectors = [_normalized_vector(vector) for vector in raw_vectors]
            if len(vectors) != len(request.texts):
                raise ValueError(
                    "BGE embedding result count must match input count"
                )
            if any(len(vector) != BGE_EMBEDDING_DIMENSION for vector in vectors):
                raise ValueError(
                    f"BGE embedding dimension must be {BGE_EMBEDDING_DIMENSION}"
                )
            metadata = EmbeddingCallMetadata(
                mode=self.mode,
                model=self.model,
                provider_request_id="",
                input_count=len(request.texts),
                vector_dimension=BGE_EMBEDDING_DIMENSION,
                latency_ms=_elapsed_ms(start),
                error_type="",
                cost_status="local_bge",
            )
            if call_records is not None:
                call_records.append(metadata)
            return EmbeddingResponse(vectors=vectors, metadata=metadata)
        except EmbeddingProviderError:
            raise
        except Exception as exc:
            metadata = EmbeddingCallMetadata(
                mode=self.mode,
                model=self.model,
                provider_request_id="",
                input_count=len(request.texts),
                vector_dimension=None,
                latency_ms=_elapsed_ms(start),
                error_type="local_model_error",
                cost_status="local_bge",
            )
            if call_records is not None:
                call_records.append(metadata)
            raise EmbeddingProviderError(
                "local_model_error",
                f"BGE embedding failed: {exc}",
                False,
            ) from exc

    def cache_metadata(self, input_count: int) -> EmbeddingCallMetadata:
        return EmbeddingCallMetadata(
            mode=self.mode,
            model=self.model,
            provider_request_id="",
            input_count=input_count,
            vector_dimension=BGE_EMBEDDING_DIMENSION,
            latency_ms=0,
            error_type="",
            cost_status="redis_cache_hit",
        )

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                cache_dir = Path(self.settings.bge_cache_dir)
                cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = self._model_factory(
                    self.model,
                    revision=self.revision,
                    cache_folder=str(cache_dir),
                    device=self.settings.bge_device,
                    max_length=self.settings.bge_embedding_max_length,
                )
        return self._model


class BGEReranker:
    def __init__(
        self,
        app_settings: Settings,
        *,
        model_factory: Callable | None = None,
    ):
        self.settings = app_settings
        self.model = app_settings.bge_reranker_model
        self.revision = app_settings.bge_reranker_revision
        self._model_factory = model_factory or _load_cross_encoder
        self._model = None
        self._load_lock = Lock()
        self._inference_lock = Lock()

    def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        model = self._get_model()
        pairs = [(query, document) for document in documents]
        with self._inference_lock:
            raw_scores = model.predict(
                pairs,
                batch_size=self.settings.bge_reranker_batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        scores = [float(value) for value in raw_scores]
        if len(scores) != len(documents) or not all(isfinite(value) for value in scores):
            raise ValueError("BGE reranker returned invalid scores")
        return scores

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is None:
                cache_dir = Path(self.settings.bge_cache_dir)
                cache_dir.mkdir(parents=True, exist_ok=True)
                self._model = self._model_factory(
                    self.model,
                    revision=self.revision,
                    cache_folder=str(cache_dir),
                    device=self.settings.bge_device,
                    max_length=self.settings.bge_reranker_max_length,
                )
        return self._model


def _load_sentence_transformer(
    model_name: str,
    *,
    revision: str,
    cache_folder: str,
    device: str,
    max_length: int,
):
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(
        model_name,
        revision=revision,
        cache_folder=cache_folder,
        device=device,
        trust_remote_code=False,
    )
    model.max_seq_length = max_length
    return model


def _load_cross_encoder(
    model_name: str,
    *,
    revision: str,
    cache_folder: str,
    device: str,
    max_length: int,
):
    from sentence_transformers import CrossEncoder

    return CrossEncoder(
        model_name,
        revision=revision,
        cache_folder=cache_folder,
        device=device,
        trust_remote_code=False,
        max_length=max_length,
    )


def _normalized_vector(raw_vector) -> list[float]:
    values = [float(value) for value in raw_vector]
    if not values or not all(isfinite(value) for value in values):
        raise ValueError("BGE embedding returned an invalid vector")
    length = sqrt(sum(value * value for value in values))
    if not length:
        raise ValueError("BGE embedding returned a zero vector")
    return [value / length for value in values]


def _elapsed_ms(start: float) -> int:
    return max(0, int((perf_counter() - start) * 1000))
