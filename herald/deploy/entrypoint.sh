#!/bin/sh
# Start HARALD. The ADB wallet is baked into the image at /wallet — same idea
# as SCOUT baking its ORDS URL. No bucket, no mount, no env vars.
set -eu

TNS_ADMIN="${TNS_ADMIN:-/wallet}"
export TNS_ADMIN

if [ ! -f "$TNS_ADMIN/tnsnames.ora" ]; then
  echo "wallet: missing tnsnames.ora under $TNS_ADMIN — image was built wrong."
  exit 1
fi

echo "wallet: ready at $TNS_ADMIN"
exec uvicorn app.main:app --host 0.0.0.0 --port 8080 \
  --proxy-headers --timeout-keep-alive 65
