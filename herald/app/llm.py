"""OCI Generative AI client. The only model service in HARALD.

Reached through the OCI SDK against the regional inference endpoint, addressing
the model by OCID. Llama models take GenericChatRequest; CohereChatRequest is a
different wire shape and will be rejected by a Llama OCID.

The SDK is synchronous, so every call runs in a worker thread. The alternative,
signing OCI requests by hand over httpx, means reimplementing request signing
and key rotation for no gain.

Production behaviour, unchanged from the previous provider:
  - bounded concurrency so a bulk fill cannot stampede the service
  - retry with exponential backoff and jitter on throttling and 5xx
  - strict JSON extraction for the calls that need structured output

The public surface is complete(), complete_json() and parse_json(). Callers do
not know which provider is behind it, which is what made this swap a one-file
change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re

import oci

from .config import cfg
from .errors import UpstreamError

log = logging.getLogger("harald.llm")

# 429 is throttling; 500/502/503/504 are transient. 400/401/404 are not, and
# retrying them just delays an error the operator needs to see.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_semaphore: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(cfg.llm_concurrency)
    return _semaphore


async def startup() -> None:
    """Resolve credentials at boot so a misconfiguration is visible in the
    startup log rather than in the first user's failed draft. Deliberately does
    not raise: the container still serves the library, the audit trail and every
    read path without a working model."""
    from . import ociclients

    try:
        await asyncio.to_thread(ociclients.genai)
        log.info("OCI GenAI ready model=%s region=%s",
                 cfg.genai_model_name or cfg.draft_model, cfg.genai_region)
    except Exception as exc:  # noqa: BLE001
        log.warning("OCI GenAI unavailable at startup; generation will fail "
                    "until this is resolved: %s", exc)


async def shutdown() -> None:
    """The SDK client holds no event-loop resources; nothing to release."""
    return None


def _chat_sync(system: str, user: str, model: str, max_tokens: int,
               temperature: float) -> str:
    """One blocking SDK round trip. Runs in a worker thread."""
    from . import ociclients

    models = oci.generative_ai_inference.models
    messages = []
    if system:
        messages.append(models.SystemMessage(
            content=[models.TextContent(text=system)]))
    messages.append(models.UserMessage(
        content=[models.TextContent(text=user)]))

    request = models.GenericChatRequest(
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        api_format=models.GenericChatRequest.API_FORMAT_GENERIC,
    )
    details = models.ChatDetails(
        serving_mode=models.OnDemandServingMode(model_id=model),
        chat_request=request,
        compartment_id=cfg.genai_compartment,
    )
    response = ociclients.genai().chat(details)
    choices = response.data.chat_response.choices
    if not choices:
        return ""
    parts = choices[0].message.content or []
    return "".join(getattr(p, "text", "") or "" for p in parts).strip()


async def complete(system: str, user: str, model: str | None = None,
                   max_tokens: int = 1500, temperature: float = 0.7,
                   *, use_cache: bool = True) -> str:
    """One completion. Retries transient failures; raises UpstreamError when the
    service is genuinely unavailable so callers surface a real error rather than
    silently writing an empty draft."""
    model = model or cfg.draft_model
    last_error = "unknown"

    if use_cache and cfg.semantic_cache_enabled:
        from . import semantic_cache
        cached = await asyncio.to_thread(
            semantic_cache.lookup,
            system=system,
            user=user,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if cached:
            return cached

    async with _sem():
        for attempt in range(cfg.llm_max_retries):
            try:
                text = await asyncio.to_thread(
                    _chat_sync, system, user, model, max_tokens, temperature)
            except oci.exceptions.ServiceError as exc:
                if exc.status in _RETRY_STATUS:
                    last_error = f"HTTP {exc.status}: {exc.message}"
                    await _backoff(attempt)
                    continue
                # Non-retryable: retired model, bad id, wrong compartment, policy.
                hint = ""
                if exc.status == 404:
                    hint = (
                        " Model not found — often a retired on-demand model. "
                        "Update GENAI_MODEL_OCID to a current id "
                        "(e.g. meta.llama-3.3-70b-instruct)."
                    )
                detail = (exc.message or "").strip()
                raise UpstreamError(
                    f"OCI Generative AI rejected the request (HTTP {exc.status})"
                    + (f": {detail}" if detail else ".")
                    + hint,
                    {"status": exc.status, "code": exc.code,
                     "message": exc.message, "model": model,
                     "compartment": cfg.genai_compartment},
                ) from exc
            except (oci.exceptions.RequestException,
                    oci.exceptions.ConnectTimeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await _backoff(attempt)
                continue

            if not text:
                last_error = "empty completion"
                await _backoff(attempt)
                continue
            if use_cache and cfg.semantic_cache_enabled:
                from . import semantic_cache
                await asyncio.to_thread(
                    semantic_cache.store,
                    system=system,
                    user=user,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    text=text,
                )
            return text

    raise UpstreamError(
        f"OCI Generative AI unavailable after {cfg.llm_max_retries} attempts.",
        {"last_error": last_error, "model": model},
    )


async def _backoff(attempt: int) -> None:
    delay = 2.0 ** attempt + random.uniform(0, 0.75)   # jitter: desynchronise retries
    log.warning("genai retry attempt=%s sleeping=%.1fs", attempt + 1, delay)
    await asyncio.sleep(min(delay, 45.0))


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json(raw: str, expect: type = dict):
    """Parse a model response that is supposed to be JSON. Strips code fences and
    recovers the outermost object or array when a model adds stray prose."""
    text = _FENCE.sub("", raw).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        opener, closer = ("[", "]") if expect is list else ("{", "}")
        start, end = text.find(opener), text.rfind(closer)
        if start == -1 or end <= start:
            raise ValueError("no JSON found in model response")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, expect):
        raise ValueError(f"expected {expect.__name__}, got {type(value).__name__}")
    return value


async def complete_json(system: str, user: str, expect: type = dict,
                        model: str | None = None, max_tokens: int = 1500,
                        attempts: int = 2):
    """Completion that must return JSON. Re-prompts once on malformed output
    before giving up, which is cheaper and more reliable than a silent fallback."""
    last: Exception | None = None
    for i in range(attempts):
        raw = await complete(system, user, model=model, max_tokens=max_tokens,
                             temperature=0.2 if i else 0.4)
        try:
            return parse_json(raw, expect)
        except ValueError as exc:
            last = exc
            log.warning("malformed JSON from model (attempt %s): %s", i + 1, exc)
            user = (
                user
                + "\n\nYour previous response was not valid JSON. Return ONLY the "
                  "JSON value, with no prose and no code fences."
            )
    raise UpstreamError("OCI Generative AI did not return valid JSON.",
                        {"error": str(last)})
