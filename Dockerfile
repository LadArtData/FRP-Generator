# WARDEN worker — OCI Container Instance (SCOUT pattern)

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TNS_ADMIN=/wallet

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends libaio1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wallet /wallet

ENV ORACLE_USER="ADMIN" \
    ORACLE_DSN="" \
    WARDEN_APP_SCHEMA="ITERIA_AI" \
    TNS_ADMIN="/wallet" \
    LOG_LEVEL="INFO" \
    POLL_INTERVAL_SEC="10" \
    HEALTH_PORT="8080"

EXPOSE 8080

CMD ["python", "-m", "app.main"]
