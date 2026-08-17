"""Passphrase verification and tenant payload encryption at rest.

Matches the algorithm embedded in the original portable Console build so
existing tenant packages remain compatible:
  PBKDF2-HMAC-SHA256, 150 000 iterations, 32-byte key, AES-GCM-256.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ITERATIONS = 150_000


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS)
    return kdf.derive(passphrase.encode("utf-8"))


def verifier_hex(passphrase: str, salt: bytes) -> str:
    """Stored in warden_tenants.key_verifier — proves passphrase without storing it."""
    return hashlib.sha256(_derive_key(passphrase, salt)).hexdigest()


def verify_passphrase(passphrase: str, salt: bytes, expected_hex: str) -> bool:
    if not expected_hex:
        return False
    got = verifier_hex(passphrase, salt)
    return hashlib.compare_digest(got, expected_hex.lower())


def encrypt_document(data: dict[str, Any], passphrase: str) -> dict[str, str]:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive_key(passphrase, salt)
    pt = json.dumps(data, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, pt, None)
    return {
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
        "verifier": verifier_hex(passphrase, salt),
    }


def decrypt_document(blob: dict[str, str], passphrase: str) -> dict[str, Any]:
    salt = base64.b64decode(blob["salt"])
    if not verify_passphrase(passphrase, salt, blob.get("verifier", "")):
        raise ValueError("incorrect passphrase")
    iv = base64.b64decode(blob["iv"])
    ct = base64.b64decode(blob["ct"])
    key = _derive_key(passphrase, salt)
    pt = AESGCM(key).decrypt(iv, ct, None)
    return json.loads(pt.decode("utf-8"))
