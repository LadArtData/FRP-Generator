# WARDEN — Deploy

Same tenancy pattern as SCOUT.

## 1. Database (ADB Database Actions)

Run `sql/*.sql` in order listed in `README.md`.

Verify:

```sql
SELECT table_name FROM all_tables
WHERE owner = 'ITERIA_AI' AND table_name LIKE 'WARDEN%' ORDER BY 1;
```

## 2. APEX page

Create an APEX page that serves `web/WARDEN.html` as the page source (or
static file upload). Inject `apiKey` into the config block at deploy time.

## 3. Worker container

GitHub Actions builds and pushes on every push to `main` (see
`.github/workflows/build-and-push.yml`). Required repository secrets:

| Secret | Purpose |
|---|---|
| `WALLET_ZIP_B64` | Base64-encoded ADB wallet zip (baked into image) |
| `OCIR_USERNAME` | OCIR user (namespace prefix added if omitted) |
| `OCIR_PASSWORD` | OCIR auth token |

Image: `bom.ocir.io/bmi3vxyqnzrv/warden/warden:latest`

Manual build (optional):

```bash
docker build --platform linux/amd64 -t warden .
docker tag warden bom.ocir.io/<namespace>/warden/warden:latest
docker push bom.ocir.io/<namespace>/warden/warden:latest
```

Create an OCI Container Instance pointing at the image. Environment variables
(baked in or set once):

| Variable | Purpose |
|---|---|
| `ORDS_BASE` | e.g. `https://<adb-id>.adb.<region>.oraclecloudapps.com/ords/admin/warden-hooks` |
| `WARDEN_API_KEY` | Same key as `api_configuration` |
| `OCI_REGION` | Bucket region |
| `WARDEN_BUCKET_NAME` | Raw Fusion export prefix root |

Worker needs read/write on the WARDEN Object Storage bucket and network
reachability to ORDS.

## 4. First tenant

1. Upload Fusion exports to the tenant prefix in Object Storage
2. POST an `INGEST_*` job via ORDS (or scheduler)
3. Worker runs ingest → detect → build payload
4. Set tenant access key via admin tooling (`tools/encrypt_tenant.py` packages the payload)
5. Open the console, enter the access key

No tenant data ships with the repository.

## 5. Health

- ORDS: `GET /warden-hooks/health`
- Worker: `GET :8080/health` (same as SCOUT)
