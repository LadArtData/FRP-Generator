# HARALD — OCI Container Instance deployment (AMD64, ap-mumbai-1)
#
# Build (from Cloud Shell or any amd64 host):
#   docker build --platform linux/amd64 -t harald .
# Tag / push:
#   docker tag harald iad.ocir.io/<ns>/harald/harald:latest
#   # mumbai registry:
#   docker tag harald bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest
#   docker push bom.ocir.io/bmi3vxyqnzrv/harald/harald:latest
#
# See deploy/DEPLOY.md for the Container Instance steps.
#
# Two things are baked in at build time so the running container needs no
# network for them:
#   - the ONNX embedding model (local embeddings, no GenAI call)
#   - LibreOffice, used headless to convert assembled DOCX to PDF

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FASTEMBED_CACHE_PATH=/opt/models \
    HOME=/home/harald \
    TNS_ADMIN=/wallet

WORKDIR /app

# libaio is required by oracledb (libaio1 on bookworm); libreoffice-writer
# performs DOCX→PDF. Keep apt lean so the image still fits a Container Instance.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        fonts-dejavu-core \
        libaio1 \
        libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image.
ARG EMBED_MODEL=BAAI/bge-base-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}')" \
    && chmod -R a+rX /opt/models

COPY app ./app
COPY web ./web
COPY data ./data
COPY deploy/entrypoint.sh /entrypoint.sh
# ADB wallet — supplied by CI into the build context (not committed to git).
# Same idea as SCOUT baking ORDS_BASE_URL: the container just runs.
COPY wallet /wallet
RUN chmod +x /entrypoint.sh \
    && useradd --create-home --uid 10001 harald \
    && chown -R harald:harald /app /home/harald /wallet /opt/models /app/data

# ──────────────────────────────────────────────────────────────────
# Same pattern as SCOUT: this tenancy's values are baked in. Create the
# Container Instance, point it at this image, open the port. No env vars.
# ──────────────────────────────────────────────────────────────────
ENV ORACLE_USER="ADMIN" \
    ORACLE_PASSWORD="CloudIteria2026" \
    ORACLE_WALLET_PASSWORD="CloudIteria2026" \
    ORACLE_DSN="zspniy715u9q85u2_high" \
    HARALD_APP_SCHEMA="ITERIA_AI" \
    TNS_ADMIN="/wallet" \
    OCI_REGION="ap-mumbai-1" \
    OCI_OBJECT_NAMESPACE="bmi3vxyqnzrv" \
    GENAI_REGION="us-chicago-1" \
    GENAI_MODEL="meta.llama-4-maverick-17b-128e-instruct-fp8" \
    GENAI_MODEL_OCID="ocid1.generativeaimodel.oc1.us-chicago-1.amaaaaaask7dceyayjawvuonfkw2ua4bob4rlnnlhs522pafbglivtwlfzta" \
    GENAI_COMPARTMENT_ID="ocid1.tenancy.oc1..aaaaaaaatznhqzbky6jdvflzkfvedppvrxbw4weyi2japj37aoagj6kcbfoa" \
    HARALD_BUCKET_OCID="ocid1.bucket.oc1.ap-mumbai-1.aaaaaaaaeh5hhalwfjmk5afmityikg3jjicckwiiao27ejsm45dlqt3i74nq" \
    HARALD_BUCKET_REGION="ap-mumbai-1" \
    HARALD_BUCKET_NAME="FRPStudio" \
    HARALD_SESSION_SECRET="harald-iteria-session-signing-key-do-not-share-outside-tenancy" \
    LOG_LEVEL="INFO"

USER harald

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS http://localhost:8000/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
