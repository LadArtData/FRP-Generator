-- ============================================================================
-- HARALD  |  Canonical schema  |  Oracle Autonomous Database 23ai
-- Run once as ADMIN in Database Actions using RUN SCRIPT.
--
-- One bid entity (harald_opportunities). One document table. One requirements
-- traceability matrix. Vector dimension is 768 to match the container's local
-- embedding model (BAAI/bge-base-en-v1.5). No OCI GenAI is used anywhere.
--
-- Re-runnable: the reset block below drops and rebuilds. Nothing is silently
-- skipped, so a partial or conflicting prior state cannot survive.
-- ============================================================================
SET SERVEROUTPUT ON
SET DEFINE OFF

-- The only ADB login is ADMIN, but the app connects with CURRENT_SCHEMA set to
-- ITERIA_AI (app/db.py). Without this the tables below would be created in
-- ADMIN and the app would resolve to an empty ITERIA_AI.
ALTER SESSION SET CURRENT_SCHEMA = iteria_ai;

-- ---------------------------------------------------------------------------
-- RESET (drops in FK-safe order). Comment out to preserve data on re-run.
-- ---------------------------------------------------------------------------
DECLARE
  TYPE tlist IS TABLE OF VARCHAR2(50);
  l_tabs tlist := tlist(
    'HARALD_AUDIT','HARALD_REVIEWS','HARALD_PRICING','HARALD_PACKAGE_SECTIONS',
    'HARALD_PACKAGES','HARALD_QUESTIONNAIRE_ITEMS','HARALD_QUESTIONNAIRES',
    'HARALD_DRAFTS','HARALD_REQUIREMENTS','HARALD_CHUNKS','HARALD_RELEASE_NOTES',
    'HARALD_ANSWERS','HARALD_DOCUMENTS','HARALD_OPPORTUNITIES',
    'HARALD_FORMAT_PROFILES','HARALD_USERS');
BEGIN
  FOR i IN 1 .. l_tabs.COUNT LOOP
    BEGIN
      EXECUTE IMMEDIATE 'DROP TABLE '||l_tabs(i)||' CASCADE CONSTRAINTS PURGE';
      DBMS_OUTPUT.PUT_LINE('dropped '||l_tabs(i));
    EXCEPTION WHEN OTHERS THEN
      IF SQLCODE != -942 THEN RAISE; END IF;   -- -942 = table does not exist
    END;
  END LOOP;
END;
/

-- ---------------------------------------------------------------------------
-- Identity and roles. The database has one shared ADMIN login, so HARALD keeps
-- its own application identity. role='approver' is Brian: pricing owner and the
-- only role that can approve or submit a package.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_users (
  user_id      NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  username     VARCHAR2(80) NOT NULL UNIQUE,
  display_name VARCHAR2(160),
  role         VARCHAR2(20) DEFAULT 'contributor' NOT NULL,
  active       CHAR(1) DEFAULT 'Y' NOT NULL,
  created_at   TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_users_role_ck   CHECK (role IN ('contributor','reviewer','approver')),
  CONSTRAINT harald_users_active_ck CHECK (active IN ('Y','N'))
);

-- ---------------------------------------------------------------------------
-- Per-agency submission format. Drives package assembly and export.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_format_profiles (
  profile_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name           VARCHAR2(200) NOT NULL,
  agency         VARCHAR2(200),
  page_order     CLOB,                       -- JSON: ordered section definitions
  heading_scheme CLOB,                       -- JSON: numbering, case, style
  page_limits    CLOB,                       -- JSON: {"section": max_pages}
  required_forms CLOB,                       -- JSON: array of form names
  font_name      VARCHAR2(80)  DEFAULT 'Calibri',
  font_size      NUMBER        DEFAULT 11,
  margin_inches  NUMBER        DEFAULT 1,
  cover_required CHAR(1)       DEFAULT 'Y',
  toc_required   CHAR(1)       DEFAULT 'Y',
  notes          VARCHAR2(2000),
  created_at     TIMESTAMP DEFAULT SYSTIMESTAMP,
  updated_at     TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_fmt_json_ck CHECK (page_order IS JSON)
);

