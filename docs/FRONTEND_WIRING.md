# WARDEN — Frontend ↔ ORDS wiring

How `web/WARDEN.html` talks to the database. Follows the `scout-hooks` and
`validate-hooks` conventions already in production.

## Module layout

- **Schema mapping:** `/ords/admin/`
- **Module:** `warden-hooks` → base path `/warden-hooks/`
- **Tables:** all live in `iteria_ai` schema, prefixed `warden_*`
- **Auth:** `?api_key=` or `api-key` header matching `iteria_ai.api_configuration`
- **Response shape:** `{"ok":true, ...}` on success; `{"ok":false,"error":"..."}` on failure

## Config block in WARDEN.html

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

`apiKey` is injected at APEX deploy — never committed to source.

## Browser endpoints

| UI action | Method | Endpoint | Body / notes |
|---|---|---|---|
| Unlock tenant | POST | `/tenant/unlock` | `{"passphrase":"<tenant access key>"}` → `{ok, data, prov}` |
| Unlock rule baseline | POST | `/baseline/unlock` | `{"passphrase":"<iteria baseline key>"}` → `{ok, baseline}` |
| Health ping | GET | `/health` | |
| List ledger entries | GET | `/ledger` | Active tenant session |
| Record disposition | POST | `/ledger` | Append-only decision row |
| Supersede / renew ledger | PUT | `/ledger/:id` | Never overwrite; supersede |
| Remediation status | PUT | `/remediation/:ref` | `{status, owner, due}` |
| Rule config toggle | PUT | `/rules/:id/status` | `{status: active\|available}` |

## Worker endpoints (container only)

| Purpose | Method | Endpoint |
|---|---|---|
| Claim next pending job | GET | `/jobs/pending` |
| Mark job done | POST | `/jobs/:id/complete` |
| Mark job failed | POST | `/jobs/:id/fail` body: `{"error":"..."}` |

Job types: `INGEST_CONFLICT`, `INGEST_MODEL`, `DETECT`, `BUILD_PAYLOAD`.

### warden_rules.config_json

Each active rule row carries detection metadata:

```json
{
  "duty_a": "Maintain payroll",
  "duty_b": "Approve payroll",
  "privs_a": ["PRIV_PAY_MAINT"],
  "privs_b": ["PRIV_PAY_APPROVE"],
  "scenario": "What could go wrong if one person holds both.",
  "control": "Dual control on payroll changes"
}
```

Load via `python tools/load_rules.py --tenant-id N rules.json`.

## Unlock response contract

The console expects the same JSON shape the embedded ciphertext previously
decrypted to. Top-level `data` object (`D` in the UI):

| Field | Type | Used by |
|---|---|---|
| `tenant` | `{name, env}` | Header chips, config screen |
| `kpi` | object | Command center metrics, badges |
| `ranking` | `[{id, unit, risk, r:[ruleIds], ...}]` | People by risk |
| `rules` | `[{id, name, a, b, people, roles}]` | Conflict rules |
| `risk` | `{ruleId: [tier, scenario, control]}` | Materiality, modals |
| `detail` | `{personId: [conflict paths]}` | Person modal, violations |
| `report` | object | Assessment report |
| `catalog` | `{roles, privs, roleStats}` | Role & privilege reference |
| `sensitive` | `{roles, stats}` | Sensitive access screen |
| `crossunit` | object | Cross-unit access screen |
| `runs` | `{series, rules, note, narrative?}` | Run comparison |
| `ledger` | array | Disposition ledger seed |
| `exceptions` | array | Per-unit rule exceptions |
| `baseline` | array (optional) | Rule config if bundled with tenant |
| `aggregatePrivs` | string[] | Aggregate privilege badges |

Separate top-level `prov` object (merged into `PROV` in the UI):

```json
{
  "conflict": {"pretty": "...", "source": "..."},
  "model": {"pretty": "...", "source": "...", "scope": "...", "roles": 0, "grants": 0},
  "drift_days": 0
}
```

Provenance values come from the extract metadata stored at ingest time — not
hardcoded in the HTML.

## Ingestion inputs

Two coordinated extracts (see Configuration screen in the UI):

1. **Conflict analysis** — user-level SOD disposition export
2. **Security model** — User & Role Access Audit Report (+ data security grants)

The detection engine joins them. Drift between as-of dates is surfaced on every
joined view. Modules outside the loaded stripe (financials, procurement,
projects) are out of scope until their extracts are loaded.

## Error contract

- 403 → session / api_key problem
- 401 on unlock → wrong passphrase
- 404 → no tenant payload built yet (run ingest + detect first)
- 5xx → generic toast; log full body to console
