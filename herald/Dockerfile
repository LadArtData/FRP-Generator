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

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/opt/models \
    HOME=/home/harald \
    TNS_ADMIN=/wallet

WORKDIR /app

# libaio is required by python-oracledb (package name flipped to libaio1t64 on
# newer Debian); libreoffice-writer performs DOCX→PDF.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        unzip \
        fonts-dejavu-core \
        libreoffice-writer \
    && (apt-get install -y --no-install-recommends libaio1t64 \
        || apt-get install -y --no-install-recommends libaio1) \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image.
ARG EMBED_MODEL=BAAI/bge-base-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}')" \
    && chmod -R a+rX /opt/models

COPY app ./app
COPY web ./web
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && mkdir -p /wallet \
    && useradd --create-home --uid 10001 harald \
    && chown -R harald:harald /app /home/harald /wallet /opt/models

# ──────────────────────────────────────────────────────────────────
# Non-secrets — safe in the image. Same tenancy as SCOUT.
# Secrets (ORACLE_PASSWORD, ORACLE_WALLET_PASSWORD, HARALD_SESSION_SECRET,
# HARALD_APPROVER_PASSPHRASE) MUST be set on the Container Instance at runtime.
# ──────────────────────────────────────────────────────────────────
ENV ORACLE_USER="ADMIN" \
    ORACLE_DSN="zspniy715u9q85u2_high" \
    HARALD_APP_SCHEMA="ITERIA_AI" \
    OCI_REGION="ap-mumbai-1" \
    OCI_OBJECT_NAMESPACE="bmi3vxyqnzrv" \
    GENAI_REGION="us-chicago-1" \
    GENAI_MODEL="meta.llama-3.2-90b-vision-instruct" \
    GENAI_MODEL_OCID="ocid1.generativeaimodel.oc1.us-chicago-1.amaaaaaask7dceya2xrydihzvu5pk6vlvfhtbnfapcvwhhugzo7jez4zcnaa" \
    GENAI_COMPARTMENT_ID="ocid1.tenancy.oc1..aaaaaaaatznhqzbky6jdvflzkfvedppvrxbw4weyi2japj37aoagj6kcbfoa" \
    HARALD_BUCKET_OCID="ocid1.bucket.oc1.ap-mumbai-1.aaaaaaaaeh5hhalwfjmk5afmityikg3jjicckwiiao27ejsm45dlqt3i74nq" \
    HARALD_BUCKET_REGION="ap-mumbai-1" \
    HARALD_BUCKET_NAME="FRPStudio" \
    LOG_LEVEL="INFO"

USER harald

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