-- ---------------------------------------------------------------------------
-- The bid. Single entity: the Studio drafts against it, Bids & Compliance
-- tracks it, packages are assembled from it.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_opportunities (
  opp_id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  client_name       VARCHAR2(200),
  agency            VARCHAR2(200),
  solicitation_no   VARCHAR2(100),
  title             VARCHAR2(400),
  due_date          VARCHAR2(50),
  status            VARCHAR2(20) DEFAULT 'evaluating' NOT NULL,
  bid_decision      VARCHAR2(20),
  portal_url        VARCHAR2(600),
  format_profile_id NUMBER REFERENCES harald_format_profiles(profile_id),
  rfp_doc_id        NUMBER,                  -- primary solicitation document
  draft_text        CLOB,                    -- Studio working narrative
  extracted_json    CLOB,                    -- parsed RFP fields + Studio form state
  gen_status        VARCHAR2(20) DEFAULT 'idle' NOT NULL,
  gen_error         VARCHAR2(2000),
  created_by        VARCHAR2(80),
  created_at        TIMESTAMP DEFAULT SYSTIMESTAMP,
  updated_at        TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_opp_status_ck CHECK (status IN
    ('evaluating','bidding','submitted','won','lost','no_bid')),
  CONSTRAINT harald_opp_gen_ck CHECK (gen_status IN ('idle','generating','error')),
  CONSTRAINT harald_opp_ejson_ck CHECK (extracted_json IS JSON)
);

-- ---------------------------------------------------------------------------
-- Every document. opp_id NULL means a pure library source. A bid's own finished
-- response is promoted into the library by setting doc_class ITERIA_NARRATIVE
-- and chunking it, which is how the system compounds.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_documents (
  doc_id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opp_id          NUMBER REFERENCES harald_opportunities(opp_id),
  filename        VARCHAR2(400) NOT NULL,
  doc_class       VARCHAR2(30) DEFAULT 'UNCLASSIFIED' NOT NULL,
  doc_role        VARCHAR2(30) DEFAULT 'reference'    NOT NULL,
  client_name     VARCHAR2(200),
  state           VARCHAR2(50),
  outcome         VARCHAR2(20) DEFAULT 'in_progress',
  version         NUMBER DEFAULT 1 NOT NULL,
  effective_date  VARCHAR2(50),
  supersedes_id   NUMBER,
  file_blob       BLOB,
  size_bytes      NUMBER,
  sha256          VARCHAR2(64),
  doc_text        CLOB,
  -- Whether this document's factual claims may be reused, as distinct from its
  -- voice. Retrieval returns UNVERIFIED material so the model can match how
  -- iteria writes; generation is told to take no dates, counts or references
  -- from it. Without this the anti-fabrication rule sits downstream of the
  -- fabrication and cannot see it.
  trust_level     VARCHAR2(20) DEFAULT 'VERIFIED' NOT NULL,
  style_anchor    CHAR(1)      DEFAULT 'N' NOT NULL,
  promoted_to_lib CHAR(1) DEFAULT 'N' NOT NULL,
  uploaded_by     VARCHAR2(80),
  uploaded_at     TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_doc_class_ck CHECK (doc_class IN
    ('ITERIA_NARRATIVE','CLIENT_RFP','COMPETITOR','PRICING','ADMIN','DEMO',
     'EXCLUDE','RELEASE_NOTE','UNCLASSIFIED')),
  CONSTRAINT harald_doc_role_ck CHECK (doc_role IN
    ('rfp','addendum','exhibit','form','cost_workbook','questionnaire',
     'attachment','reference','iteria_response')),
  CONSTRAINT harald_doc_outcome_ck CHECK (outcome IN
    ('won','lost','in_progress','test','no_bid')),
  CONSTRAINT harald_doc_promo_ck CHECK (promoted_to_lib IN ('Y','N')),
  CONSTRAINT harald_doc_style_anchor_ck CHECK (style_anchor IN ('Y','N'))
) LOB (file_blob) STORE AS SECUREFILE (COMPRESS MEDIUM);

