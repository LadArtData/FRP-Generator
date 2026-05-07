-- ═══════════════════════════════════════════════════════════════════
-- Studio Copilot — schema additions
-- C2 of the STUDIO_COPILOT_SPEC.md plan.
-- Run as ITERIA_AI in Database Actions → SQL.
-- Idempotent? No — uses CREATE TABLE. Re-running will error harmlessly
-- on the second pass; that's fine.
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

-- ─────────────────────────────────────────────────────────
-- FRP_COPILOT_CONVERSATIONS
-- One row per chat thread. Tied to a proposal (one-per-proposal
-- scope per spec). gist holds a rolling summary the copilot writes
-- every 10 messages so older context survives the 10-turn window.
-- ─────────────────────────────────────────────────────────
CREATE TABLE FRP_COPILOT_CONVERSATIONS (
  conversation_id        VARCHAR2(64)  DEFAULT LOWER(SYS_GUID()) NOT NULL,
  user_id                VARCHAR2(256) NOT NULL,
  proposal_id            VARCHAR2(64),
  title                  VARCHAR2(200),
  gist                   CLOB,
  gist_anchor_message_id VARCHAR2(64),
  message_count          NUMBER DEFAULT 0,
  created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT pk_copilot_conv  PRIMARY KEY (conversation_id),
  CONSTRAINT uq_copilot_conv  UNIQUE (user_id, proposal_id)
);

CREATE INDEX ix_copilot_conv_user     ON FRP_COPILOT_CONVERSATIONS(user_id, updated_at DESC);
CREATE INDEX ix_copilot_conv_proposal ON FRP_COPILOT_CONVERSATIONS(proposal_id);


-- ─────────────────────────────────────────────────────────
-- FRP_COPILOT_MESSAGES
-- Append-only log. role = user | assistant | tool_result | system.
-- citations_json: array of doc_ids the assistant referenced.
-- tool_calls_json: array of {tool, args} the assistant proposed.
-- applied_json: which tool calls the user clicked Apply on.
-- ─────────────────────────────────────────────────────────
CREATE TABLE FRP_COPILOT_MESSAGES (
  message_id      VARCHAR2(64)  DEFAULT LOWER(SYS_GUID()) NOT NULL,
  conversation_id VARCHAR2(64)  NOT NULL,
  role            VARCHAR2(16)  NOT NULL,
  content         CLOB,
  citations_json  CLOB,
  tool_calls_json CLOB,
  applied_json    CLOB,
  input_tokens    NUMBER,
  output_tokens   NUMBER,
  provider        VARCHAR2(32),
  model           VARCHAR2(128),
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT pk_copilot_msg  PRIMARY KEY (message_id),
  CONSTRAINT fk_copilot_conv FOREIGN KEY (conversation_id)
              REFERENCES FRP_COPILOT_CONVERSATIONS(conversation_id) ON DELETE CASCADE,
  CONSTRAINT ck_copilot_role CHECK (role IN ('user','assistant','tool_result','system'))
);

CREATE INDEX ix_copilot_msg_conv ON FRP_COPILOT_MESSAGES(conversation_id, created_at);


-- Sanity check
SELECT object_name, object_type, status FROM user_objects
 WHERE object_name LIKE 'FRP_COPILOT%' ORDER BY object_type, object_name;
