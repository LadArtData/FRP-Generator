# HARALD container.
#
# Two things are baked in at build time so the running container needs no
# network for them:
#   - the ONNX embedding model (local embeddings, no OCI Generative AI)
#   - LibreOffice, used headless to convert the assembled DOCX to PDF with the
#     layout intact
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/opt/models \
    HOME=/home/harald

WORKDIR /app

# libaio1 is required by python-oracledb; libreoffice-writer performs DOCX to PDF.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libaio1 \
        libreoffice-writer \
        fonts-dejavu-core \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image.
ARG EMBED_MODEL=BAAI/bge-base-en-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBED_MODEL}')" \
    && chmod -R a+rX /opt/models

COPY app ./app
COPY web ./web

# Run unprivileged.
RUN useradd --create-home --uid 10001 harald \
    && chown -R harald:harald /app /home/harald
USER harald

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--timeout-keep-alive", "65"]
