# Web only: no model or market credentials, no research code paths run here.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /bin/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY a_share_research ./a_share_research
USER 1000:1000
EXPOSE 8620
CMD ["uvicorn", "a_share_research.web:app", "--host", "0.0.0.0", "--port", "8620", "--proxy-headers", "--forwarded-allow-ips", "*"]
