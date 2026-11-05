FROM python:3.12-slim@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app

FROM base AS dependencies
ENV UV_LINK_MODE=copy
COPY deploy/crawl4ai/pyproject.toml deploy/crawl4ai/uv.lock ./
RUN pip install --no-cache-dir uv==0.13.4 && uv sync --locked --no-dev --no-install-project

FROM base
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=dependencies /app/.venv/ /app/.venv/
COPY deploy/crawl4ai/app.py deploy/crawl4ai/parse_html.py ./
USER 1000:1000
EXPOSE 11235
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "11235", "--no-access-log", "--limit-concurrency", "8"]