ALTER TABLE harald_documents ADD CONSTRAINT harald_doc_supersedes_fk
  FOREIGN KEY (supersedes_id) REFERENCES harald_documents(doc_id);

ALTER TABLE harald_opportunities ADD CONSTRAINT harald_opp_rfpdoc_fk
  FOREIGN KEY (rfp_doc_id) REFERENCES harald_documents(doc_id);

-- ---------------------------------------------------------------------------
-- Retrieval corpus. Only ITERIA_NARRATIVE documents are chunked here.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_chunks (
  chunk_id    NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  doc_id      NUMBER NOT NULL REFERENCES harald_documents(doc_id) ON DELETE CASCADE,
  module_tag  VARCHAR2(40)  DEFAULT 'GENERAL' NOT NULL,
  section_tag VARCHAR2(100) DEFAULT 'general' NOT NULL,
  -- How the tag was arrived at. Retrieval ranks measured tags above inherited
  -- ones, so this changes results; it is not documentation.
  tag_source  VARCHAR2(20)  DEFAULT 'body'    NOT NULL,
  chunk_index NUMBER,
  chunk_text  CLOB,
  token_count NUMBER,
  embedding   VECTOR(768, FLOAT32),
  created_at  TIMESTAMP DEFAULT SYSTIMESTAMP
);

COMMENT ON COLUMN harald_chunks.tag_source IS
 'body = the chunk text itself scored above threshold for this tag. smoothed = the chunk sits between two body-scored anchors that agree, so it inherited the surrounding section. manual = a human overrode it.';
COMMENT ON COLUMN harald_chunks.token_count IS
 'Approximate: characters divided by four. Not a tokenizer count.';

-- ---------------------------------------------------------------------------
-- Requirements traceability matrix. req_text holds the agency's exact wording.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_requirements (
  req_id         NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opp_id         NUMBER NOT NULL REFERENCES harald_opportunities(opp_id) ON DELETE CASCADE,
  source_doc_id  NUMBER REFERENCES harald_documents(doc_id),
  rfp_ref        VARCHAR2(120),
  req_text       CLOB NOT NULL,
  module_tag     VARCHAR2(40) DEFAULT 'GENERAL' NOT NULL,
  mandatory      CHAR(1)      DEFAULT 'N' NOT NULL,
  response_type  VARCHAR2(20) DEFAULT 'narrative' NOT NULL,
  section_ref    VARCHAR2(200),
  owner          VARCHAR2(100),
  status         VARCHAR2(20) DEFAULT 'not_started' NOT NULL,
  from_amendment CHAR(1)      DEFAULT 'N' NOT NULL,
  notes          VARCHAR2(2000),
  sort_order     NUMBER DEFAULT 0,
  created_at     TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_req_status_ck CHECK (status IN
    ('not_started','in_progress','drafted','reviewed','complete','gap')),
  CONSTRAINT harald_req_rtype_ck CHECK (response_type IN
    ('narrative','questionnaire','form','pricing')),
  CONSTRAINT harald_req_mand_ck CHECK (mandatory IN ('Y','N')),
  CONSTRAINT harald_req_amend_ck CHECK (from_amendment IN ('Y','N'))
);

CREATE TABLE harald_drafts (
  draft_id     NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  req_id       NUMBER NOT NULL UNIQUE REFERENCES harald_requirements(req_id) ON DELETE CASCADE,
  draft_text   CLOB,
  final_text   CLOB,
  sources_json CLOB,
  -- Output of the deterministic post-generation check: placeholders kept,
  -- banned words, em dashes, and numbers introduced that were not in the
  -- source excerpts. The humanize prompt asks for all four; this records
  -- whether they were actually delivered, because a prompt is a request.
  check_report CLOB,
  updated_at   TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_draft_json_ck CHECK (sources_json IS JSON),
  CONSTRAINT harald_draft_check_json_ck CHECK (check_report IS JSON)
);

