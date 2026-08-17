# WARDEN — Architecture

## Components

```
Oracle Fusion exports (Object Storage)
        │
        ▼
Worker container ──► ORDS /warden-hooks/jobs/*
        │                    │
        │ ingest + detect    ▼
        │              iteria_ai.warden_* tables
        │                    │
        ▼                    ▼
Encrypted payload blob   Ledger, rules, run history
        │
        ▼
Browser: WARDEN.html ──► ORDS /warden-hooks/tenant/unlock
```

## Detection engine (deterministic)

Described in the Configuration screen and implemented in `app/detection.py`:

1. Parse conflict analysis → person × rule dispositions
2. Parse security model → role hierarchy, privileges, data-security grants
3. Flatten each person's effective access through the full role tree
4. For each active rule, flag anyone holding both sides with evidence
5. Classify triage buckets (recruiting by-design, AOR, low materiality, review)
6. Build role-level self-conflict analysis independent of holders
7. Compute cross-unit scope from data-role wrappers
8. Serialize to the `D` payload contract in `docs/FRONTEND_WIRING.md`

No GenAI. Set-based, auditable, repeatable.

## Data boundaries

| In git | Not in git |
|---|---|
| UI (`web/WARDEN.html`) | Client Console builds (`WARDEN_Console_*.html`) |
| Schema, ORDS handlers | Encrypted tenant payloads |
| Worker source | Raw Fusion exports |
| Baseline rule *structure* (when built) | iteria baseline encryption key |

## Multi-tenant model

Each organization is a **tenant** row with:

- Object Storage prefix for raw extracts
- Encrypted analysis payload (PBKDF2 + AES-GCM, same algorithm as the Console gate)
- Separate access key per tenant (passphrase never stored — only verifier hash)
- Append-only disposition ledger in `warden_ledger`

Onboarding = load exports → run detection → store payload. The app does not
rebuild per customer.

## Relationship to other iteria products

| Product | Overlap |
|---|---|
| SCOUT | Same ORDS auth, job queue, and container deploy pattern |
| HARALD | Same tenancy and OCIR; different domain (proposals vs access governance) |
| VALIDATE | Same worker poll pattern; different job types |

WARDEN is the interactive console DoLight-style vendors do not provide: the
customer owns the tool, schedules pulls, and edits rules — not just PDF reports.
