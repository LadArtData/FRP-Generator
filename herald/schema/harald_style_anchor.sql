-- Style anchor flag + SQL helper for rhythm calibration.
-- Run in Database Actions (F5) with CURRENT_SCHEMA = iteria_ai.
SET DEFINE OFF;
ALTER SESSION SET CURRENT_SCHEMA = iteria_ai;

BEGIN
  EXECUTE IMMEDIATE
    'ALTER TABLE harald_documents ADD (style_anchor CHAR(1) DEFAULT ''N'' NOT NULL)';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE NOT IN (-1430, -942) THEN RAISE; END IF;
END;
/

BEGIN
  EXECUTE IMMEDIATE
    'ALTER TABLE harald_documents ADD CONSTRAINT harald_doc_style_anchor_ck '
    || 'CHECK (style_anchor IN (''Y'',''N''))';
EXCEPTION
  WHEN OTHERS THEN
    IF SQLCODE NOT IN (-2260, -2261, -2264) THEN RAISE; END IF;
END;
/

COMMENT ON COLUMN harald_documents.style_anchor IS
 'Y = canonical voice reference (rhythm/calibration). Not used for factual retrieval.';

-- Mark St. Petersburg proposal as the style anchor when present.
UPDATE harald_documents
SET    style_anchor = 'Y'
WHERE  doc_class = 'ITERIA_NARRATIVE'
AND    (LOWER(filename) LIKE '%stpetersburg%'
        OR LOWER(client_name) LIKE '%st. petersburg%')
AND    NVL(style_anchor, 'N') = 'N';

COMMIT;

CREATE OR REPLACE TYPE harald_style_anchor_row AS OBJECT (
  chunk_id    NUMBER,
  chunk_text  CLOB
);
/

CREATE OR REPLACE TYPE harald_style_anchor_tab AS TABLE OF harald_style_anchor_row;
/

CREATE OR REPLACE FUNCTION harald_style_anchor(
  p_doc_id NUMBER DEFAULT NULL,
  p_limit  NUMBER DEFAULT 999
) RETURN harald_style_anchor_tab PIPELINED
IS
  l_limit NUMBER := LEAST(GREATEST(NVL(p_limit, 999), 1), 5000);
BEGIN
  FOR rec IN (
    SELECT c.chunk_id, c.chunk_text
    FROM   harald_chunks c
    JOIN   harald_documents d ON d.doc_id = c.doc_id
    WHERE  d.doc_class = 'ITERIA_NARRATIVE'
    AND    d.style_anchor = 'Y'
    AND    (p_doc_id IS NULL OR d.doc_id = p_doc_id)
    ORDER  BY c.chunk_index
    FETCH FIRST l_limit ROWS ONLY
  ) LOOP
    PIPE ROW (harald_style_anchor_row(rec.chunk_id, rec.chunk_text));
  END LOOP;
  RETURN;
END;
/
