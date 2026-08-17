# WARDEN — deploy package

Oracle Fusion Cloud segregation-of-duties and access governance console.

Oracle Database 26ai · APEX · ORDS · OCI worker container.

No seed or mock data in source. Tenant analysis payloads are built offline,
stored encrypted per tenant, and loaded at runtime after unlock.

## What it does

WARDEN ingests Oracle Fusion **User & Role Access Audit Report** exports and
user-level SOD analysis, runs deterministic set-based conflict detection, and
presents findings through a read-only console:

- People by risk, schools/units, role self-conflict analysis
- Sensitive data access (data-security dimension)
- Cross-unit scope attribution
- Run comparison between extracts
- Review queue triage (recruiting by-design, AOR, low materiality)
- Disposition ledger (append-only audit trail)
- Assessment report and CSV exports

The UI spec lives in `web/WARDEN.html`. Regenerate it from a local client
build with:

```powershell
python tools/prepare_github.py
```

That script strips embedded ciphertext and wires ORDS. It does **not** ship
tenant data.

## sql/ — run in this order

Log in as `ADMIN`. Each script starts with
`ALTER SESSION SET CURRENT_SCHEMA = iteria_ai`.

| # | File | What it does |
|---|---|---|
| 1 | `sql/schema.sql` | Core `warden_*` tables |
| 2 | `sql/PKG_warden_core.sql` | Auth, tenant unlock, payload assembly |
| 3 | `sql/PKG_warden_worker.sql` | Ingest and detection job handlers |
| 4 | `sql/ORDS_warden_api.sql` | Browser-facing routes |
| 5 | `sql/ORDS_warden_worker.sql` | Container job routes |

Re-runs are safe: tables use idempotent create patterns; ORDS handlers
overwrite.

API key auth uses the shared `iteria_ai.api_configuration` table (same as
SCOUT, FRP, VALIDATE).

## app/ — the worker container

Polling worker (SCOUT pattern): claims jobs from
`GET /warden-hooks/jobs/pending`, dispatches by `job_type`.

| File | Does |
|---|---|
| `main.py` | Poll loop |
| `ords.py` | ORDS client |
| `ingest.py` | Parse Fusion exports into staging tables |
| `detection.py` | Flatten role hierarchy, evaluate active rules, write payload |

Job types: `INGEST_CONFLICT`, `INGEST_MODEL`, `DETECT`, `BUILD_PAYLOAD`.

## web/

`WARDEN.html` — the console. Config block at top (injected at deploy):

```html
<script id="warden-config" type="application/json">
{
  "ordsBase": "/ords/admin/warden-hooks",
  "apiKey": "<injected at deploy>",
  "env": "PROD",
  "build": "1.0.0"
}
</script>
```

See `docs/FRONTEND_WIRING.md` for the full endpoint map and payload contract.

## First tenant run (pipeline)

After schema + ORDS are deployed and a tenant row exists:

```powershell
# 1. Load active rules (local JSON file — not in git)
python tools/load_rules.py --tenant-id 1 .\local\rules.json

# 2. Upload Fusion exports to Object Storage, then enqueue jobs
python tools/enqueue_pipeline.py --tenant-id 1 `
  --conflict-key tenants/acme/conflict.csv `
  --model-key tenants/acme/model.csv `
  --access-key "<tenant passphrase>" `
  --as-of-conflict 2026-06-29 --as-of-model 2026-07-27

# 3. Worker processes: INGEST_CONFLICT → INGEST_MODEL → DETECT → BUILD_PAYLOAD
# 4. Open console, enter access key
```

Job types and staging tables:

| Job | Writes to |
|-----|-----------|
| `INGEST_CONFLICT` | `warden_staging_conflict` |
| `INGEST_MODEL` | `warden_staging_model` |
| `DETECT` | reads staging + `warden_rules` → interim `warden_tenants.payload_json` |
| `BUILD_PAYLOAD` | encrypts interim payload → `warden_tenants` (blob + verifier) |

## tools/

| File | Does |
|---|---|
| `prepare_github.py` | Strip client blobs from local Console → `web/WARDEN.html` |
| `load_rules.py` | Upsert active rules into `warden_rules` |
| `enqueue_pipeline.py` | POST the four pipeline jobs to ORDS |
| `encrypt_tenant.py` | Offline encrypted package (portable Console format) |

## Deploy

See `docs/DEPLOY.md` for ADB schema, ORDS, OCIR worker, and APEX hosting steps.


```
Fusion exports → Object Storage
       ↓
Worker (ingest → detect → build payload) → ADB warden_* tables
       ↓
ORDS /warden-hooks → WARDEN.html console
```

- **No mock data in git.** Tenant payloads are produced by the detection pipeline and stored per tenant in ADB.
- **Console** is the UI spec (`web/WARDEN.html`), regenerated from local builds via `tools/prepare_github.py`.
- **Detection** is deterministic Python (`app/detection/`) — set-based, auditable, no GenAI.
- **Ledger** is append-only in `warden_ledger` with audit trail.
- **Deploy** follows SCOUT: ORDS on ADB + worker container in OCIR.

See `docs/ARCHITECTURE.md` and `docs/FRONTEND_WIRING.md`.

## Tests

```powershell
python -m pytest tests/ -q
```

Offline logic tests only — no live OCI or ADB required.

## GitHub

Repository: `LadArtData/WARDEN` (private). CI builds the worker image on push to
`main`.

Required GitHub secrets (same as HAROLD/SCOUT):

| Secret | Purpose |
|---|---|
| `WALLET_ZIP_B64` | Base64 ADB wallet zip |
| `OCIR_USERNAME` | OCIR user |
| `OCIR_PASSWORD` | OCIR auth token |

Local client builds (`WARDEN_Console_*.html`, marketing assets) stay on disk
only — excluded by `.gitignore`.
