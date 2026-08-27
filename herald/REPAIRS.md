# HARALD — repair notes

Twelve defects fixed across 12 files, plus 2 new files. `pytest tests/` goes
from 91 passing to 108 passing (17 new regression tests, all written against
the TTUHSC incident described below).

The two problems you could see in the UI turned out to have the same shape:
something written at request time collided with something already stored, and
the code that was supposed to reconcile them either did not exist or asked the
wrong question.

---

## 1. Autofill wrote the wrong data into every proposal

**HIGH — this is the one that would have shipped a wrong proposal.**

`POST /api/rfp/parse` on TTUHSC RFP 739-SL3821039 extracted the solicitation
correctly and then stored something else:

| Field | Parsed from the RFP | Reached the proposal |
|---|---|---|
| industry | higher education, healthcare | **County Government** |
| engagement_type | ai_enablement | **Full implementation** |
| due_date | September 21, 2026 | **(blank)** |
| pain_points | AI adoption and enablement | **[]** |
| win_theme | — | **"Cloud-first cutover."** |

`extracted_json` has two halves, `parsed_fields` and `studio_form`, written by
two different actors. `studio.parse()` wrote only `parsed_fields`; the form was
written by a DOM scrape in the browser. Nothing reconciled them. And every
consumer merges the form *over* the parse — `opportunities.grounding_context()`
and `pricing_matrix._context_from_opp()` both do
`{**parsed_fields, **studio_form}` — so the stale half always won.

The drafting brief that reached the model was therefore, verbatim:

> industry County Government, win theme Cloud-first cutover., engagement Full
> implementation

for a health sciences centre asking about AI adoption. `pain_points` was empty,
so the client's one stated need never reached the model at all. Library
retrieval used the same brief as its probe, which is why an AI-enablement RFP
matched Oracle Cloud **HCM** proposals from Outagamie County at 0.31.

**Fixed** by adding `app/field_mapping.py`, the translation layer that was
missing, and calling it from `studio.parse()`:

- Parse vocabulary → form vocabulary. `"higher education, healthcare"` →
  `Healthcare / Health Sciences`; `ai_enablement` → `AI enablement & consulting`;
  `"September 21, 2026"` → `2026-09-21`. An `input[type=date]` silently
  discards anything that is not ISO, which is exactly how the deadline vanished.
- The solicitation wins for the fields it is authoritative about — the button
  says "Autofill from solicitation" and now does that. Human-authored fields
  (`win_theme`, `project_manager`, `solution_architect`, …) are never
  overwritten.
- Where a saved value contradicts the document, it is **reported** rather than
  silently kept: the response carries a `conflicts` list, and the UI renders it.
  On this bid that surfaces the cutover win theme instead of shipping it.
- Free-text needs that the seven-item checkbox vocabulary cannot express are
  preserved in `pain_points_text` and folded into the drafting brief.
  `"AI adoption and enablement"` matches no checkbox and used to be dropped.
- `applyTagGroup` no longer *unticks* every box when the parse matches none of
  them, which was silently erasing human selections.

**Note on the deployed build.** The running server at `:8000` offers neither
`Healthcare / Health Sciences` (industry) nor `AI enablement & consulting`
(engagement) — both are present in your local `web/index.html`. The container
is older than this source tree. Redeploy before testing, or the mapping will
have no option to land on.

## 2. ORA-00955 on section 07, every proposal

**HIGH — pricing was unusable across the board.**

`ensure_table()` asked two different questions. The check looked for a table
owned by `cfg.app_schema` (`ITERIA_AI`) in `ALL_TABLES`; the `CREATE TABLE` was
unqualified, so it targeted `CURRENT_SCHEMA`. Any drift between those — a table
created by hand as `ADMIN` from `schema/harald_pricing_matrix.sql`, which no
deploy tool runs and the README never mentions — makes the check permanently
false and the create permanently collide. `get_for_opportunity()` calls
`ensure_table()` on every request, so every request issued DDL and every
request raised ORA-00955. Nothing depends on `opp_id`, which is why it was
identical for all nine proposals.

**Fixed** — idempotent by construction rather than by prediction:

- Every object name is schema-qualified, so the create target and the check
  target cannot drift.
- One statement per transaction, so a pre-existing table no longer aborts the
  block before the indexes are attempted.
- Catches `oracledb.DatabaseError` with `code == 955` specifically. The old
  bare `except Exception` would examine ORA-01031 (insufficient privileges) and
  ORA-00942 with a `getattr` that returns `None`, then re-raise them under a
  log line claiming the table already existed.
- Memoised, so DDL stops after the first success instead of running per request.
- Adds the missing `harald_pmat_status_idx` and the two CHECK constraints that
  `harald_pricing_matrix.sql` has and the inline DDL did not.

Nothing is dropped or recreated — this table holds approved, locked pricing.

**Still needs a human.** Run this to find what is actually colliding:

```sql
SELECT owner, object_name, object_type, created FROM all_objects
 WHERE object_name IN ('HARALD_PRICING_MATRIX','HARALD_PMAT_OPP_IDX','HARALD_PMAT_STATUS_IDX');
```

If it reports `OWNER = ADMIN`, the table is in the wrong schema and the fix
above will create a second, empty one in `ITERIA_AI` — you want to move the
data, not just deploy this. Also worth reconciling
`schema/harald_pricing_matrix.sql` into `harald_schema.sql`; it is currently an
orphan that no tool deploys, which is how this diverged in the first place.

## 3. CLOB/BLOB read after the connection went back to the pool

**HIGH — 8 sites, on ordinary request paths.**

`oracledb.defaults.fetch_lobs` is `True`, so a CLOB arrives as a lazy locator
and `db.clob()` calls `.read()` on it — which needs the connection. Eight
places did that read *after* the `with cursor()` block had already returned the
connection to the pool. Under load that is not just a 500: it issues a LOB
round-trip on a socket another request now owns.

