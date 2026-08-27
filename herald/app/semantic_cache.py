"""Oracle semantic cache for LLM completions (langchain-oracledb OracleSemanticCache)."""
from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any

from . import embeddings
from .config import cfg
from .db import _pool_or_init, init_pool

log = logging.getLogger("harald.semantic_cache")

_lock = threading.Lock()
_cache: Any = None
_init_failed = False


class HeraldEmbeddings:
    """Duck-typed embedding adapter for OracleSemanticCache (local FastEmbed)."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [list(v) for v in embeddings.embed_passages(texts)]

    def embed_query(self, text: str) -> list[float]:
        return list(embeddings.embed_query(text))


def _llm_string(*, model: str, max_tokens: int, temperature: float, system: str) -> str:
    sys_hash = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
    return f"{model}|tok={max_tokens}|temp={temperature}|sys={sys_hash}"


def _cache_prompt(system: str, user: str) -> str:
    return f"SYSTEM:\n{system}\n\nUSER:\n{user}"


def init() -> Any:
    """Create the semantic cache table/index on first use. Safe to call repeatedly."""
    global _cache, _init_failed
    if not cfg.semantic_cache_enabled:
        return None
    if _init_failed:
        return None
    with _lock:
        if _cache is not None:
            return _cache
        try:
            from langchain_oracledb.cache import OracleSemanticCache

            init_pool()
            _cache = OracleSemanticCache(
                client=_pool_or_init(),
                embedding=HeraldEmbeddings(),
                table_name=cfg.semantic_cache_table,
                score_threshold=cfg.semantic_cache_threshold,
                create_index_if_missing=True,
            )
            log.info(
                "semantic cache ready table=%s threshold=%s",
                cfg.semantic_cache_table,
                cfg.semantic_cache_threshold,
            )
            return _cache
        except Exception as exc:  # noqa: BLE001
            _init_failed = True
            log.warning("semantic cache disabled: %s", exc)
            return None


def lookup(*, system: str, user: str, model: str, max_tokens: int,
           temperature: float) -> str | None:
    cache = init()
    if cache is None:
        return None
    prompt = _cache_prompt(system, user)
    llm_string = _llm_string(
        model=model, max_tokens=max_tokens, temperature=temperature, system=system,
    )
    try:
        generations = cache.lookup(prompt, llm_string)
    except Exception as exc:  # noqa: BLE001
        log.debug("semantic cache lookup failed: %s", exc)
        return None
    if not generations:
        return None
    text = (generations[0].text or "").strip()
    if text:
        log.debug("semantic cache hit model=%s len=%s", model, len(text))
    return text or None


def store(*, system: str, user: str, model: str, max_tokens: int,
          temperature: float, text: str) -> None:
    cache = init()
    if cache is None or not text:
        return
    from langchain_core.outputs import Generation

    prompt = _cache_prompt(system, user)
    llm_string = _llm_string(
        model=model, max_tokens=max_tokens, temperature=temperature, system=system,
    )
    try:
        cache.update(prompt, llm_string, [Generation(text=text)])
    except Exception as exc:  # noqa: BLE001
        log.debug("semantic cache store failed: %s", exc)
