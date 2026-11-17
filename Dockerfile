ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim

ARG SCRIPTS_REVISION=unversioned-local
ENV OTEL_SCRIPTS_REVISION=${SCRIPTS_REVISION} \
    PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
LABEL org.opencontainers.image.revision=${SCRIPTS_REVISION}

WORKDIR /app
COPY grafana_stats/ /app/grafana_stats/
COPY tests/ /app/tests/
COPY scripts/ /app/scripts/
COPY Dockerfile /app/
ENTRYPOINT ["python3", "-m", "grafana_stats.cli"]