-- ---------------------------------------------------------------------------
-- Governed answer library. Approved answers are the first source for both
-- requirement drafting and questionnaire fill.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_answers (
  ans_id             NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  question_canonical VARCHAR2(1000) NOT NULL,
  answer_text        CLOB NOT NULL,
  module_tag         VARCHAR2(40) DEFAULT 'GENERAL' NOT NULL,
  tags               VARCHAR2(400),
  owner_sme          VARCHAR2(100),
  status             VARCHAR2(20) DEFAULT 'draft' NOT NULL,
  effective_date     DATE,
  review_due         DATE,
  source_refs        VARCHAR2(1000),
  times_used         NUMBER DEFAULT 0 NOT NULL,
  last_used_at       TIMESTAMP,
  embedding          VECTOR(768, FLOAT32),
  created_at         TIMESTAMP DEFAULT SYSTIMESTAMP,
  updated_at         TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_ans_status_ck CHECK (status IN ('draft','approved','deprecated'))
);

-- ---------------------------------------------------------------------------
-- Excel questionnaires.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_questionnaires (
  q_id          NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opp_id        NUMBER NOT NULL REFERENCES harald_opportunities(opp_id) ON DELETE CASCADE,
  source_doc_id NUMBER NOT NULL REFERENCES harald_documents(doc_id),
  filename      VARCHAR2(400),
  sheet_map     CLOB,
  status        VARCHAR2(20) DEFAULT 'imported' NOT NULL,
  item_count    NUMBER DEFAULT 0 NOT NULL,
  fill_error    VARCHAR2(2000),
  created_at    TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_q_status_ck CHECK (status IN
    ('imported','filling','filled','exported','error')),
  CONSTRAINT harald_q_json_ck CHECK (sheet_map IS JSON)
);

CREATE TABLE harald_questionnaire_items (
  qi_id            NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  q_id             NUMBER NOT NULL REFERENCES harald_questionnaires(q_id) ON DELETE CASCADE,
  opp_id           NUMBER NOT NULL,
  sheet_name       VARCHAR2(200) NOT NULL,
  row_index        NUMBER NOT NULL,
  question_col     VARCHAR2(10),
  response_col     VARCHAR2(10),
  comment_col      VARCHAR2(10),
  question_text    CLOB NOT NULL,
  allowed_codes    CLOB,
  response_code    VARCHAR2(200),
  response_text    CLOB,
  confidence       NUMBER,
  source_answer_id NUMBER REFERENCES harald_answers(ans_id),
  owner            VARCHAR2(100),
  status           VARCHAR2(20) DEFAULT 'todo' NOT NULL,
  sort_order       NUMBER DEFAULT 0,
  CONSTRAINT harald_qi_status_ck CHECK (status IN
    ('todo','drafted','needs_review','approved')),
  CONSTRAINT harald_qi_codes_ck CHECK (allowed_codes IS JSON)
);

-- ---------------------------------------------------------------------------
-- Submission packages. Versioned, downloadable, recallable.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_packages (
  package_id        NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opp_id            NUMBER NOT NULL REFERENCES harald_opportunities(opp_id) ON DELETE CASCADE,
  version           NUMBER DEFAULT 1 NOT NULL,
  status            VARCHAR2(20) DEFAULT 'draft' NOT NULL,
  format_profile_id NUMBER REFERENCES harald_format_profiles(profile_id),
  docx_blob         BLOB,
  pdf_blob          BLOB,
  filename          VARCHAR2(400),
  compliance_json   CLOB,       -- snapshot of the RTM at assembly time
  pricing_id        NUMBER,
  approved_by       VARCHAR2(80),
  approved_at       TIMESTAMP,
  submitted_at      TIMESTAMP,
  created_by        VARCHAR2(80),
  created_at        TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_pkg_status_ck CHECK (status IN
    ('draft','in_review','approved','submitted')),
  CONSTRAINT harald_pkg_json_ck CHECK (compliance_json IS JSON)
) LOB (docx_blob) STORE AS SECUREFILE (COMPRESS MEDIUM)
  LOB (pdf_blob)  STORE AS SECUREFILE (COMPRESS MEDIUM);

