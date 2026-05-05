-- ═══════════════════════════════════════════════════════════════════
-- FRP Studio — Schema additions
-- Run as ITERIA_AI in Database Actions → SQL
-- These are additive; no existing tables are touched.
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

-- ─────────────────────────────────────────────────────────
-- 1. FRP_PROPOSAL_ATTACHMENTS
--    Many-to-many link between proposals and library docs.
--    "These 4 won proposals are attached as reference material
--     for the Raleigh ERP draft."
-- ─────────────────────────────────────────────────────────
CREATE TABLE FRP_PROPOSAL_ATTACHMENTS (
  attach_id     VARCHAR2(64)  DEFAULT LOWER(SYS_GUID())  NOT NULL,
  proposal_id   VARCHAR2(64)  NOT NULL,
  doc_id        VARCHAR2(64)  NOT NULL,
  attach_role   VARCHAR2(32)  DEFAULT 'reference',
                              -- reference | template | rfp_source | competitor
  attached_by   VARCHAR2(128),
  attached_at   TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
  notes         VARCHAR2(1000),
  CONSTRAINT pk_frp_attach    PRIMARY KEY (attach_id),
  CONSTRAINT uq_frp_attach    UNIQUE (proposal_id, doc_id, attach_role),
  CONSTRAINT fk_frp_attach_p  FOREIGN KEY (proposal_id)
                              REFERENCES FRP_PROPOSALS(proposal_id) ON DELETE CASCADE,
  CONSTRAINT fk_frp_attach_d  FOREIGN KEY (doc_id)
                              REFERENCES FRP_DOCS(doc_id) ON DELETE CASCADE
);

CREATE INDEX ix_frp_attach_prop ON FRP_PROPOSAL_ATTACHMENTS(proposal_id);
CREATE INDEX ix_frp_attach_doc  ON FRP_PROPOSAL_ATTACHMENTS(doc_id);


-- ─────────────────────────────────────────────────────────
-- 2. FRP_INGEST_LOG
--    Per-file ingest history. Used by the Library page to show
--    "last sync 2 min ago", error counts, and recent activity.
-- ─────────────────────────────────────────────────────────
CREATE TABLE FRP_INGEST_LOG (
  log_id        VARCHAR2(64)  DEFAULT LOWER(SYS_GUID())  NOT NULL,
  bucket_path   VARCHAR2(1024) NOT NULL,
  doc_id        VARCHAR2(64),
  status        VARCHAR2(16),  -- ok | failed | skipped
  size_bytes    NUMBER,
  chunks        NUMBER,
  duration_ms   NUMBER,
  error_msg     VARCHAR2(4000),
  started_at    TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
  finished_at   TIMESTAMP,
  CONSTRAINT pk_frp_ilog PRIMARY KEY (log_id)
);

CREATE INDEX ix_frp_ilog_path     ON FRP_INGEST_LOG(bucket_path);
CREATE INDEX ix_frp_ilog_status   ON FRP_INGEST_LOG(status, started_at);
CREATE INDEX ix_frp_ilog_started  ON FRP_INGEST_LOG(started_at DESC);


-- ─────────────────────────────────────────────────────────
-- 3. FRP_CONFIG
--    Key/value bag for per-deployment settings.
--    Lets us tune chunk size, model selection per task, etc.
--    without redeploying packages.
-- ─────────────────────────────────────────────────────────
CREATE TABLE FRP_CONFIG (
  cfg_key       VARCHAR2(128) NOT NULL,
  cfg_value     VARCHAR2(4000),
  cfg_type      VARCHAR2(16) DEFAULT 'string', -- string | number | json | bool
  description   VARCHAR2(500),
  updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_by    VARCHAR2(128),
  CONSTRAINT pk_frp_config PRIMARY KEY (cfg_key)
);

-- Seed sensible defaults
INSERT INTO FRP_CONFIG (cfg_key, cfg_value, cfg_type, description) VALUES
  ('chunk.max_words',       '800',    'number', 'Words per chunk before embedding');
INSERT INTO FRP_CONFIG (cfg_key, cfg_value, cfg_type, description) VALUES
  ('chunk.overlap_words',   '80',     'number', 'Overlap between consecutive chunks');
INSERT INTO FRP_CONFIG (cfg_key, cfg_value, cfg_type, description) VALUES
  ('embed.model',           'cohere.embed-v4.0', 'string', 'OCI Generative AI embedding model');
INSERT INTO FRP_CONFIG (cfg_key, cfg_value, cfg_type, description) VALUES
  ('rfp.parser_model',      'llama3.3', 'string', 'LLM used to extract structured fields from an RFP');
INSERT INTO FRP_CONFIG (cfg_key, cfg_value, cfg_type, description) VALUES
  ('draft.writer_model',    'grok4',    'string', 'LLM used to draft proposal narrative');
INSERT INTO FRP_CONFIG (cfg_key, cfg_value, cfg_type, description) VALUES
  ('match.top_k',           '5',        'number', 'Default top-K library matches per requirement');
INSERT INTO FRP_CONFIG (cfg_key, cfg_value, cfg_type, description) VALUES
  ('library.exclude_status_at_match', '["no_bid","tool_output"]', 'json',
   'Statuses to exclude from match results so we do not echo our own AI drafts or non-bids');
COMMIT;


-- ─────────────────────────────────────────────────────────
-- Sanity check
-- ─────────────────────────────────────────────────────────
SELECT object_type, object_name, status
  FROM user_objects
 WHERE object_name IN ('FRP_PROPOSAL_ATTACHMENTS','FRP_INGEST_LOG','FRP_CONFIG')
 ORDER BY object_type, object_name;

SELECT cfg_key, cfg_value FROM FRP_CONFIG ORDER BY cfg_key;
