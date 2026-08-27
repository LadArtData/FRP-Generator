"""Semantic cache helpers (no live Oracle required)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.semantic_cache import HeraldEmbeddings, _cache_prompt, _llm_string  # noqa: E402


def test_cache_prompt_combines_system_and_user():
    text = _cache_prompt("sys", "user")
    assert "SYSTEM:" in text
    assert "USER:" in text
    assert "sys" in text
    assert "user" in text


def test_llm_string_changes_when_system_changes():
    base = _llm_string(model="m", max_tokens=100, temperature=0.2, system="a")
    other = _llm_string(model="m", max_tokens=100, temperature=0.2, system="b")
    assert base != other


def test_herald_embeddings_has_required_methods():
    embedder = HeraldEmbeddings()
    assert callable(embedder.embed_documents)
    assert callable(embedder.embed_query)
