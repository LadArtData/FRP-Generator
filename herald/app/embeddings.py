"""Local embeddings. An ONNX model runs inside the container, so no embedding
service, no API key, and no OCI Generative AI dependency. The model is baked
into the image at build time, so the running container makes no network call to
embed."""
from __future__ import annotations

import array
import logging
import threading

from fastembed import TextEmbedding

from .config import cfg
from .errors import HaraldError

log = logging.getLogger("harald.embeddings")

_model: TextEmbedding | None = None
_lock = threading.Lock()


def model() -> TextEmbedding:
    global _model
    with _lock:
        if _model is None:
            log.info("loading embedding model %s", cfg.embed_model)
            _model = TextEmbedding(model_name=cfg.embed_model)
        return _model


def _vector(values) -> array.array:
    vec = array.array("f", (float(v) for v in values))
    if len(vec) != cfg.embed_dim:
        raise HaraldError(
            f"Embedding dimension mismatch: model produced {len(vec)}, "
            f"schema expects {cfg.embed_dim}. Set HARALD_EMBED_DIM and the VECTOR "
            f"columns to the same value."
        )
    return vec


def embed_passages(texts) -> list[array.array]:
    """Document-side embeddings (bge uses an asymmetric passage/query split)."""
    texts = [t for t in texts]
    if not texts:
        return []
    return [_vector(v) for v in model().passage_embed(texts)]


def embed_query(text: str) -> array.array:
    return _vector(next(iter(model().query_embed([text]))))
