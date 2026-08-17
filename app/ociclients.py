"""OCI Object Storage — lazy client (SCOUT pattern)."""

from __future__ import annotations

import functools
import logging
import os

import oci

from .config import cfg

log = logging.getLogger("warden.oci")


@functools.lru_cache(maxsize=1)
def _client():
    if os.environ.get("OCI_CLI_KEY_CONTENT"):
        config = {
            "user": os.environ["OCI_CLI_USER"],
            "tenancy": os.environ["OCI_CLI_TENANCY"],
            "fingerprint": os.environ["OCI_CLI_FINGERPRINT"],
            "region": cfg.oci_region or os.environ.get("OCI_REGION"),
            "key_content": os.environ["OCI_CLI_KEY_CONTENT"].replace("\\n", "\n"),
        }
        return oci.object_storage.ObjectStorageClient(config)
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return oci.object_storage.ObjectStorageClient(config={"region": cfg.oci_region}, signer=signer)


def get_object(bucket: str, object_key: str) -> bytes:
    ns = cfg.bucket_namespace
    if not ns or not bucket:
        raise RuntimeError("OCI_OBJECT_NAMESPACE and WARDEN_BUCKET_NAME required")
    resp = _client().get_object(ns, bucket, object_key)
    return resp.data.content
