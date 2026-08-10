-- Additive: fillable pricing matrix for Brian (approver).
-- Learns from saved / approved matrices across similar proposals.
-- Run as ITERIA_AI (or ADMIN with CURRENT_SCHEMA = ITERIA_AI).

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

CREATE TABLE harald_pricing_matrix (
  matrix_id       NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  opp_id          NUMBER NOT NULL REFERENCES harald_opportunities(opp_id) ON DELETE CASCADE,
  price_id        NUMBER REFERENCES harald_pricing(price_id),
  engagement_type VARCHAR2(80),
  industry        VARCHAR2(120),
  modules         VARCHAR2(400),
  client_name     VARCHAR2(200),
  lines_json      CLOB NOT NULL,
  total_amount    NUMBER,
  currency        VARCHAR2(8) DEFAULT 'USD' NOT NULL,
  status          VARCHAR2(20) DEFAULT 'draft' NOT NULL,
  suggested_from  CLOB,
  locked          CHAR(1) DEFAULT 'N' NOT NULL,
  owner           VARCHAR2(80),
  created_at      TIMESTAMP DEFAULT SYSTIMESTAMP,
  updated_at      TIMESTAMP DEFAULT SYSTIMESTAMP,
  CONSTRAINT harald_pmat_status_ck CHECK (status IN
    ('draft','suggested','reviewed','approved')),
  CONSTRAINT harald_pmat_locked_ck CHECK (locked IN ('Y','N'))
);

CREATE INDEX harald_pmat_opp_idx ON harald_pricing_matrix(opp_id, updated_at DESC);
CREATE INDEX harald_pmat_status_idx ON harald_pricing_matrix(status, industry);
