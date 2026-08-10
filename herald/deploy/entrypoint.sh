#!/bin/sh
# Bring up the wallet, then start uvicorn.
#
# The image never contains the ADB wallet. Either:
#   1. A volume is mounted at $TNS_ADMIN (default /wallet), or
#   2. HARALD_WALLET_BUCKET + HARALD_WALLET_OBJECT point at a private zip in
#      Object Storage, which this script downloads with instance principals.
#
# Failures here print a sentence a human can act on, then exit. Uvicorn must
# not start without a wallet: the first request would hang for minutes and look
# like a dead container.

set -eu

TNS_ADMIN="${TNS_ADMIN:-/wallet}"
export TNS_ADMIN

have_wallet() {
  [ -f "$TNS_ADMIN/tnsnames.ora" ] && [ -f "$TNS_ADMIN/cwallet.sso" -o -f "$TNS_ADMIN/ewallet.pem" ]
}

if ! have_wallet; then
  if [ -n "${HARALD_WALLET_BUCKET:-}" ] && [ -n "${HARALD_WALLET_OBJECT:-}" ]; then
    echo "wallet: downloading ${HARALD_WALLET_OBJECT} from ${HARALD_WALLET_BUCKET}"
    mkdir -p "$TNS_ADMIN"
    python - <<'PY'
import os, zipfile, io
from app import ociclients

ns = os.environ.get("OCI_OBJECT_NAMESPACE", "bmi3vxyqnzrv")
bucket = os.environ["HARALD_WALLET_BUCKET"]
obj = os.environ["HARALD_WALLET_OBJECT"]
dest = os.environ.get("TNS_ADMIN", "/wallet")

client = ociclients.object_storage()
data = client.get_object(ns, bucket, obj).data.content
with zipfile.ZipFile(io.BytesIO(data)) as zf:
    names = zf.namelist()
    zf.extractall(dest)
print(f"wallet: extracted {len(names)} files to {dest}")
PY
  else
    echo "wallet: nothing at $TNS_ADMIN and HARALD_WALLET_BUCKET is unset."
    echo "        Mount the ADB wallet at /wallet, or set HARALD_WALLET_BUCKET"
    echo "        and HARALD_WALLET_OBJECT to a private zip in Object Storage."
    exit 1
  fi
fi

if ! have_wallet; then
  echo "wallet: still incomplete after download. Need tnsnames.ora and cwallet.sso or ewallet.pem."
  exit 1
fi

echo "wallet: ready at $TNS_ADMIN"
exec uvicorn app.main:app --host 0.0.0.0 --port 8080 \
  --proxy-headers --timeout-keep-alive 65