CREATE TABLE harald_package_sections (
  section_id  NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  package_id  NUMBER NOT NULL REFERENCES harald_packages(package_id) ON DELETE CASCADE,
  title       VARCHAR2(300) NOT NULL,
  sort_order  NUMBER DEFAULT 0 NOT NULL,
  body        CLOB,
  module_tag  VARCHAR2(40),
  source      VARCHAR2(20) DEFAULT 'generated' NOT NULL,
  req_ids     CLOB,
  page_break  CHAR(1) DEFAULT 'Y' NOT NULL,
  CONSTRAINT harald_sec_source_ck CHECK (source IN
    ('generated','answer_lib','manual','pricing','form')),
  CONSTRAINT harald_sec_json_ck CHECK (req_ids IS JSON)
);

-- ---------------------------------------------------------------------------
-- Pricing. Brian owns this. HARALD never generates it. Enforced in the API by
-- the approver role, and here by the locked flag and owner.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_pricing (
  price_id    NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opp_id      NUMBER NOT NULL REFERENCES harald_opportunities(opp_id) ON DELETE CASCADE,
  version     NUMBER DEFAULT 1 NOT NULL,
  filename    VARCHAR2(400),
  file_blob   BLOB,
  size_bytes  NUMBER,
  status      VARCHAR2(20) DEFAULT 'draft' NOT NULL,
  owner       VARCHAR2(80) NOT NULL,
  locked      CHAR(1) DEFAULT 'N' NOT NULL,
  notes       VARCHAR2(2000),
  updated_at  TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_price_status_ck CHECK (status IN ('draft','final','approved')),
  CONSTRAINT harald_price_locked_ck CHECK (locked IN ('Y','N'))
) LOB (file_blob) STORE AS SECUREFILE (COMPRESS MEDIUM);

ALTER TABLE harald_packages ADD CONSTRAINT harald_pkg_pricing_fk
  FOREIGN KEY (pricing_id) REFERENCES harald_pricing(price_id);

-- ---------------------------------------------------------------------------
-- Review gates. The 'final' gate can only be decided by the approver (Brian).
-- ---------------------------------------------------------------------------
CREATE TABLE harald_reviews (
  review_id  NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  package_id NUMBER NOT NULL REFERENCES harald_packages(package_id) ON DELETE CASCADE,
  gate       VARCHAR2(20) NOT NULL,
  reviewer   VARCHAR2(80),
  status     VARCHAR2(20) DEFAULT 'pending' NOT NULL,
  comments   CLOB,
  created_at TIMESTAMP DEFAULT SYSTIMESTAMP,
  decided_at TIMESTAMP,
  CONSTRAINT harald_rev_gate_ck CHECK (gate IN ('internal','pink','red','final')),
  CONSTRAINT harald_rev_status_ck CHECK (status IN
    ('pending','passed','changes_requested'))
);

