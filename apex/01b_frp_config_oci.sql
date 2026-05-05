-- ═══════════════════════════════════════════════════════════════════
-- FRP_CONFIG — OCI Generative AI identifiers
-- Run AFTER 01_schema_additions.sql (FRP_CONFIG must exist).
-- These rows are read by future chat-model code (rfp.parser_model,
-- draft.writer_model). They are NOT used by embed_one — that path
-- uses OCI$RESOURCE_PRINCIPAL and resolves the compartment implicitly.
-- ═══════════════════════════════════════════════════════════════════

ALTER SESSION SET CURRENT_SCHEMA = ITERIA_AI;

MERGE INTO FRP_CONFIG c USING (
  SELECT 'oci.compartment_id' AS k,
         'ocid1.tenancy.oc1..aaaaaaaatznhqzbky6jdvflzkfvedppvrxbw4weyi2japj37aoagj6kcbfoa' AS v,
         'string' AS t,
         'OCI compartment (root tenancy) used by Generative AI calls' AS d FROM dual
  UNION ALL
  SELECT 'oci.genai.region',
         'us-chicago-1', 'string',
         'OCI region hosting Generative AI inference' FROM dual
  UNION ALL
  SELECT 'oci.genai.endpoint',
         'https://inference.generativeai.us-chicago-1.oci.oraclecloud.com',
         'string',
         'OCI Generative AI inference endpoint base URL' FROM dual
  UNION ALL
  SELECT 'oci.cohere.chat_model_ocid',
         'ocid1.generativeaimodel.oc1.us-chicago-1.amaaaaaask7dceyapnibwg42qjhwaxrlqfpreueirtwghiwvv2whsnwmnlva',
         'string',
         'Cohere chat model OCID — generic backend chat' FROM dual
  UNION ALL
  SELECT 'oci.llama.parser_model_ocid',
         'ocid1.generativeaimodel.oc1.us-chicago-1.amaaaaaask7dceya2xrydihzvu5pk6vlvfhtbnfapcvwhhugzo7jez4zcnaa',
         'string',
         'Meta Llama model OCID — used by rfp.parser_model for document tagging' FROM dual
) src
ON (c.cfg_key = src.k)
WHEN MATCHED THEN UPDATE SET c.cfg_value = src.v, c.cfg_type = src.t,
                             c.description = src.d, c.updated_at = CURRENT_TIMESTAMP
WHEN NOT MATCHED THEN INSERT (cfg_key, cfg_value, cfg_type, description)
                      VALUES (src.k, src.v, src.t, src.d);

COMMIT;

SELECT cfg_key, cfg_value FROM FRP_CONFIG
 WHERE cfg_key LIKE 'oci.%' ORDER BY cfg_key;