Fixed in `documents.get_text`, `answers.get`, `answers.best_match` (on every
generation call), `formats.get` (four JSON CLOBs, on every assemble),
`opportunities.requirement`, `reviews.get`, `freshness._note_body`,
`packages.download`. `documents.get_blob` and `pricing.download` already did it
correctly and were the model.

## 4. Library uploads were classified by filename alone

**MEDIUM-HIGH — silent, and it starves retrieval.**

`documents.store` called `classifier.classify_path()`, which reads only the
name. `classifier.classify()` — which also reads the body, and whose own
docstring says the path version "is not enough and was never enough" — was
dead code, called from nowhere.

Measured on your own seed library: `Brown County Proposal.docx`,
`West Fargo ERP Proposal Final.docx` and `Ozaukee_Technical_Response.pdf` all
return `UNCLASSIFIED`. Only `ITERIA_NARRATIVE` is indexed, so uploading a real
past proposal returned `200 OK` with `chunks: 0` and it was never retrievable
again. Only files literally containing "iteria" in the name ever indexed.

Fixed: extract text first, then classify from the body.

## 5. Everything else

| Fix | File | Why it mattered |
|---|---|---|
| `clone()` inverted cover/TOC | `formats.py` | `get()` returns `'Y'`/`'N'`; `create()` expects booleans, and `'N'` is truthy. Cloning a profile that *forbids* a cover page produced one that requires it — a page-limit compliance failure on submission. |
| Orphaned `asyncio.create_task` | `main.py` | asyncio keeps only a weak reference, so a long generation could be GC'd mid-flight. The row was already `gen_status='generating'` and the coroutine's `except` never ran, leaving bids stuck on "generating" with no route to reset. Now held in a set with a done-callback that logs failures. |
| LibreOffice on the event loop | `packages.py` | `assemble()` is `async def` and called `render()` → `subprocess.run(..., timeout=180)` synchronously. One package assembly froze every other request in the process, health checks included. Now `asyncio.to_thread`. Same for the two upload routes, which ran extraction + local embedding + DB inline. |
| N+1 in `export()` | `questionnaires.py` | One `pool.acquire()` per row for two columns `get()` already had in hand — 600 sequential acquisitions on a 600-row agency workbook, against `pool_max=10`. Also fixes a `TypeError` when the row is missing. |
| Unvalidated enums → 500 | `documents.py` | `deal_status=submitted` hit an Oracle CHECK and surfaced as a 500 with a raw ORA-02290. Now a 400 naming the allowed values. |
| `heading_scheme` unvalidated | `formats.py` | `PATCH {"heading_scheme": []}` returned 200 and then broke *every* later assemble with an `AttributeError` naming neither the profile nor the field. The bad value persisted until someone patched it back. |
| `engagement` shadowing | `pricing_matrix.py` | A local string shadowed the imported `engagement` module inside `save()`. Harmless today, one line away from not being. |

---

## Not fixed — needs your decision

**The `reviewer` gate does not hold.** `main.py identity()` falls back to
`auth.SHARED_WORKSPACE` when the token is missing *or invalid* — the `except`
swallows the failure and downgrades instead of rejecting. `SHARED_WORKSPACE`
has `role = REVIEWER`, so an anonymous caller satisfies `Depends(reviewer_role)`
and can reach `GET /api/audit`, `POST/PATCH /api/formats`, review decisions,
and `PATCH /api/answers/{id}` with `status=approved` — approving an answer is
what makes it auto-answer future questionnaires. The approver gate *is*
enforced, so pricing and final approval are safe.

The docstring says drafting has no login wall on purpose. I did not change this
because tightening it will lock out whatever currently calls those routes
without a token, and that is your call, not mine.

**`app/slots.py` is orphaned** — 17KB imported by nothing. `app/gate.py` is a
CLI, not on a request path. Neither was reviewed in depth.

## Submission packet (materials.zip)

Found by unzipping all four real packets rather than reading the manifest.

**Jefferson County's packet shipped our Outagamie County proposal.** Doc 59 is
a won proposal promoted to the library and attached to the Jefferson bid so the
drafter could retrieve from it. `export_materials_zip` walked every attached
document, so it went into `03_attachments` and would have been submitted to the
agency. Library material is now withheld from the packet
(`_is_internal_doc`, `studio.py`).

**Nashua's packet shipped the same agency workbook twice.** q21 imported
Appendix A with the old detector and caught 45 of 3,041 rows; q41 imported it
again after the fix and caught all of them. Both exported, so the packet held
`5260 (1)_iteria_response.xlsx` and `5260 (1)_iteria_response_2.xlsx` and an
evaluator had even odds of opening the near-empty one. `packet_questionnaires`
now keeps one import per source document, the one that answered it.

**A packet download took 176 seconds.** Every questionnaire in the zip re-ran
an openpyxl load-and-save of the agency's own workbook; one of Nashua's is
9.4 MB and there were four of them. The render is pure, so `questionnaires.export`
now memoises it against a counter fingerprint and `fill` drops the entry.

**The packet did not say what was missing.** It downloads clean whether or not
pricing, signatures, resumes and references are in it. Every packet now opens
with `00_SUBMISSION_CHECKLIST.txt`, which names the open items, flags any
workbook HARALD could not answer, and warns that `05_pricing` is internal.

**Completed forms sat beside the blank ones.** Salem's and Jefferson's filled
Attachment A went into `03_attachments` next to the agency's empty original,
separated only by filename. Documents with `doc_role='form'` now go to
`02_filled_forms/`.

Zip is now named `<client>_submission_packet.zip`.
