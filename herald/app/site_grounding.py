"""External site / web grounding for FRP drafting.

Pulls short, citable snippets from preferred Oracle and Iteria domains (and
optional broader web search) so questionnaire and narrative answers are not
limited to the local library. Disabled cleanly when offline or when search
returns nothing — library grounding still works alone.
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import cfg

log = logging.getLogger("harald.site_grounding")

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_RESULT_HREF_RE = re.compile(
    r'href="(https?://[^"]+)"[^>]*class="[^"]*result__a',
    re.I,
)
_RESULT_HREF_RE_ALT = re.compile(
    r'uddg=([^&"]+)',
    re.I,
)


def _domains() -> list[str]:
    raw = cfg.site_grounding_domains or ""
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def _allowed_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for domain in _domains():
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _clean_text(raw: str, limit: int = 1200) -> str:
    text = unescape(_TAG_RE.sub(" ", raw or ""))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0] + "…"
    return text


def _module_hint(module: str | None) -> str:
    if not module:
        return "Oracle Fusion Cloud ERP"
    titles = {
        "FIN": "Oracle Fusion Cloud Financials",
        "HCM": "Oracle Fusion Cloud HCM",
        "PAYROLL": "Oracle Fusion Cloud Payroll",
        "PROC": "Oracle Fusion Cloud Procurement",
        "BUDGET": "Oracle Fusion Cloud Budget",
        "INV": "Oracle Fusion Cloud Inventory",
        "TECH": "Oracle Fusion Cloud technical security integration",
        "GENERAL": "Oracle Fusion Cloud ERP",
    }
    return titles.get((module or "").upper(), "Oracle Fusion Cloud ERP")


def build_queries(question: str, module: str | None = None) -> list[str]:
    q = _WS_RE.sub(" ", (question or "").strip())
    if len(q) > 220:
        q = q[:220].rsplit(" ", 1)[0]
    product = _module_hint(module)
    queries = [
        f"{product} {q}",
        f"site:docs.oracle.com {product} {q[:140]}",
        f"site:iteria.us Oracle Fusion {q[:120]}",
    ]
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for item in queries:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out[: cfg.site_grounding_max_queries]


async def _serper_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    key = cfg.serper_api_key
    if not key:
        return []
    try:
        res = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query, "num": cfg.site_grounding_max_results},
            timeout=cfg.site_grounding_timeout,
        )
        res.raise_for_status()
        data = res.json()
    except Exception as exc:
        log.warning("serper search failed: %s", exc)
        return []
    hits = []
    for row in (data.get("organic") or [])[: cfg.site_grounding_max_results]:
        url = row.get("link") or ""
        if not _allowed_url(url):
            continue
        hits.append({
            "title": row.get("title") or url,
            "url": url,
            "snippet": row.get("snippet") or "",
            "provider": "serper",
        })
    return hits


async def _tavily_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    key = cfg.tavily_api_key
    if not key:
        return []
    try:
        res = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": key,
                "query": query,
                "include_domains": _domains(),
                "max_results": cfg.site_grounding_max_results,
                "search_depth": "basic",
            },
            timeout=cfg.site_grounding_timeout,
        )
        res.raise_for_status()
        data = res.json()
    except Exception as exc:
        log.warning("tavily search failed: %s", exc)
        return []
    hits = []
    for row in (data.get("results") or [])[: cfg.site_grounding_max_results]:
        url = row.get("url") or ""
        if not _allowed_url(url):
            continue
        hits.append({
            "title": row.get("title") or url,
            "url": url,
            "snippet": row.get("content") or "",
            "provider": "tavily",
        })
    return hits


async def _duckduckgo_search(client: httpx.AsyncClient, query: str) -> list[dict]:
    """Keyless fallback. Best-effort HTML parse; failures are silent."""
    try:
        res = await client.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "HAROLD-FRP-Grounding/1.0"},
            timeout=cfg.site_grounding_timeout,
            follow_redirects=True,
        )
        res.raise_for_status()
        html = res.text
    except Exception as exc:
        log.warning("duckduckgo search failed: %s", exc)
        return []

    urls: list[str] = []
    for match in _RESULT_HREF_RE.finditer(html):
        urls.append(match.group(1))
    if not urls:
        from urllib.parse import unquote
        for match in _RESULT_HREF_RE_ALT.finditer(html):
            urls.append(unquote(match.group(1)))

    hits = []
    for url in urls:
        if not url.startswith("http"):
            continue
        if not _allowed_url(url):
            continue
        if any(h["url"] == url for h in hits):
            continue
        hits.append({
            "title": urlparse(url).path.rsplit("/", 1)[-1] or url,
            "url": url,
            "snippet": "",
            "provider": "duckduckgo",
        })
        if len(hits) >= cfg.site_grounding_max_results:
            break
    return hits


async def _fetch_snippet(client: httpx.AsyncClient, hit: dict) -> dict:
    if hit.get("snippet") and len(hit["snippet"]) > 160:
        hit["snippet"] = _clean_text(hit["snippet"], 900)
        return hit
    try:
        res = await client.get(
            hit["url"],
            headers={"User-Agent": "HAROLD-FRP-Grounding/1.0"},
            timeout=cfg.site_grounding_timeout,
            follow_redirects=True,
        )
        if res.status_code >= 400:
            return hit
        ctype = (res.headers.get("content-type") or "").lower()
        if "html" not in ctype and "text" not in ctype:
            return hit
        hit["snippet"] = _clean_text(res.text, 900)
    except Exception as exc:
        log.debug("fetch failed %s: %s", hit.get("url"), exc)
    return hit


async def search(question: str, module: str | None = None) -> list[dict]:
    """Return grounded source dicts: title, url, snippet, provider, kind=site."""
    if not cfg.site_grounding_enabled:
        return []
    if not (question or "").strip():
        return []

    collected: list[dict] = []
    seen_urls: set[str] = set()

    async with httpx.AsyncClient() as client:
        for query in build_queries(question, module):
            batches: list[list[dict]] = []
            if cfg.serper_api_key:
                batches.append(await _serper_search(client, query))
            if cfg.tavily_api_key:
                batches.append(await _tavily_search(client, query))
            if not batches:
                batches.append(await _duckduckgo_search(client, query))

            for batch in batches:
                for hit in batch:
                    url = hit.get("url") or ""
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    collected.append(hit)
                    if len(collected) >= cfg.site_grounding_max_results:
                        break
                if len(collected) >= cfg.site_grounding_max_results:
                    break
            if len(collected) >= cfg.site_grounding_max_results:
                break

        enriched = []
        for hit in collected[: cfg.site_grounding_max_results]:
            enriched.append(await _fetch_snippet(client, hit))

    sources = []
    for hit in enriched:
        snippet = _clean_text(hit.get("snippet") or "", 900)
        if not snippet:
            continue
        sources.append({
            "kind": "site",
            "title": hit.get("title") or hit.get("url"),
            "url": hit.get("url"),
            "snippet": snippet,
            "provider": hit.get("provider"),
            "score": None,
        })
    log.info(
        "site grounding hits=%s module=%s providers=%s",
        len(sources),
        module,
        sorted({s.get("provider") for s in sources}),
    )
    return sources


def format_context(sources: list[dict]) -> str:
    if not sources:
        return ""
    blocks = []
    for i, src in enumerate(sources, 1):
        blocks.append(
            f"[site source {i}: {src.get('title')} | {src.get('url')}]\n{src.get('snippet')}"
        )
    return (
        "ORACLE / ITERIA / WEB MATERIAL (use for product facts; do not invent beyond this "
        "or the library; mark client-specific unknowns as [NEEDS HUMAN: ...]):\n\n"
        + "\n\n---\n\n".join(blocks)
    )


def configured() -> dict[str, Any]:
    return {
        "enabled": cfg.site_grounding_enabled,
        "domains": _domains(),
        "serper": bool(cfg.serper_api_key),
        "tavily": bool(cfg.tavily_api_key),
        "fallback": "duckduckgo" if not (cfg.serper_api_key or cfg.tavily_api_key) else None,
        "max_results": cfg.site_grounding_max_results,
    }
