"""OCI credentials and the Generative AI client, resolved on first use.

Building the client at import time takes the whole container down when
credentials are unavailable, and a refused connection to the instance metadata
service is a slow failure — the better part of a minute — with no application
log to explain it. Credentials are a runtime concern, so they are resolved when
a generation first needs them. A missing credential then fails that one request
with a message naming what was tried, instead of crash-looping the container.

The auth order and the failure text follow the SCOUT worker, so an operator who
has debugged one has debugged both.
"""
from __future__ import annotations

import functools
import logging
import os

import oci

from .config import cfg
from .errors import UpstreamError

log = logging.getLogger("harald.oci")


def _config_from_env() -> dict | None:
    """Build an API-key config from environment variables, or return None.

    The names match the OCI CLI's own, so the same variables work here, in the
    CLI and in a pipeline. This path is first because it is the only one that
    does not care where the container runs: no metadata service, no dynamic
    group, no mounted key file.
    """
    key = os.getenv("OCI_CLI_KEY_CONTENT")
    if not key:
        return None

    config = {
        "user": os.getenv("OCI_CLI_USER"),
        "tenancy": os.getenv("OCI_CLI_TENANCY"),
        "fingerprint": os.getenv("OCI_CLI_FINGERPRINT"),
        "region": os.getenv("OCI_CLI_REGION") or cfg.genai_region,
        # Content rather than a path, so the key can come from a vault
        # injection instead of living on the image filesystem.
        "key_content": key.replace("\\n", "\n"),
    }
    missing = [k for k, v in config.items() if not v]
    if missing:
        raise UpstreamError(
            "OCI_CLI_KEY_CONTENT is set but these are not: "
            + ", ".join("OCI_CLI_" + k.upper() for k in missing)
        )
    oci.config.validate_config(config)
    return config


# lru_cache does not cache exceptions, so a credential that becomes available
# later (a policy finally propagating, say) is picked up without a restart.
@functools.lru_cache(maxsize=1)
def _auth():
    """Return (config, signer) from whichever auth method this host offers."""
    attempts: list[str] = []

    try:
        config = _config_from_env()
        if config:
            log.info("OCI auth: API key from environment")
            return config, None
        attempts.append("environment API key (OCI_CLI_KEY_CONTENT unset)")
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"environment API key ({exc})")

    try:
        config = oci.config.from_file()
        oci.config.validate_config(config)
        log.info("OCI auth: config file")
        return config, None
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"config file ({type(exc).__name__})")

    # OKE workload identity. Before instance principals because it fails
    # instantly on an env var, while instance principals spend most of a minute
    # retrying an unreachable metadata endpoint.
    try:
        signer = oci.auth.signers.get_resource_principals_signer()
        log.info("OCI auth: resource principal")
        return {"region": signer.region}, signer
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"resource principal ({type(exc).__name__})")

    try:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        log.info("OCI auth: instance principal")
        return {"region": signer.region}, signer
    except Exception as exc:  # noqa: BLE001
        attempts.append(f"instance principal ({type(exc).__name__})")

    raise UpstreamError(
        "No OCI credentials available.",
        {
            "tried": attempts,
            "hint": "On OCI this is usually a missing dynamic group matching this "
                    "container plus a policy granting it use of generative-ai-family. "
                    "A refused connection to 169.254.169.254 means the metadata "
                    "service is unreachable, so the container is not running "
                    "somewhere instance principals apply.",
        },
    )


def available() -> bool:
    """Whether credentials resolve, for health reporting."""
    try:
        _auth()
        return True
    except Exception:  # noqa: BLE001
        return False


@functools.lru_cache(maxsize=1)
def genai():
    config, signer = _auth()
    return oci.generative_ai_inference.GenerativeAiInferenceClient(
        config=config,
        signer=signer,
        service_endpoint=cfg.genai_endpoint,
        # The SDK's own retry handling would compound the backoff in llm.py.
        retry_strategy=oci.retry.NoneRetryStrategy(),
        timeout=cfg.llm_timeout,
    )


@functools.lru_cache(maxsize=1)
def object_storage():
    """Object Storage in the bucket's own region.

    Deliberately not cfg.genai_region. Generative AI is served from Chicago
    while the tenancy, the database and the bucket are all in Mumbai, and a
    client built for the wrong region reports a bucket that plainly exists as
    not found.
    """
    config, signer = _auth()
    config = dict(config)
    config["region"] = cfg.bucket_region
    return oci.object_storage.ObjectStorageClient(config=config, signer=signer)


@functools.lru_cache(maxsize=1)
def namespace() -> str:
    """The tenancy's Object Storage namespace, which every call needs and which
    is not derivable from any OCID."""
    return object_storage().get_namespace().data


@functools.lru_cache(maxsize=8)
def bucket_name_for(bucket_ocid: str) -> str:
    """Resolve a bucket OCID to the name the API actually takes.

    Object Storage is addressed by namespace and name; there is no lookup by
    OCID. Listing the compartment and matching is the only route, so the answer
    is cached: it cannot change without the bucket being deleted.
    """
    client = object_storage()
    listing = oci.pagination.list_call_get_all_results(
        client.list_buckets, namespace(), cfg.bucket_compartment)
    for bucket in listing.data:
        if bucket.id == bucket_ocid:
            return bucket.name

    found = ", ".join(f"{b.name} ({b.id[-12:]})" for b in listing.data) or "none"
    raise UpstreamError(
        f"No bucket with OCID {bucket_ocid[-16:]} in this compartment.",
        {"region": cfg.bucket_region, "namespace": namespace(),
         "buckets_found": found,
         "compartment_searched": cfg.bucket_compartment,
         "hint": "If the bucket lives in a different compartment from the one "
                 "billing model calls, set HARALD_BUCKET_COMPARTMENT_ID."},
    )
