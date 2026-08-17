#!/usr/bin/env python3
"""Encrypt a detection payload for tenant storage (offline tooling).

Usage:
  python tools/encrypt_tenant.py payload.json access-key -o tenant.enc.json

Output is written locally (gitignored). Load into warden_tenants via admin SQL
or a future deploy tool. Same algorithm as the original Console gate:
PBKDF2-SHA256 (150k iter) + AES-GCM-256.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=150_000)
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_payload(data: dict, passphrase: str) -> dict:
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = derive_key(passphrase, salt)
    pt = json.dumps(data, separators=(",", ":")).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, pt, None)
    return {
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Encrypt WARDEN tenant payload")
    p.add_argument("payload", help="Path to plain JSON (D object + optional prov)")
    p.add_argument("passphrase", help="Tenant access key")
    p.add_argument("-o", "--output", required=True, help="Output path (gitignored)")
    args = p.parse_args(argv)

    raw = json.loads(open(args.payload, encoding="utf-8").read())
    blob = encrypt_payload(raw, args.passphrase)
    out_path = args.output
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(blob, f)
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
