# HARALD / FRP Studio

An enterprise bid-production engine for iteria. It drafts government ERP proposals
from iteria's own past submissions, tracks each solicitation to zero compliance
gaps, answers Excel questionnaires, assembles the submission in each agency's
required format, keeps pricing as the approver's artifact alone, and compounds:
every submitted bid returns to the library so the next one starts from it.

OCI Generative AI is the only model service, addressed by model OCID in the
tenancy's own region. Embeddings run locally inside the container, so retrieval
costs nothing per call and needs no network.

--------------------------------------------------------------------------------
What it does
--------------------------------------------------------------------------------

Drafting (the FRP Studio, at `/`)
  The team's Studio, unchanged. It drafts against a bid, parses a solicitation to
  autofill its fields, and answers questions from the library through the
  assistant. It runs on the same bid record the other workspaces use.

Bids & Compliance (`/opportunities`)
  A bid is a complete document set: the solicitation, its amendments, exhibits,
  forms, and questionnaires. Shredding the solicitation builds a requirements
  traceability matrix in the agency's exact wording. Amendments load as
  superseding versions and their new requirements are flagged. Drafting fills the
  matrix from iteria's own material, and the rollup tracks it to zero mandatory
  gaps.

Answer Library (`/answers`)
  Governed, approved, SME-owned standing answers, separate from raw proposal prose.
  Only approved answers auto-answer anything. This is the first source for both
  requirement drafting and questionnaire fill, and the thing freshness keeps current.

Excel questionnaires (inside a bid)
  Import a vendor workbook: HARALD detects the question, response, and comment
  columns and reads the response cell's own dropdown values. Fill answers every row
  from the answer library first and retrieval second, choosing a code from the
  workbook's own allowed values and scoring confidence, with low-confidence rows
  flagged for review. Export writes the answers back into the original workbook with
  its formatting and dropdowns intact.

Packages (`/packages`)
  Assemble the submission in the agency's required page order and heading scheme,
  as a DOCX with a cover, table of contents, and an appended compliance matrix, then
  convert to PDF. Run it through review gates. The approver attaches pricing, gives
  the final sign-off, and marks it submitted, which promotes the narrative back into
  the library.

Admin (`/admin`)
  Per-agency format profiles, the freshness review queue and Oracle release-note
  impact assessment, the audit trail, and a system view.

--------------------------------------------------------------------------------
The anti-AI-tell engine
--------------------------------------------------------------------------------

Evaluation boards reject copy that reads as machine-written, so the generation
prompts enforce a strict ruleset: no buzzword register, broken rule-of-three, hard
sentence-length variation, active voice, concrete specifics, and no em dashes.
Drafting is two passes, a draft and a dedicated humanize pass. Every draft is
grounded in iteria's own past responses, reused for substance and voice but never
copied.

--------------------------------------------------------------------------------
Roles
--------------------------------------------------------------------------------

  contributor   draft, edit, import, fill, assemble
  reviewer      the above, plus approve answers and decide non-final review gates
  approver      Brian. The only role that can upload or lock pricing, decide the
                final gate, approve a package, or mark it submitted.

The database has one shared ADMIN login, so HARALD keeps its own application
identity: a signed, expiring session token issued at sign-in and required on every
state-changing call. Sign-in is pick-a-name; the approver role on that name is
what gates pricing and final approval.

--------------------------------------------------------------------------------
Deploy
--------------------------------------------------------------------------------

1. Schema. In Database Actions, open `schema/harald_schema.sql` and Run Script. It
   builds every table, index, and vector index, and seeds the users and a default
   format profile. It is re-runnable: the reset block at the top drops and rebuilds
   in FK-safe order, so nothing is silently skipped.

2. Wallet. Unzip the Autonomous Database wallet somewhere you will mount into the
   container, for example `./wallet`.

3. Configure. For a Container Instance on this tenancy, values are baked into
   the image (same pattern as SCOUT) — no env-var clipboard. For local runs,
   copy `.env.example` to `.env` and fill ORACLE_PASSWORD, ORACLE_DSN, and
   HARALD_SESSION_SECRET. GenAI region/model OCIDs are already filled in.

4. Build.
       docker build -t harald .
   The build bakes in the embedding model and LibreOffice, so the first build is
   the slow one.

5. Run.
       docker run -d --name harald -p 8080:8080 \
         --env-file .env \
         -v "$PWD/wallet:/app/wallet:ro" \
         harald

6. Verify.
       curl -s localhost:8080/api/health | python -m json.tool
   Expect ok=true, database=up, and the model and embedding fields. If database is
   down, the message says what failed.

7. Sign in and seed the library. Open http://localhost:8080/opportunities, pick
   your name, then upload iteria's past proposals. The `library_seed/` folder
   holds ten real iteria narratives extracted from the SharePoint corpus. Upload
   them through the Studio rail or the library. Uploads are classified
   automatically; only iteria narrative is chunked and indexed.

--------------------------------------------------------------------------------
Layout
--------------------------------------------------------------------------------

  schema/harald_schema.sql   the single canonical schema
  app/                       the engine and API (24 modules)
    config, errors, db, audit, auth            infrastructure
    llm, ociclients, embeddings, prompts       OCI GenAI client, auth, local embeddings
    classifier, chunking, retrieval            corpus gate, chunking, vector search
    documents, answers, generation             storage, answer library, grounded drafting
    opportunities, questionnaires              the bid, Excel round-trip
    formats, packages, pricing, reviews        format profiles, assembly, pricing, gates
    freshness, studio, main                    release impact, Studio adapter, API
  web/                       the Studio and the four workspaces on a shared client
  tests/test_logic.py        offline logic tests (classifier, chunking, JSON,
                             questionnaire detection, Excel round-trip, auth, formats)
  library_seed/              ten real iteria narratives to seed the library
  Dockerfile, requirements.txt, .env.example

--------------------------------------------------------------------------------
Tested here, and what is left to first deploy
--------------------------------------------------------------------------------

Verified offline: the whole app compiles, there are no import cycles, every
workspace's JavaScript parses, the Studio-to-bridge contract is complete (all 13
functions the Studio calls are implemented), and the logic suite passes 37 tests
including the Excel column detection and round-trip and the corpus classifier. The
classifier was validated against the real 202-file corpus.

Not reachable from here, so verified on first deploy against your ADB: the schema
run, the pooled database connections, live OCI GenAI calls, and PDF conversion. Hit
`/api/health` first; it reports each of these.

Run the tests any time with:
    python -m pytest tests/test_logic.py -q