-- ---------------------------------------------------------------------------
-- Freshness: ingested Oracle release notes drive answer-library review flags.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_release_notes (
  note_id         NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source          VARCHAR2(200),
  title           VARCHAR2(400),
  release_version VARCHAR2(60),
  published_date  DATE,
  body            CLOB,
  embedding       VECTOR(768, FLOAT32),
  ingested_at     TIMESTAMP DEFAULT SYSTIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Audit trail. Every state change.
-- ---------------------------------------------------------------------------
CREATE TABLE harald_audit (
  event_id    NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor       VARCHAR2(80),
  action      VARCHAR2(80) NOT NULL,
  entity_type VARCHAR2(40),
  entity_id   NUMBER,
  detail      VARCHAR2(4000),
  at          TIMESTAMP DEFAULT SYSTIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Tag vocabularies
-- ---------------------------------------------------------------------------
-- BEGIN GENERATED VOCABULARY (tools/gen_vocabulary_sql.py) --
-- Generated from app/vocabulary.py. Do not hand-edit: run the generator.
-- These constraints are the reason a tag cannot silently drift out of
-- range and return an empty library with no error.

ALTER TABLE harald_chunks ADD CONSTRAINT harald_chunks_section_ck
  CHECK (section_tag IN (
    'transmittal', 'exec_summary', 'qualifications', 'solution',
    'methodology', 'project_mgmt', 'staffing', 'references', 'technical',
    'support', 'cost', 'contract', 'compliance', 'risk', 'general'
  ));

ALTER TABLE harald_chunks ADD CONSTRAINT harald_chunks_module_ck
  CHECK (module_tag IN (
    'HCM', 'PAYROLL', 'FIN', 'BUDGET', 'PROC', 'INV', 'TECH', 'CROSS',
    'GENERAL'
  ));

ALTER TABLE harald_chunks ADD CONSTRAINT harald_chunks_tagsrc_ck
  CHECK (tag_source IN (
    'body', 'smoothed', 'manual'
  ));

ALTER TABLE harald_documents ADD CONSTRAINT harald_doc_trust_ck
  CHECK (trust_level IN (
    'VERIFIED', 'UNVERIFIED'
  ));
-- END GENERATED VOCABULARY --

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX harald_doc_opp_idx     ON harald_documents(opp_id, doc_role);
CREATE INDEX harald_doc_class_idx   ON harald_documents(doc_class, outcome);
CREATE INDEX harald_chunk_doc_idx   ON harald_chunks(doc_id);
CREATE INDEX harald_chunk_mod_idx   ON harald_chunks(module_tag, section_tag);
CREATE INDEX harald_req_opp_idx     ON harald_requirements(opp_id, status);
CREATE INDEX harald_req_mod_idx     ON harald_requirements(opp_id, module_tag);
CREATE INDEX harald_ans_status_idx  ON harald_answers(status, module_tag);
CREATE INDEX harald_ans_review_idx  ON harald_answers(review_due);
CREATE INDEX harald_qi_q_idx        ON harald_questionnaire_items(q_id, status);
CREATE INDEX harald_pkg_opp_idx     ON harald_packages(opp_id, version);
CREATE INDEX harald_sec_pkg_idx     ON harald_package_sections(package_id, sort_order);
CREATE INDEX harald_price_opp_idx   ON harald_pricing(opp_id, version);
CREATE INDEX harald_rev_pkg_idx     ON harald_reviews(package_id, gate);
CREATE INDEX harald_audit_ent_idx   ON harald_audit(entity_type, entity_id);

CREATE VECTOR INDEX harald_chunk_vidx ON harald_chunks(embedding)
  ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95;

CREATE VECTOR INDEX harald_ans_vidx ON harald_answers(embedding)
  ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE WITH TARGET ACCURACY 95;

-- ---------------------------------------------------------------------------
-- Retention
--
-- The API exposes no delete route, so nothing the application does can destroy
-- a proposal. A person holding the ADMIN login is the exposure: the foreign
-- keys cascade, so deleting one opportunity row in Database Actions takes every
-- package under it, approved and submitted alike, without a warning.
--
-- Cascading deletes fire row triggers on the child table, so raising here stops
-- the whole statement, not just the child row.
--
-- These are deliberately not absolute. Work that was never approved is still
-- disposable, and a genuine removal stays possible in two deliberate steps: set
-- the row out of its protected state, then delete it. What is prevented is
-- doing it by accident, in one statement, against a table you were not looking
-- at.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TRIGGER harald_pkg_retain_trg
  BEFORE DELETE ON harald_packages
  FOR EACH ROW
  WHEN (OLD.status IN ('approved', 'submitted'))
BEGIN
  RAISE_APPLICATION_ERROR(-20101,
    'Package ' || :OLD.package_id || ' (version ' || :OLD.version || ', ' ||
    :OLD.status || ') is a proposal that was approved or sent to a client, and '
    || 'is retained. This fires on a cascade too, so an opportunity carrying '
    || 'one cannot be deleted either. To remove it deliberately: UPDATE '
    || 'harald_packages SET status = ''draft'' WHERE package_id = ' ||
    :OLD.package_id || '; then delete.');
END;
/

CREATE OR REPLACE TRIGGER harald_doc_retain_trg
  BEFORE DELETE ON harald_documents
  FOR EACH ROW
  WHEN (OLD.promoted_to_lib = 'Y')
BEGIN
  RAISE_APPLICATION_ERROR(-20102,
    'Document ' || :OLD.doc_id || ' (' || :OLD.filename || ') is in the answer '
    || 'library and is what the model reads to write in iteria''s voice. '
    || 'Deleting it removes that source permanently. To remove it deliberately: '
    || 'UPDATE harald_documents SET promoted_to_lib = ''N'' WHERE doc_id = ' ||
    :OLD.doc_id || '; then delete.');
END;
/

-- ---------------------------------------------------------------------------
-- Seed: users. Brian is the approver, the pricing owner and the final gate.
-- ---------------------------------------------------------------------------
INSERT INTO harald_users (username, display_name, role) VALUES ('brian',    'Brian Schell',      'approver');
INSERT INTO harald_users (username, display_name, role) VALUES ('amanda',   'Amanda Desilets',   'reviewer');
INSERT INTO harald_users (username, display_name, role) VALUES ('ravi',     'Ravi Krishnamurthy','reviewer');
INSERT INTO harald_users (username, display_name, role) VALUES ('emmanuel', 'Emmanuel Vouvakis', 'contributor');
INSERT INTO harald_users (username, display_name, role) VALUES ('nadia',    'Nadia Shaikh',      'contributor');

-- ---------------------------------------------------------------------------
-- Seed: a general-purpose format profile so assembly works on day one.
-- ---------------------------------------------------------------------------
INSERT INTO harald_format_profiles (name, agency, page_order, heading_scheme, page_limits,
  required_forms, font_name, font_size, margin_inches, cover_required, toc_required, notes)
VALUES ('Standard Public-Sector ERP', NULL,
  '[{"key":"transmittal","title":"Letter of Transmittal","source":"generated"},
    {"key":"exec_summary","title":"Executive Summary","source":"generated"},
    {"key":"qualifications","title":"Firm Qualifications and Experience","source":"generated"},
    {"key":"solution","title":"Proposed Solution","source":"requirements"},
    {"key":"methodology","title":"Implementation Approach","source":"generated"},
    {"key":"project_mgmt","title":"Project Management and Governance","source":"generated"},
    {"key":"staffing","title":"Project Team and Key Personnel","source":"generated"},
    {"key":"support","title":"Support and Managed Services","source":"generated"},
    {"key":"references","title":"References and Past Performance","source":"generated"},
    {"key":"contract","title":"Contract Alignment and Exceptions","source":"generated"},
    {"key":"pricing","title":"Cost Proposal","source":"pricing"}]',
  '{"numbered":true,"style":"decimal","uppercase_h1":false}',
  '{}', '[]', 'Calibri', 11, 1, 'Y', 'Y',
  'Default profile. Clone per agency and set page order, limits, and required forms.');

COMMIT;

-- ---------------------------------------------------------------------------
-- Verify
-- ---------------------------------------------------------------------------
SELECT table_name FROM user_tables WHERE table_name LIKE 'HARALD%' ORDER BY 1;
SELECT index_name, index_type FROM user_indexes WHERE table_name LIKE 'HARALD%'
  AND index_type = 'VECTOR' ORDER BY 1;
SELECT username, role FROM harald_users ORDER BY role, username;
