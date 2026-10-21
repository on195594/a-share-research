# Multi-stage build for web (frontend) and worker (backend)
FROM python:3.13-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /bin/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY a_share_research ./a_share_research

# Frontend Web service: serves HTML & handles OAuth, holds no model/market credentials
FROM base AS web
USER 1000:1000
EXPOSE 8620
# No access log: /oauth_callback query strings carry authorization codes; nginx logs paths only.
CMD ["uvicorn", "a_share_research.web:app", "--host", "0.0.0.0", "--port", "8620", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]

# Backend Worker service: runs queued research, PDF extraction & LLM inference
FROM base AS worker
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils && rm -rf /var/lib/apt/lists/*
USER 1000:1000
CMD ["python", "-m", "a_share_research.worker"]
